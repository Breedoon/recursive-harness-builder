#!/usr/bin/env python3
"""Fleet cache-hit tally from the cache proxy's usage log (read-only).

CACHE-PREFIX: this is THE way to measure fleet caching before/after a proxy
change. A naive "consecutive requests of one session" count is wrong: since
2026-10-09 (commit acee2c4) the CLI's small utility calls share the session id
and model with the conversation on the GPT route, which made a naive tally
report a false 10-16 % GPT break rate (mission vault-1r7n, E1 findings).

Metrics, per route ("anthropic" = Claude, "cli-proxy" = GPT via CLIProxyAPI):
- share: cache_read / (cache_read + cache_creation + input_tokens).
- break rate: consecutive MAIN-thread requests of one (session, model) with a
  gap <= --gap seconds and a previous prompt >= 10k tokens; a BREAK is a request
  that read < 50 % of the previous prompt. A request < 0.8x the previous main
  request is a side call and is skipped. Pairs above --gap are not counted
  (cache entries may have expired: Claude fleet writes 1-hour entries, Haiku
  5-minute ones).
- short-gap break rate: the same, restricted to gaps <= 30 s (no expiry can
  explain those misses).

Usage:
  cache_tally.py --since 2026-10-12T18:00:00 [--until ...] [--gap 3600]
                 [--breaks-out breaks.jsonl] [--exclude-sessions harness_sessions.txt]
Session ids exist in the usage log only from 2026-10-08 23:53Z.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json

LOG = "/workspace/runtime/logs/cache-proxy/usage.jsonl"


def ts(s: str | None) -> float | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(
        tzinfo=dt.timezone.utc).timestamp()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="UTC ISO time, e.g. 2026-10-12T18:00:00")
    ap.add_argument("--until", default=None)
    ap.add_argument("--gap", type=int, default=3600)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--breaks-out", default=None, help="write every break pair as JSONL")
    ap.add_argument("--exclude-sessions", default=None,
                    help="file of session ids (one per line, prefixes ok) to leave out, e.g. "
                         "prefix_repro harness sessions run during the window")
    args = ap.parse_args(argv)
    excluded = []
    if args.exclude_sessions:
        excluded = [x.strip() for x in open(args.exclude_sessions) if x.strip()]
    since, until = ts(args.since), ts(args.until) or 1e12

    agg = collections.defaultdict(lambda: [0, 0, 0, 0])
    pairs = collections.defaultdict(lambda: [0, 0, 0, 0, 0])  # eligible, breaks, rebilled, short_elig, short_breaks
    last: dict = {}
    out = open(args.breaks_out, "w") if args.breaks_out else None
    with open(args.log) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("ts", 0)
            if not (since <= t < until):
                continue
            sid0 = d.get("session_id") or ""
            if excluded and any(sid0.startswith(x) for x in excluded):
                continue
            route = d.get("route") or "unknown"
            r, c, i = d.get("cache_read", 0), d.get("cache_creation", 0), d.get("input_tokens", 0)
            a = agg[route]
            a[0] += r; a[1] += c; a[2] += i; a[3] += 1
            sid = d.get("session_id")
            if not sid or sid == "nosession":
                continue
            key = (sid, d.get("model"))
            tot = r + c + i
            if key in last:
                pts, ptot, preq = last[key]
                gap = t - pts
                if gap > args.gap:
                    last[key] = (t, tot, d.get("req_id"))
                    continue
                if tot < 0.8 * ptot:
                    continue  # side call
                if ptot >= 10000:
                    p = pairs[route]
                    p[0] += 1
                    short = gap <= 30
                    if short:
                        p[3] += 1
                    if r < 0.5 * ptot:
                        p[1] += 1
                        p[2] += ptot - r
                        if short:
                            p[4] += 1
                        if out:
                            out.write(json.dumps({"route": route, "model": d.get("model"), "session_id": sid,
                                                  "req_id": d.get("req_id"), "prev_req_id": preq,
                                                  "gap_s": round(gap, 1), "prev_total": ptot,
                                                  "cache_read": r}) + "\n")
            last[key] = (t, tot, d.get("req_id"))
    for route in sorted(agg):
        r, c, i, n = agg[route]
        tot = r + c + i
        e, b, rb, se, sb = pairs.get(route, [0, 0, 0, 0, 0])
        print(f"{route}: requests={n} share={r / tot if tot else 0:.4f} input={tot / 1e6:.1f}M "
              f"warm_pairs={e} breaks={b} rate={b / e if e else 0:.4f} rebilled={rb / 1e6:.2f}M "
              f"short_gap_pairs={se} short_gap_breaks={sb} short_rate={sb / se if se else 0:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
