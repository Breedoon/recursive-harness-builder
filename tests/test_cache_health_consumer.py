import copy
import datetime as dt
import gzip
import json
import sqlite3

import pytest

from obs_agent.cache_health_consumer import consume, sha


NOW = dt.datetime(2026, 10, 10, 21, 19, tzinfo=dt.timezone.utc).timestamp()


def write_json(path, value):
    path.write_text(json.dumps(value) + '\n')


@pytest.fixture
def feed(tmp_path):
    root = tmp_path
    source = root / 'usage.jsonl'
    index = root / 'index.jsonl'
    source_rows = [dict(req_id=f'req-{i}', session_id='11111111-1111-1111-1111-111111111111', model='gpt-6.1-sol', route='cli-proxy', ts=NOW - 30 + i, cache_read=0, cache_creation=10, input_tokens=10) for i in range(3)]
    records = []
    with source.open('wb') as stream:
        for item in source_rows:
            raw = (json.dumps(item) + '\n').encode()
            records.append(dict(item, source_offset=stream.tell(), usage_line_sha256=sha(raw), prompt_total=20, index_join='exact_id_identity_tokens', index_source=str(index), wire_sha256='a' * 64))
            stream.write(raw)
    raw_index = b''.join((json.dumps(dict(req_id=r['req_id'], session_id=r['session_id'], model=r['model'], route=r['route'], usage={k:r[k] for k in ('cache_read', 'cache_creation', 'input_tokens')}, wire_sha256='a' * 64)) + '\n').encode() for r in records)
    index.write_bytes(raw_index)
    body = b'{"tools":[],"system":[]}'
    bodypath = root / 'redacted.wire.json.gz'
    bodypath.write_bytes(gzip.compress(body))
    records[-1]['prefix_sample'] = dict(path=str(bodypath), stored_body_sha256=sha(body), tools_system_redacted_sha256=sha(b'{"system":[],"tools":[]}'))
    db = sqlite3.connect(root / 'records.sqlite3')
    db.execute('CREATE TABLE records (key TEXT PRIMARY KEY,ts REAL,data TEXT)')
    db.execute('CREATE TABLE cursor (id INTEGER PRIMARY KEY,inode INTEGER,offset INTEGER)')
    db.executemany('INSERT INTO records VALUES (?,?,?)', [(r['req_id'], r['ts'], json.dumps(r)) for r in records])
    db.execute('INSERT INTO cursor VALUES (1,?,?)', (source.stat().st_ino, source.stat().st_size))
    db.commit(); db.close()
    generation = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat()
    samples = [dict(r, classification_observational='SKIP' if i == 0 else 'MISS', uncached_tokens=20) for i, r in enumerate(records)]
    latest = dict(schema='obs-cache-health/1', collected_at=generation, records_retained=3, new_rows_read=3, invalid_rows_this_collection=0, observed_last_ts=records[-1]['ts'], source=str(source), source_inode=source.stat().st_ino, source_offset_end=source.stat().st_size, collection_gap_seconds=3600, latest_usage_age_seconds=28, classes_observational=dict(SKIP=1,MISS=2), joins=dict(exact_id_identity_tokens=3), index_sources=[dict(path=str(index), lines=3, sha256_read_bytes=sha(raw_index))], samples=samples, prefix_samples=[samples[-1]], missing_usage_hours_last7days=[])
    write_json(root / 'latest.json', latest)
    for name, item in [('hourly-summary.json',dict(hour='2026-10-10T21',route='cli-proxy',model='gpt-6.1-sol')), ('session-summary.json', dict(session_id=records[0]['session_id'],route='cli-proxy',model='gpt-6.1-sol'))]:
        write_json(root/name, dict(schema='obs-cache-health/1', collected_at=generation, rows=[dict(item,calls=3,cache_read=0,uncached_tokens=60,HIT=0,MISS=2,EDGE=0,SKIP=1)]))
    write_json(root/'samples.json', dict(schema='obs-cache-health/1',collected_at=generation,samples=samples,prefix_samples=[samples[-1]]))
    write_json(root/'receipts.jsonl', {k:latest[k] for k in ('schema','collected_at','records_retained','new_rows_read','invalid_rows_this_collection','observed_last_ts','source_offset_end','collection_gap_seconds')})
    return root


def alter(root, name, edit):
    value = json.loads((root/name).read_text())
    edit(value)
    write_json(root/name, value)


def test_valid_and_old_usage_not_outage(feed):
    assert consume(feed, now=NOW)['allow_observational_assessment']
    alter(feed, 'latest.json', lambda v:v.update(latest_usage_age_seconds=10000,missing_usage_hours_last7days=['2026-10-10T20']))
    result = consume(feed, now=NOW)
    assert result['allow_observational_assessment']
    assert result['usage_activity'] == 'ACTIVITY_LOGGING_UNKNOWN'
    assert not result['qualified95'] and not result['new_burner_attribution']


@pytest.mark.parametrize('name,key', [('hourly-summary.json','calls'),('hourly-summary.json','cache_read'),('hourly-summary.json','uncached_tokens'),('session-summary.json','calls'),('session-summary.json','cache_read'),('session-summary.json','uncached_tokens')] + [(n,c) for n in ('hourly-summary.json','session-summary.json') for c in ('HIT','MISS','EDGE','SKIP')])
def test_all_summary_fields_refuse(feed,name,key):
    alter(feed,name,lambda v:v['rows'][0].update({key:v['rows'][0][key]+1}))
    result = consume(feed,now=NOW)
    assert not result['allow_observational_assessment']
    assert result['field'] == name+'.'+key
    assert result['route']['wait_resume_owner'].endswith('takeover-depth3')


@pytest.mark.parametrize('cls', ['HIT','MISS','EDGE','SKIP'])
def test_latest_all_classes_including_absent_zero(feed,cls):
    alter(feed,'latest.json',lambda v:v['classes_observational'].update({cls:v['classes_observational'].get(cls,0)+1}))
    assert consume(feed,now=NOW)['field']=='latest.classes_observational.'+cls


def test_mixed_generation(feed):
    alter(feed,'samples.json',lambda v:v.update(collected_at='2026-10-10T20:19:00Z'))
    assert consume(feed,now=NOW)['decision']=='INCOMPLETE_GENERATION'


def test_missing_receipt(feed):
    (feed/'receipts.jsonl').write_text('')
    assert consume(feed,now=NOW)['decision']=='MISSING_OR_INVALID_RECEIPT'


def test_missing_file(feed):
    (feed/'samples.json').unlink()
    assert not consume(feed,now=NOW)['allow_observational_assessment']


def test_stale(feed):
    assert consume(feed,now=NOW+5401)['decision']=='STALE_COLLECTOR'


def test_gap(feed):
    for name in ('latest.json','receipts.jsonl'):
        alter(feed,name,lambda v:v.update(collection_gap_seconds=5401))
    assert consume(feed,now=NOW)['decision']=='COLLECTOR_GAP'


@pytest.mark.parametrize('field', ['source_offset','usage_line_sha256','cache_creation','input_tokens','model','route','session_id'])
def test_wrong_current_source_join(feed,field):
    db=sqlite3.connect(feed/'records.sqlite3')
    value=json.loads(db.execute("SELECT data FROM records WHERE key='req-1'").fetchone()[0])
    value[field] = value[field]+1 if isinstance(value[field],int) else 'wrong'
    db.execute("UPDATE records SET data=? WHERE key='req-1'",(json.dumps(value),)); db.commit(); db.close()
    assert not consume(feed,now=NOW)['allow_observational_assessment']


def test_duplicate_index_not_last_wins(feed):
    path=feed/'index.jsonl'
    raw=path.read_bytes()+path.read_bytes().splitlines(keepends=True)[0]
    path.write_bytes(raw)
    alter(feed,'latest.json',lambda v:v['index_sources'][0].update(lines=4,sha256_read_bytes=sha(raw)))
    assert consume(feed,now=NOW)['decision']=='DUPLICATE_INDEX_REFUSED'


def test_wrong_index_tokens_with_valid_ledger_hash(feed):
    path=feed/'index.jsonl'
    values=[json.loads(x) for x in path.read_bytes().splitlines()]
    values[1]['usage']['input_tokens']+=1
    raw=b''.join((json.dumps(v)+'\n').encode() for v in values)
    path.write_bytes(raw)
    alter(feed,'latest.json',lambda v:v['index_sources'][0].update(sha256_read_bytes=sha(raw)))
    assert consume(feed,now=NOW)['decision']=='TOKEN_JOIN_REFUSED'


def test_corrupt_stored_redacted_body(feed):
    (feed/'redacted.wire.json.gz').write_bytes(gzip.compress(b'{"tools":[1],"system":[]}'))
    assert consume(feed,now=NOW)['decision']=='STORED_BODY_REFUSED'


def test_forged_projection_metadata(feed):
    for name in ('latest.json','samples.json'):
        def edit(v):
            for row in v['prefix_samples']:
                row['prefix_sample']['tools_system_redacted_sha256']='0'*64
            for row in v['samples']:
                if 'prefix_sample' in row:
                    row['prefix_sample']['tools_system_redacted_sha256']='0'*64
        alter(feed,name,edit)
    assert consume(feed,now=NOW)['decision']=='STORED_BODY_REFUSED'
