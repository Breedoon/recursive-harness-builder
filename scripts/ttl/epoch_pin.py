import gzip, json, os, sys, collections
"""Per-proxy-epoch TTL pin census: incoming (pre) vs forwarded (wire) system-breakpoint TTLs.
Usage: epoch_pin.py <proxy_pid> [YYYY-MM-DD]  (pid = the proxy process id in request-log filenames)."""
import datetime
ep = sys.argv[1]
day = sys.argv[2] if len(sys.argv) > 2 else datetime.datetime.utcnow().strftime('%Y-%m-%d')
R = '/workspace/runtime/logs/cache-proxy/requests/' + day
c = collections.Counter(); sessions = collections.defaultdict(collections.Counter)
for fn in sorted(os.listdir(R)):
    if f'-{ep}-' not in fn or not fn.endswith('.pre.json.gz'):
        continue
    rid = fn[:-len('.pre.json.gz')]
    try:
        p = json.load(gzip.open(os.path.join(R, fn))); w = json.load(gzip.open(os.path.join(R, rid + '.wire.json.gz')))
    except Exception:
        continue
    if 'opus' not in p.get('model', '') and 'sonnet' not in p.get('model', ''):
        continue
    t = lambda d: tuple(b.get('cache_control', {}).get('ttl', '5m') for b in d.get('system') or [] if b.get('cache_control'))
    k = (t(p), t(w)); c[k] += 1; sessions[rid.split('-')[-1]][k] += 1
print('epoch', ep, dict(c))
for s, v in sessions.items():
    if any(k[0] != k[1] or '5m' in k[1] for k in v):
        print('  ', s, dict(v))
