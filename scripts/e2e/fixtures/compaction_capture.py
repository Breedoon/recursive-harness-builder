#!/usr/bin/env python3
"""Capture a real Anthropic-route auto-compaction from the pinned 2.1.59 (obs-22 E3, design-v2 §10.2).

The frozen request log has no Anthropic-route compaction. This drives the
pinned CLI, launched exactly as the fixture checker's ``harness`` profile
launches it (so the result is a replayable fixture), past a lowered
auto-compact threshold through a scratch cache proxy that logs requests:

  turns 1..N   Bash prints a block of numbers, until a compact_boundary appears
  2 more turns short replies after the compaction, so the session has a
               post-compaction main request to resume from

2.1.59 has no CLAUDE_CODE_AUTO_COMPACT_WINDOW; the threshold is lowered with
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (percent of the model's window). If no
auto-compaction happens, ``--manual`` sends /compact instead (same builder).

Real credential (the CLI's own config dir) and a real model call: use a
scratch proxy (own CACHE_PROXY_LOG_DIR, request log on), never 28925.
Output: <out>/capture.json with session id, cwd, model, the turn log and
the proxy request ids of the compaction request and the main requests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import obs_fixture_check as fc  # noqa: E402
from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage  # noqa: E402


def options(model: str, cwd: Path, base_url: str, pct: str, resume: str | None):
    meta = {"profile": "harness", "model": model, "route": "anthropic", "anthropic_beta": ""}
    o = fc.launch_options(meta, {}, cli_path=fc.PINNED, base_url=base_url,
                          config_dir=Path("/nonexistent"), cwd=str(cwd), resume=resume)
    # real credential: the CLI's own config dir, no dummy token
    o.env.pop("CLAUDE_CONFIG_DIR", None)
    o.env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    o.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = pct
    return o


async def turn(client, prompt: str, log: list) -> dict:
    await client.query(prompt)
    rec = {"prompt": prompt[:80], "compact_boundary": False, "result": None, "t0": time.time()}
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
            rec["compact_boundary"] = True
            rec["compact_metadata"] = msg.data.get("compact_metadata")
        if isinstance(msg, ResultMessage):
            rec["result"] = {"subtype": msg.subtype, "is_error": msg.is_error, "session_id": msg.session_id}
    rec["t1"] = time.time()
    log.append(rec)
    return rec


async def amain(a) -> int:
    out = Path(a.out)
    cwd = out / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    log: list = []
    sid = None
    compacted = False
    async with ClaudeSDKClient(options=options(a.model, cwd, a.base_url, a.pct, None)) as c:
        for i in range(a.max_fill):
            r = await turn(c, f"Run this exact Bash command and then reply with just the word done: "
                              f"seq {i * 100000 + 1} {i * 100000 + a.lines}", log)
            sid = (r["result"] or {}).get("session_id") or sid
            if r["compact_boundary"]:
                compacted = True
                break
        if not compacted and a.manual:
            r = await turn(c, "/compact", log)
            compacted = r["compact_boundary"]
        if compacted:
            for k in range(2):
                r = await turn(c, f"Post-compaction turn {k + 1}: reply with just the word ok.", log)
    summary = {"session_id": sid, "cwd": str(cwd), "model": a.model, "pct": a.pct,
               "compacted": compacted, "manual": bool(a.manual and not any(t["compact_boundary"] for t in log[:-1])),
               "turns": log, "log_dir": a.log_dir}
    if a.log_dir and sid:
        time.sleep(2)
        rows = [r for r in fc.pd.read_index(a.log_dir) if r.get("session_id") == sid]
        summary["requests"] = [{"req_id": r["req_id"], "ts": r["ts"], "path": r.get("path"),
                                "sizes": r.get("sizes"), "norm_action": (r.get("norm") or {}).get("action")}
                               for r in sorted(rows, key=lambda r: r["ts"])]
    (out / "capture.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: summary[k] for k in ("session_id", "compacted", "manual")}))
    return 0 if compacted else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--log-dir", required=True, help="the scratch proxy's requests/ dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--pct", default="15")
    ap.add_argument("--lines", type=int, default=3000)
    ap.add_argument("--max-fill", type=int, default=8)
    ap.add_argument("--manual", action="store_true")
    a = ap.parse_args()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    sys.exit(asyncio.run(amain(a)))


if __name__ == "__main__":
    main()
