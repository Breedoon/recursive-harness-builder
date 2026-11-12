"""Required M-SQL lane operations; never imported/executed by host tooling."""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import sqlite3
import sys
from pathlib import Path

from obs_agent import cli_runtime as current
from tests.e2e.harness.suites import d5_rc1_store as old

DB = Path('/workspace/runtime/state/obs-cli-runtime.sqlite3')
BASE = ('session_id', 'runtime', 'cli_path', 'updated_at')
ADDED = dict(current._ADDED_COLUMNS)


def snapshot():
    with sqlite3.connect(str(DB)) as conn:
        conn.row_factory = sqlite3.Row
        return {table: [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('session_runtime', 'binary_hash', 'cli_path_seen')}


def preserves(before, after):
    for table, rows in before.items():
        columns = BASE if table == 'session_runtime' else tuple(rows[0])
        assert [{k: row[k] for k in columns} for row in after[table]] == rows, table


def initialize():
    assert not DB.exists(), 'native lane must begin without runtime store'
    fixture = json.loads(Path(__file__).with_name('d5_old_rows.json').read_text())
    conn = old._connect(DB, create=True)
    assert conn is not None
    for table, rows in fixture['tables'].items():
        for row in rows:
            columns = tuple(row)
            conn.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', tuple(row.values()))
    conn.commit()
    old_schema = [tuple(row) for row in conn.execute('PRAGMA table_info(session_runtime)')]
    assert [r[1] for r in old_schema] == list(BASE)
    conn.close()
    before = snapshot()
    assert before == fixture['tables']
    # A real prior-schema store with actual producer rows, not five fresh DBs.
    conn = current._connect(DB, create=True)
    conn.commit()
    columns = [tuple(row) for row in conn.execute('PRAGMA table_info(session_runtime)')]
    conn.close()
    after = snapshot()
    preserves(before, after)
    assert {r[1]: r[2] for r in columns} == {
        'session_id': 'TEXT', 'runtime': 'TEXT', 'cli_path': 'TEXT', 'updated_at': 'REAL', **ADDED}
    for row in after['session_runtime']:
        assert all(row[k] is None for k in ADDED)
    for _ in range(3):
        conn = current._connect(DB, create=True)
        conn.commit()
        conn.close()
        assert snapshot() == after
    poisoned = copy.deepcopy(after)
    poisoned['session_runtime'][0]['runtime'] = 'seeded-loss'
    rejected = False
    try:
        preserves(before, poisoned)
    except AssertionError:
        rejected = True
    assert rejected, 'preservation predicate must reject seeded loss'
    return {'fixture': fixture, 'old_schema': old_schema, 'migrated_schema': columns,
            'before': before, 'after': after, 'idempotent_reopens': 3,
            'seeded_loss_rejected': rejected}


def row_for(sid):
    with sqlite3.connect(str(DB)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM session_runtime WHERE session_id=?', (sid,)).fetchone()
        return dict(row) if row else None


def roundtrip(target):
    current.wait_for_hashes()
    records = []
    sid = 'd5-conflict'
    runtime = current.runtime_id_for_path(target, db_path=DB, wait=True)
    operations = [
        (current.BUNDLED_RUNTIME, None, 'percentage-bundled', 50, 'owner-a', 'team-a'),
        (runtime, target, 'percentage', 25, 'owner-b', 'team-b'),
        (runtime, target, 'named', 100, 'owner-c', 'team-c'),
        (current.BUNDLED_RUNTIME, None, 'unselected', None, 'owner-d', 'team-d'),
        (runtime, target, 'percentage', 75, 'owner-e', 'team-e'),
    ]
    created = None
    for index, (rt, path, assignment, percentage, agent, team) in enumerate(operations):
        current.put_record(DB, sid, rt, path, reason=f'd5-operation-{index}', assignment=assignment,
                           percentage=percentage, agent_name=agent, team_name=team)
        raw = row_for(sid)
        loaded = dataclasses.asdict(current.get_record(DB, sid))
        assert raw['runtime'] == rt and raw['cli_path'] == path
        expected_assignment, expected_pct = ('percentage', 25) if index == 2 else (assignment, percentage)
        assert (raw['assignment'], raw['percentage']) == (expected_assignment, expected_pct)
        assert loaded == {k: raw[k] for k in ('runtime', 'cli_path', 'assignment', 'percentage')}
        assert raw['reason'] == f'd5-operation-{index}' and raw['agent_name'] == agent and raw['team_name'] == team
        assert isinstance(raw['created_at'], float) and isinstance(raw['updated_at'], float)
        created = raw['created_at'] if created is None else created
        assert raw['created_at'] == created
        assert raw['updated_at'] >= raw['created_at']
        assert raw['percentage'] is None or isinstance(raw['percentage'], float)
        records.append({'raw': raw, 'reloaded': loaded})
    with sqlite3.connect(str(DB)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM session_runtime WHERE session_id=?', (sid,)).fetchone()[0] == 1
    # The actual prior committed API can read every migrated record's original fields.
    for row in snapshot()['session_runtime']:
        legacy = old.get_record(DB, row['session_id'])
        assert dataclasses.asdict(legacy) == {'runtime': row['runtime'], 'cli_path': row['cli_path']}
    legacy_records = []
    for rt, path in [(current.BUNDLED_RUNTIME, None), (runtime, target), (current.BUNDLED_RUNTIME, None)]:
        old.put_record(DB, 'd5-old-insert', rt, path)
        raw = row_for('d5-old-insert')
        assert (raw['runtime'], raw['cli_path']) == (rt, path)
        assert all(raw[k] is None for k in ADDED)
        assert dataclasses.asdict(current.get_record(DB, 'd5-old-insert')) == {
            'runtime': rt, 'cli_path': path, 'assignment': None, 'percentage': None}
        legacy_records.append(raw)
    candidate_metadata = row_for(sid)
    for rt, path in [(current.BUNDLED_RUNTIME, None), (runtime, target), (current.BUNDLED_RUNTIME, None)]:
        old.put_record(DB, sid, rt, path)
        raw = row_for(sid)
        assert (raw['runtime'], raw['cli_path']) == (rt, path)
        assert all(raw[k] == candidate_metadata[k] for k in ADDED)
        legacy_records.append(raw)
    # An old writer cannot maintain new provenance columns; the current consumer
    # still sees bundled runtime and cannot resurrect an obs-cli percentage winner.
    selected = current.select_runtime(canary=current.Canary('valid', cli_path=target, percentage=100,
                models=frozenset({'claude-haiku-5-5'})), agent_name='d5', team_name='d5', eligible=True,
                resume_session_id=sid, db_path=DB, model='claude-haiku-5-5')
    assert selected.cli_path is None and selected.runtime == current.BUNDLED_RUNTIME
    with sqlite3.connect(str(DB)) as conn:
        integrity = conn.execute('PRAGMA integrity_check').fetchall()
        foreign = conn.execute('PRAGMA foreign_key_check').fetchall()
        duplicates = conn.execute('SELECT session_id, COUNT(*) FROM session_runtime GROUP BY session_id HAVING COUNT(*)<>1').fetchall()
    assert integrity == [('ok',)] and foreign == [] and duplicates == []
    return {'candidate_conflicts': records, 'old_api_insert_and_conflicts': legacy_records,
            'old_api_boundary': 'rc1 API, NOT actual production890ad270 (module absent there)',
            'old_writer_keeps_new_metadata': True, 'current_consumer_after_old_bundled_write': dataclasses.asdict(selected),
            'integrity': integrity, 'foreign_keys': foreign, 'duplicates': duplicates}


def inspect(sid):
    raw = row_for(sid)
    assert raw is not None
    loaded = current.get_record(DB, sid)
    assert loaded is not None
    return {'raw': raw, 'reloaded': dataclasses.asdict(loaded), 'module': current.__file__, 'db': str(DB)}


def production_rollback():
    import hashlib
    import signal
    import socket
    import subprocess
    import time
    import zipfile

    before = snapshot()
    root = DB.parent / 'd5-production-890ad270'
    root.mkdir(exist_ok=True)
    fixture = Path(__file__).with_name('d5_production_source.zip')
    with zipfile.ZipFile(fixture) as archive:
        manifest = json.loads(archive.read('production-source-manifest.json'))
        assert manifest['commit'] == '890ad270bcf1f97cba621bdf906472edb068ff53'
        assert 'src/obs_agent/cli_runtime.py' not in manifest['files']
        for name, digest in manifest['files'].items():
            assert name.startswith('src/') and '..' not in Path(name).parts
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == digest
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    receipt_path = root / 'entrypoint.json'
    bootstrap = (
        "import importlib.util,hashlib,json,runpy; from pathlib import Path; "
        "p=Path(importlib.util.find_spec('obs_agent.telegram_main').origin); "
        f"Path({str(receipt_path)!r}).write_text(json.dumps({{'entrypoint':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
        "'runtime_module_exists':importlib.util.find_spec('obs_agent.cli_runtime') is not None})); "
        "runpy.run_module('obs_agent.telegram_main',run_name='__main__')"
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root / 'src')
    env['OBS_E2E_CODE_DIR'] = str(root)
    log_path = root / 'production-native.log'
    control = Path(env.get('OBS_CONTROL_DIR', '/workspace/runtime/state/obs-control'))
    ready = None
    cleanup = {}
    with log_path.open('wb') as log:
        process = subprocess.Popen(['/workspace/obs/.venv/bin/python', '-c', bootstrap, '--prod'],
                                   env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                assert process.poll() is None, f'production exited {process.returncode}; see native lane log {log_path}'
                try:
                    token = (control / 'token').read_text().strip()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(2)
                        client.connect(str(control / 'control.sock'))
                        client.sendall((json.dumps({'op': 'ping', 'caller': 'd5-msql', 'args': {}, 'token': token}) + '\n').encode())
                        chunks = []
                        while True:
                            chunk = client.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    reply = json.loads(b''.join(chunks))
                    if reply.get('ok'):
                        ready = reply
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)  # obs-e2e-lint: allow — polls external production control socket; timeout only fails readiness
            assert ready is not None, 'actual production entrypoint did not become ready'
            entry = json.loads(receipt_path.read_text())
            assert entry['entrypoint'] == str(root / 'src/obs_agent/telegram_main.py')
            assert entry['sha256'] == manifest['files']['src/obs_agent/telegram_main.py']
            assert entry['runtime_module_exists'] is False
            native_process = {'pid': process.pid, 'argv': [part.decode('utf-8', 'replace') for part in Path(f'/proc/{process.pid}/cmdline').read_bytes().split(b'\0')[:2]],
                              'cwd': str(Path(f'/proc/{process.pid}/cwd').resolve())}
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            # Only this subprocess's fresh session/group, inside the private lane.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                cleanup['remaining_group_signal'] = 'TERM'
            except ProcessLookupError:
                cleanup['remaining_group_signal'] = 'already absent'
            cleanup['entrypoint_exit'] = process.returncode
    after = snapshot()
    assert after == before, 'production rollback changed separate runtime-store contents'
    return {'actual_production_commit': manifest['commit'], 'source_files_verified': len(manifest['files']),
            'entrypoint': entry, 'process': native_process, 'control_ping': ready,
            'runtime_db_before': before, 'runtime_db_after': after, 'runtime_db_unchanged': True,
            'boundary': 'no consumer exists; actual production does NOT open runtime store', 'cleanup': cleanup}


def main():
    assert os.environ.get('OBS_E2E_LANE'), 'lane-only required proof'
    op = sys.argv[1]
    if op == 'initialize':
        result = initialize()
    elif op == 'roundtrip':
        result = roundtrip(sys.argv[2])
    elif op == 'inspect':
        result = inspect(sys.argv[2])
    elif op == 'production-rollback':
        result = production_rollback()
    elif op == 'resolve-model':
        from obs_agent.config import resolve_model_context
        result = {'observed_model': sys.argv[2], 'resolved_model': resolve_model_context(sys.argv[2]).model}
    elif op == 'assert-absent':
        assert not DB.exists()
        result = {'runtime_db_exists': False, 'path': str(DB)}
    else:
        raise ValueError(op)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
