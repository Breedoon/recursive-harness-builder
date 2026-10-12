#!/usr/bin/env python3
"""Check that CLIProxyAPI still gives each GPT conversation its own cache key.

CACHE-PREFIX WARNING: run this after ANY CLIProxyAPI upgrade or config change.
The cache proxy's assign_conversation_cache_key() (src/cache_proxy.py) relies
on one observed CLIProxyAPI behaviour: the Codex ``prompt_cache_key`` it sends
upstream is derived from the session part of ``metadata.user_id``. If an
upgrade changes that, the proxy fix silently stops working and the whole GPT
fleet may again share one cache key (the state before 2026-10-12, when many
byte-identical GPT prefixes got no cache hit -- the exact provider-side cause
is unproven).

How: sends five tiny requests DIRECTLY to CLIProxyAPI (bypassing the cache
proxy, so the production request log is untouched) with different session
suffixes, then finds the prompt_cache_key each produced in CLIProxyAPI's own
request logs. Costs five 16-token GPT calls.

PASS when: same session -> same key; different sessions -> different keys;
neither equals the constant-session key.
Exit status: 0 PASS, 1 FAIL, 2 inconclusive (requests failed or keys not found
in the logs -- check that CLIProxyAPI request logging is still on).

Usage: .venv/bin/python scripts/cliproxy_cache_key_probe.py [--out result.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

BASE = os.environ.get("CLI_PROXY_BASE_URL", "http://127.0.0.1:8317")
LOGDIR = os.environ.get("CLI_PROXY_LOG_DIR", "/workspace/runtime/logs/cliproxy")
ACCOUNT = "user_" + "a" * 64 + "_account_"
VARIANTS = {
    "constant": ACCOUNT + "_session_0",
    "A": ACCOUNT + "_session_11111111-1111-4111-8111-111111111111",
    "B": ACCOUNT + "_session_22222222-2222-4222-8222-222222222222",
    "A_again": ACCOUNT + "_session_11111111-1111-4111-8111-111111111111",
}


def probe(model: str) -> dict:
    out = {}
    for name, uid in VARIANTS.items():
        marker = f"keyprobe-{name}-{int(time.time() * 1000)}"
        body = {"model": model, "max_tokens": 16, "stream": False,
                "metadata": {"user_id": uid},
                "messages": [{"role": "user", "content": [{"type": "text", "text": f"Reply OK. {marker}"}]}]}
        req = urllib.request.Request(BASE + "/v1/messages", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json",
                                              "x-api-key": os.environ.get("CLI_PROXY_API_KEY", "sk-anything"),
                                              "anthropic-version": "2023-06-01"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status = r.status
        except Exception as e:
            status = f"ERR {e}"
        time.sleep(2)
        key = None
        for f in sorted(glob.glob(os.path.join(LOGDIR, "v1-messages-*.log")), key=os.path.getmtime)[-60:]:
            if os.path.getmtime(f) < t0 - 5:
                continue
            txt = open(f, errors="replace").read()
            if marker in txt:
                m = re.search(r'"prompt_cache_key":"([^"]*)"', txt)
                key = m.group(1) if m else "ABSENT"
                break
        out[name] = {"status": status, "prompt_cache_key": key}
        print(f"{name:9s} status={status} prompt_cache_key={key}", flush=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="sol")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    try:
        from obs_agent.config import resolve_model
        model = resolve_model(args.model)
    except Exception:
        model = args.model
    res = probe(model)
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1))
    keys = {k: v["prompt_cache_key"] for k, v in res.items()}
    if any(v in (None, "ABSENT") for v in keys.values()):
        print("RESULT: INCONCLUSIVE -- a request failed or its key was not found in", LOGDIR)
        return 2
    ok = (keys["A"] == keys["A_again"] and keys["A"] != keys["B"]
          and keys["constant"] not in (keys["A"], keys["B"]))
    print("RESULT: PASS" if ok else
          "RESULT: FAIL -- CLIProxyAPI no longer derives the cache key from the session; "
          "assign_conversation_cache_key() needs a new mechanism (see docs/cache-proxy-safety.md)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
