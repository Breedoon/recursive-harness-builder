"""Read-only integrity admission for the standalone caching weekly monitor."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import pathlib
import re
import sqlite3
import time

CLASSES = ('HIT', 'MISS', 'EDGE', 'SKIP')
TOKENS = ('cache_read', 'cache_creation', 'input_tokens')
ROUTE = {
    'principal': '6b3cac918e-caching-solidification-successor-supervisor',
    'wait_resume_owner': '6febe8b574-caching-whole-amended-content-takeover-depth3',
    'implementation_conduit': 'fe06d8430d-caching-solidification-flat-router',
    'registry': '2026-10-08-23-33-topic-16234',
    'team_name': '2026-10-08-23-33-topic-16234',
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def iso(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class Refuse(Exception):
    def __init__(self, field, decision, evidence=None):
        self.field, self.decision, self.evidence = field, decision, evidence


def require(condition, field, decision='INCONSISTENT_SUMMARY', evidence=None):
    if not condition:
        raise Refuse(field, decision, evidence)


def safe(value, pattern, default=''):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else default


def classify(rows):
    lanes = collections.defaultdict(list)
    for row in rows:
        identity = row['session_id'] or '_unknown_' + str(row['source_offset'])
        lanes[(identity, row['route'], row['model'])].append(row)
    for lane in lanes.values():
        baseline = min((r['cache_read'] for r in lane[1:] if r['cache_read'] > 0), default=0)
        prior = None
        for row in lane:
            prefix = prior['prompt_total'] - baseline if prior else 0
            rate = (row['cache_read'] - baseline) / prefix if prefix > 0 else 0.0
            delta = abs(prefix - (row['cache_read'] - baseline))
            cls = 'SKIP' if prefix <= 0 else ('HIT' if rate >= .95 or (delta < 2000 and rate >= .5) else 'MISS' if rate < .05 else 'EDGE')
            row['classification_observational'] = cls
            row['uncached_tokens'] = row['cache_creation'] + row['input_tokens']
            prior = row


def consume(root: pathlib.Path, *, now: float | None = None):
    now = time.time() if now is None else now
    receipt = {'schema': 'obs-cache-health-consumer/1', 'checked_at': now,
               'allow_observational_assessment': False, 'qualified95': False,
               'new_burner_attribution': False, 'route': ROUTE,
               'source_receipts': {}, 'unknowns': []}
    field = 'input'
    try:
        files = {}
        for name in ('latest.json', 'hourly-summary.json', 'session-summary.json', 'samples.json', 'receipts.jsonl'):
            field = name
            raw = (root / name).read_bytes()
            receipt['source_receipts'][name] = {'path': str(root / name), 'sha256': sha(raw), 'bytes': len(raw)}
            files[name] = raw
        latest = json.loads(files['latest.json'])
        generation = latest['collected_at']
        receipt['collection_identity'] = generation
        for name in ('latest.json', 'hourly-summary.json', 'session-summary.json', 'samples.json'):
            field = name + '.collected_at'
            value = json.loads(files[name])
            require(value.get('collected_at') == generation, field, 'INCOMPLETE_GENERATION')
            require(value.get('schema') == 'obs-cache-health/1', name + '.schema', 'INCOMPLETE_GENERATION')
        logs = [json.loads(line) for line in files['receipts.jsonl'].splitlines() if line.strip()]
        matches = [r for r in logs if r.get('collected_at') == generation]
        require(len(matches) == 1, 'receipts.collection_identity', 'MISSING_OR_INVALID_RECEIPT', {'matches': len(matches)})
        collection_receipt = matches[0]
        for key in ('schema', 'records_retained', 'new_rows_read', 'invalid_rows_this_collection', 'observed_last_ts', 'source_offset_end', 'collection_gap_seconds'):
            require(collection_receipt.get(key) == latest.get(key), 'receipts.' + key, 'MISSING_OR_INVALID_RECEIPT')
        age = now - iso(generation)
        receipt['finish_freshness_seconds'] = age
        receipt['start_cutoff_semantics'] = 'retention uses collector invocation start; collected_at is finish; no finish-derived expiry assertion'
        require(-60 <= age <= 5400, 'latest.collected_at', 'STALE_COLLECTOR', {'age_seconds': age})
        # Absence of old usage is not an absent collector or absent daily tier.
        gap = latest.get('collection_gap_seconds')
        if gap is not None:
            require(0 <= gap <= 5400, 'receipts.collection_gap_seconds', 'COLLECTOR_GAP', {'gap_seconds': gap})
        field = 'records.sqlite3'
        db_path = root / field
        require(db_path.is_file(), field, 'MISSING_FILE')
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.execute('BEGIN')
            require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'records.integrity', 'INCOMPLETE_GENERATION')
            rows = [json.loads(x[0]) for x in db.execute('SELECT data FROM records ORDER BY ts,key')]
            cursor = db.execute('SELECT inode,offset FROM cursor WHERE id=1').fetchone()
            require(cursor == (latest['source_inode'], latest['source_offset_end']), 'records.cursor', 'INCOMPLETE_GENERATION')
        require(len(rows) == latest['records_retained'], 'records_retained')
        counts = collections.Counter()
        totals = collections.Counter()
        hourly, sessions = {}, {}
        by_id = {}
        for row in rows:
            req = row['req_id']
            if req:
                require(req not in by_id, 'records.req_id.uniqueness', 'IDENTITY_JOIN_REFUSED', {'req_id': req})
                by_id[req] = row
            for key in TOKENS:
                require(type(row[key]) is int and row[key] >= 0, 'records.' + key, 'TOKEN_JOIN_REFUSED', {'req_id': req})
                totals[key] += row[key]
            require(row['prompt_total'] == sum(row[k] for k in TOKENS), 'records.prompt_total', 'TOKEN_JOIN_REFUSED', {'req_id': req})
        classify(rows)
        for row in rows:
            cls = row['classification_observational']
            counts[cls] += 1
            hkey = dt.datetime.fromtimestamp(row['ts'], dt.timezone.utc).strftime('%Y-%m-%dT%H') + '|' + row['route'] + '|' + row['model']
            skey = '|'.join((row['session_id'] or 'unknown', row['route'], row['model']))
            for target, key in ((hourly, hkey), (sessions, skey)):
                agg = target.setdefault(key, collections.Counter())
                agg.update({'calls': 1, cls: 1, 'cache_read': row['cache_read'], 'uncached_tokens': row['uncached_tokens']})
        for cls in CLASSES:
            require(latest['classes_observational'].get(cls, 0) == counts[cls], 'latest.classes_observational.' + cls)
        require(sum(latest['classes_observational'].get(c, 0) for c in CLASSES) == len(rows), 'latest.rowclasssum')
        for name, expected in (('hourly-summary.json', hourly), ('session-summary.json', sessions)):
            summary = json.loads(files[name])['rows']
            seen = set()
            for item in summary:
                key = (item['hour'] if name.startswith('hourly') else item.get('session_id') or 'unknown') + '|' + item['route'] + '|' + item['model']
                require(key not in seen and key in expected, name + '.rowkey', evidence={'key': key})
                seen.add(key)
                for value in ('calls', 'cache_read', 'uncached_tokens') + CLASSES:
                    require(item.get(value, 0) == expected[key][value], name + '.' + value, evidence={'key': key})
                require(sum(item.get(c, 0) for c in CLASSES) == item['calls'], name + '.rowclasssum', evidence={'key': key})
            require(seen == set(expected), name + '.keyset')
        require(totals['cache_creation'] + totals['input_tokens'] == sum(v['uncached_tokens'] for v in hourly.values()), 'totals.creation_plus_input')
        receipt['totals'] = dict(totals, uncached_tokens=totals['cache_creation'] + totals['input_tokens'], calls=len(rows), classes={c: counts[c] for c in CLASSES})
        joins = collections.Counter(r.get('index_join', 'unjoined') for r in rows)
        require(dict(joins) == latest['joins'], 'latest.joins', 'IDENTITY_JOIN_REFUSED')
        field = 'usage.source'
        usage_path = pathlib.Path(latest['source'])
        usage_unknown = 0
        with usage_path.open('rb') as source:
            for row in rows:
                field = 'usage.source_offset/LF_sha256'
                source.seek(row['source_offset'])
                raw = source.readline()
                source_ok = raw.endswith(b'\n') and sha(raw) == row['usage_line_sha256']
                # Legacy unjoined retained rows can lack a supported current source binding.
                if not source_ok and row.get('index_join', 'unjoined') == 'unjoined':
                    usage_unknown += 1
                    continue
                require(source_ok, field, 'SOURCE_JOIN_REFUSED', {'req_id': row['req_id'], 'offset': row['source_offset'], 'expected_line_sha256': row['usage_line_sha256'], 'observed_line_sha256': sha(raw)})
                src = json.loads(raw)
                require(all(src.get(k, 0) == row[k] for k in TOKENS), 'usage.tokens', 'TOKEN_JOIN_REFUSED', {'req_id': row['req_id']})
                identity = {
                    'req_id': safe(src.get('req_id'), r'[A-Za-z0-9_.-]{1,160}'),
                    'session_id': safe(src.get('session_id'), r'[0-9a-f-]{36}'),
                    'model': safe(src.get('model'), r'[A-Za-z0-9_.:/\[\]-]{1,100}', 'unknown'),
                    'route': safe(src.get('route'), r'[A-Za-z0-9_-]{1,50}', 'unknown'),
                }
                require(all(value == row[k] for k, value in identity.items()), 'usage.identity.model_route_session_req', 'IDENTITY_JOIN_REFUSED', {'req_id': row['req_id']})
        receipt['unknowns'].append({'unjoined_records': joins.get('unjoined', 0), 'unsupported_old_source_bindings': usage_unknown, 'status': 'UNKNOWN; not forged joins or healthy-fleet proof'})
        index_seen = collections.Counter()
        index_audited = 0
        # Audit every retained ledger, not just the collector last-row-wins label.
        for ledger in latest['index_sources']:
            field = 'index.source'
            path = pathlib.Path(ledger['path'])
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for number in range(ledger['lines']):
                    offset = stream.tell()
                    raw = stream.readline()
                    require(raw.endswith(b'\n'), 'index.offset/LF_line', 'SOURCE_JOIN_REFUSED', {'line': number + 1, 'offset': offset})
                    digest.update(raw)
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        raise Refuse('index.line_json', 'SOURCE_JOIN_REFUSED', {'line': number + 1, 'offset': offset})
                    req = item.get('req_id')
                    if not req:
                        continue
                    index_seen[req] += 1
                    require(index_seen[req] == 1, 'index.req_id.uniqueness', 'DUPLICATE_INDEX_REFUSED', {'req_id': req, 'line': number + 1, 'offset': offset, 'LF_sha256': sha(raw)})
                    row = by_id.get(req)
                    if row is None:
                        continue
                    require(row.get('index_join') == 'exact_id_identity_tokens', 'records.index_join', 'IDENTITY_JOIN_REFUSED', {'req_id': req})
                    require(str(path) == row.get('index_source'), 'index.source_path', 'IDENTITY_JOIN_REFUSED', {'req_id': req})
                    require(all(item.get(k) == row[k] for k in ('session_id', 'model', 'route')), 'index.identity.model_route_session', 'IDENTITY_JOIN_REFUSED', {'req_id': req})
                    require(all((item.get('usage') or {}).get(k) == row[k] for k in TOKENS), 'index.tokens', 'TOKEN_JOIN_REFUSED', {'req_id': req})
                    require(item.get('wire_sha256') == row.get('wire_sha256'), 'index.wire_sha256', 'IDENTITY_JOIN_REFUSED', {'req_id': req})
                    index_audited += 1
            require(digest.hexdigest() == ledger['sha256_read_bytes'], 'index.prefix_sha256', 'SOURCE_JOIN_REFUSED', {'path': str(path), 'lines': ledger['lines']})
        for row in rows:
            if row.get('index_join') == 'exact_id_identity_tokens':
                require(index_seen[row['req_id']] == 1, 'index.exact_id_present', 'IDENTITY_JOIN_REFUSED', {'req_id': row['req_id']})
            elif row.get('index_join', 'unjoined') != 'unjoined':
                raise Refuse('records.index_join', 'IDENTITY_JOIN_REFUSED', {'req_id': row['req_id'], 'join': row.get('index_join')})
        samples = json.loads(files['samples.json'])
        require(samples['samples'] == latest['samples'], 'samples.latest_rows')
        require(samples['prefix_samples'] == latest['prefix_samples'], 'samples.prefix_samples')
        for row in samples['samples'] + samples['prefix_samples']:
            if row['req_id']:
                actual = by_id.get(row['req_id'])
                require(actual is not None, 'samples.req_id', 'IDENTITY_JOIN_REFUSED')
                for key in TOKENS + ('session_id', 'model', 'route', 'source_offset', 'usage_line_sha256', 'classification_observational'):
                    require(row.get(key) == actual.get(key), 'samples.' + key, 'IDENTITY_JOIN_REFUSED', {'req_id': row['req_id']})
            if 'prefix_sample' in row:
                field = 'stored_redacted_body'
                meta = row['prefix_sample']
                require(meta == by_id[row['req_id']].get('prefix_sample'), field + '.metadata_join', 'STORED_BODY_REFUSED', {'req_id': row['req_id']})
                with gzip.open(meta['path'], 'rb') as stream:
                    raw = stream.read(32 * 1024 * 1024 + 1)
                require(len(raw) <= 32 * 1024 * 1024, field + '.size', 'STORED_BODY_REFUSED')
                require(sha(raw) == meta['stored_body_sha256'], field + '.sha256', 'STORED_BODY_REFUSED', {'req_id': row['req_id']})
                body = json.loads(raw)
                projection = json.dumps({k: body.get(k) for k in ('tools', 'system')}, sort_keys=True, separators=(',', ':')).encode()
                require(sha(projection) == meta['tools_system_redacted_sha256'], field + '.projection_sha256', 'STORED_BODY_REFUSED', {'req_id': row['req_id']})
        # Detect a changing publication during the audit, without retry/poll.
        require((root / 'latest.json').read_bytes() == files['latest.json'], 'latest.changed_during_audit', 'INCOMPLETE_GENERATION')
        receipt['index_rows_audited'] = index_audited
        receipt['latest_usage_age_seconds'] = latest.get('latest_usage_age_seconds')
        receipt['usage_activity'] = 'ACTIVITY_LOGGING_UNKNOWN' if latest.get('latest_usage_age_seconds') is None or latest['latest_usage_age_seconds'] > 5400 or latest.get('missing_usage_hours_last7days') else 'OBSERVED_RECENT_USAGE'
        receipt.update(decision='VALID_OBSERVATIONAL_INPUT', allow_observational_assessment=True, field=None)
    except Refuse as exc:
        receipt.update(decision=exc.decision, field=exc.field, evidence=exc.evidence)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        receipt.update(decision='MISSING_OR_INVALID_INPUT', field=field, error_type=type(exc).__name__)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, default=pathlib.Path('/workspace/runtime/state/cache-health'))
    parser.add_argument('--receipt', type=pathlib.Path, required=True)
    args = parser.parse_args()
    receipt = consume(args.root)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'decision': receipt['decision'], 'field': receipt.get('field'), 'receipt': str(args.receipt), 'route': receipt['route']}, sort_keys=True))
    raise SystemExit(0 if receipt['allow_observational_assessment'] else 2)


if __name__ == '__main__':
    main()
