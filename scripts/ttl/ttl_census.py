"""Census of system-breakpoint cache TTL per proxy epoch / session for Opus main-thread requests."""
import gzip, json, os, sys, collections
root = '/workspace/runtime/logs/cache-proxy/requests'
import datetime
days = sys.argv[1:] or [datetime.datetime.utcnow().strftime('%Y-%m-%d')]
rows = []
for day in days:
    p = os.path.join(root, day)
    for fn in sorted(os.listdir(p)):
        if not fn.endswith('.pre.json.gz'):
            continue
        try:
            d = json.load(gzip.open(os.path.join(p, fn)))
        except Exception:
            continue
        model = d.get('model', '')
        if 'opus' not in model and 'sonnet' not in model:
            continue
        ttls = []
        for blk in d.get('system') or []:
            cc = blk.get('cache_control') if isinstance(blk, dict) else None
            if cc:
                ttls.append(cc.get('ttl', '5m'))
        parts = fn.split('-')
        epoch = parts[1]
        sess = fn.split('-')[3].split('.')[0]
        rows.append((fn[:22], epoch, sess, model, tuple(ttls)))
by = collections.defaultdict(lambda: collections.Counter())
first = {}
for ts, ep, se, m, t in rows:
    key = (ep, se)
    by[key][t] += 1
    first.setdefault(key, ts)
mixed = 0
for (ep, se), c in sorted(by.items(), key=lambda kv: first[kv[0]]):
    flag = ''
    if any('5m' in t for t in c):
        flag = '  <== 5m'
    print(first[(ep, se)], ep, se, dict(c), flag)
