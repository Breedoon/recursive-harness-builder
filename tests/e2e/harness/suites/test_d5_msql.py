"""M-SQL: old committed schema/actual native rows, migration and live lifecycle.

Run only through obs-e2e, two lanes, --canary-cli (not --cli-path): the fixture
varies runtime deliberately. The rc1 API projection is not current production.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.suite('d5-msql'), pytest.mark.needs(all_lanes=True, credential=('dev', 'sub'))]


async def _operation(lane, *args):
    code = lane.info['code_dir']
    result = await lane.exec('/workspace/obs/.venv/bin/python',
        f'{code}/tests/e2e/harness/suites/d5_msql_lane.py', *args,
        env={'PYTHONPATH': f'{code}/src:{code}'}, cwd=code, timeout=180)
    assert result['rc'] == 0, result
    return json.loads(result['stdout'])


async def test_d5_old_schema_migration_and_native_restore(lane):
    assert lane.manifest.get('canary_cli'), '--canary-cli is required; skipping is not evidence'
    target = lane.manifest['canary_cli']
    proof = {'run_id': lane.manifest['run_id'], 'lane': lane.name, 'canary_cli': target,
             'canary_sha256': lane.manifest.get('canary_cli_sha256')}
    destination = lane.run_dir / 'tests' / lane.name / 'd5-msql.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert len(lane.manifest['lanes']) == 2, 'exactly two required scenarios: migration and first deploy'
        if lane.name == 'lane1':
            proof['scenario'] = 'old-schema migration'
            proof['migration'] = await _operation(lane, 'initialize')
            proof['roundtrip'] = await _operation(lane, 'roundtrip', target)
        else:
            proof['scenario'] = 'first deploy: runtime DB absent'
            proof['first_deploy'] = await _operation(lane, 'assert-absent')
        nonce = uuid.uuid4().hex[:8].upper()
        topic = await lane.new_topic(f'd5 msql {nonce}')
        first = await lane.turn(topic, f'Reply with exactly: BASE-{nonce}')
        assert f'BASE-{nonce}' in first.text
        agent = first.end['agent_name']
        canary_file = lane.lane_dir / 'state' / 'obs-cli-canary.json'
        model = lane.find('session_build', agent=agent)[-1]['model']
        canary_file.write_text(json.dumps({'cli_path': target, 'agents': [], 'teams': [], 'percentage': 100,
                                          'models': [model], 'new_sessions_only': True}))
        await lane.command(topic, '/clear')
        native = await lane.turn(topic, f'Remember codeword SQL{nonce}. Reply with exactly: INIT-{nonce}')
        assert f'INIT-{nonce}' in native.text
        sid = native.end['session_id']
        proof['native_init'] = {'end': native.end, 'store': await _operation(lane, 'inspect', sid)}
        row = proof['native_init']['store']['raw']
        assert row['cli_path'] == target and row['assignment'] == 'percentage'
        assert row['agent_name'] == agent and row['team_name']
        assert row['created_at'] is not None and row['percentage'] == 100
        proof['restart'] = await lane.restart_obs('maintenance')
        restored = await lane.turn(topic, 'What codeword did I ask you to remember? Reply with only the codeword.')
        assert restored.end['session_id'] == sid and f'SQL{nonce}' in restored.text
        proof['native_restore'] = {'end': restored.end, 'store': await _operation(lane, 'inspect', sid),
                                   'codeword_present': f'SQL{nonce}' in restored.text}
        restored_row = proof['native_restore']['store']['raw']
        assert restored_row['cli_path'] == target and restored_row['assignment'] == 'percentage'
        assert restored_row['created_at'] == row['created_at']
        assert restored_row['agent_name'] == agent and restored_row['team_name'] == row['team_name']
        assert restored_row['reason'] == 'resume stays on the recorded obs-cli (percentage)'
        processes = await lane.procs()
        proof['restored_processes'] = [r for r in processes if r.get('args') and '--resume' in r['args'] and sid in r['args']]
        assert proof['restored_processes'] and all(r['args'][0] == target for r in proof['restored_processes'])
        proof['candidate_stop_for_rollback'] = await lane.supervisor('stop')
        assert proof['candidate_stop_for_rollback']['rc'] == 0
        try:
            proof['actual_production_rollback'] = await _operation(lane, 'production-rollback')
        finally:
            proof['candidate_roll_forward_restart'] = await lane.restart_obs('plain')
        forward = await lane.turn(topic, 'What codeword did I ask you to remember? Reply with only the codeword.')
        assert forward.end['session_id'] == sid and f'SQL{nonce}' in forward.text
        proof['roll_forward'] = {'end': forward.end, 'store': await _operation(lane, 'inspect', sid),
                                 'codeword_present': f'SQL{nonce}' in forward.text}
        forward_row = proof['roll_forward']['store']['raw']
        assert forward_row['cli_path'] == target and forward_row['assignment'] == 'percentage'
        assert forward_row['created_at'] == row['created_at']
        assert forward_row['agent_name'] == row['agent_name'] and forward_row['team_name'] == row['team_name']
        db = lane.lane_dir / 'state' / 'obs-cli-runtime.sqlite3'
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            schema = [list(r) for r in conn.execute('PRAGMA table_info(session_runtime)')]
            proof['final_schema'] = schema
            assert [r[1] for r in schema] == ['session_id', 'runtime', 'cli_path', 'updated_at', 'created_at', 'reason', 'assignment', 'percentage', 'agent_name', 'team_name']
            old_rows = proof.get('migration', {}).get('before', {}).get('session_runtime', [])
            retained = [dict(conn.execute('SELECT session_id,runtime,cli_path,updated_at FROM session_runtime WHERE session_id=?',
                                        (r['session_id'],)).fetchone()) for r in old_rows]
            assert retained == old_rows
            proof['old_rows_after_native_restore'] = retained
            proof['final_integrity'] = [list(r) for r in conn.execute('PRAGMA integrity_check')]
            proof['final_foreign_keys'] = [list(r) for r in conn.execute('PRAGMA foreign_key_check')]
        assert proof['final_integrity'] == [['ok']] and proof['final_foreign_keys'] == []
    finally:
        destination.write_text(json.dumps(proof, indent=2, default=str) + '\n')
