"""Spike: Is cross-SDK cache truly bidirectional for conversation tokens?

Uses a minimal system prompt ("Hi") to eliminate system prompt cache noise.
Tests whether TS fork hits Python's conversation cache and vice versa.

Usage: .venv/bin/python spikes/cache_direction_test.py
       cat /tmp/cache_direction_test.log
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

sys.path.insert(0, str(Path(__file__).parent))
from cache_utils import extract_cache_stats, extract_message_uuids

MODEL = "claude-haiku-4-5-20251001"
TS_WORKER = str(Path(__file__).parent / "ts_fork_worker.mjs")
PROJECT_DIR = str(Path(__file__).parent.parent)
LOG = Path("/tmp/cache_direction_test.log")
_log_lines: list[str] = []


def log(s: str = ""):
    _log_lines.append(s)


def flush_log():
    LOG.write_text("\n".join(_log_lines))


async def run_query(prompt: str, options: ClaudeAgentOptions) -> tuple[str | None, str]:
    session_id = None
    parts = []
    async for message in query(prompt=prompt, options=options):
        if hasattr(message, "session_id"):
            session_id = message.session_id
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return session_id, "\n".join(parts)


def run_ts_fork(session_id: str, message_uuid: str) -> tuple[str | None, str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["node", TS_WORKER, session_id, message_uuid, MODEL, PROJECT_DIR],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).parent), env=env,
    )
    log(f"  [TS stderr]: {result.stderr.strip()[-200:]}")
    if result.returncode != 0:
        log(f"  [TS ERROR] exit {result.returncode}")
        return None, ""
    for line in result.stdout.strip().splitlines():
        if line.strip().startswith("{"):
            try:
                data = json.loads(line.strip())
                return data.get("forkSessionId"), data.get("response", "")
            except json.JSONDecodeError:
                continue
    return None, ""


async def main():
    TINY_PROMPT = "Hi"  # Minimal system prompt to eliminate noise

    log("=" * 70)
    log("  SPIKE: Cache Direction Test (minimal system prompt)")
    log("=" * 70)
    log(f"  System prompt: '{TINY_PROMPT}' (should be ~1 token)")

    # --- Turn 1: build conversation context ---
    log("\n[1] Python turn 1 — long response...")
    flush_log()
    opts = ClaudeAgentOptions(
        system_prompt=TINY_PROMPT, model=MODEL,
        permission_mode="bypassPermissions", max_turns=1,
    )
    session_id, text1 = await run_query(
        "Give me a detailed history of computing from Babbage to modern GPUs. "
        "At least 2000 words. Be very thorough.",
        opts,
    )
    log(f"  Session: {session_id}")
    log(f"  Response: {len(text1)} chars")
    flush_log()

    # --- Turn 2: more context ---
    log("\n[2] Python turn 2 (resume)...")
    flush_log()
    opts2 = ClaudeAgentOptions(
        resume=session_id, model=MODEL,
        permission_mode="bypassPermissions", max_turns=1,
    )
    _, text2 = await run_query(
        "Now discuss Moore's Law and its implications. 500+ words.", opts2,
    )
    log(f"  Response: {len(text2)} chars")
    flush_log()

    # --- Find fork point ---
    messages = extract_message_uuids(session_id)
    user_msgs = [m for m in messages if m["type"] == "user"]
    second_user = user_msgs[1]["uuid"]
    turn1_last_asst = None
    for m in messages:
        if m["uuid"] == second_user:
            break
        if m["type"] == "assistant":
            turn1_last_asst = m
    fork_uuid = turn1_last_asst["uuid"]
    log(f"\n  Fork point: {fork_uuid}")

    # --- Python fork from end (control A) ---
    log("\n[3] Python fork from END...")
    flush_log()
    py_fork_id, _ = await run_query(
        "What is the most important point? 2 sentences.",
        ClaudeAgentOptions(
            resume=session_id, fork_session=True, model=MODEL,
            permission_mode="bypassPermissions", max_turns=1,
        ),
    )
    log(f"  Py fork: {py_fork_id}")
    flush_log()

    # --- TS fork from turn 1 message ---
    log("\n[4] TS fork from turn 1 message...")
    flush_log()
    ts_fork_id, _ = run_ts_fork(session_id, fork_uuid)
    log(f"  TS fork: {ts_fork_id}")
    flush_log()

    # --- Python resume of TS fork ---
    log("\n[5] Python resumes TS fork...")
    flush_log()
    py_resume_id, _ = await run_query(
        "Summarize what we discussed.",
        ClaudeAgentOptions(
            resume=ts_fork_id, model=MODEL,
            permission_mode="bypassPermissions", max_turns=1,
        ),
    )
    log(f"  Py resume of TS fork: {py_resume_id} (same={py_resume_id == ts_fork_id})")
    flush_log()

    # --- TS fork from Python turn 2 (fork from END, not mid) ---
    # This tests: does TS hit cache when forking from the very latest message?
    all_assts = [m for m in messages if m["type"] == "assistant"]
    last_asst = all_assts[-1]
    log(f"\n[6] TS fork from LAST message (end of turn 2)...")
    flush_log()
    ts_end_fork_id, _ = run_ts_fork(session_id, last_asst["uuid"])
    log(f"  TS end fork: {ts_end_fork_id}")
    flush_log()

    # --- Extract stats ---
    orig = extract_cache_stats(session_id)
    py_fork = extract_cache_stats(py_fork_id) if py_fork_id else []
    ts_fork = extract_cache_stats(ts_fork_id) if ts_fork_id else []
    ts_resume = extract_cache_stats(ts_fork_id) if ts_fork_id else []  # same session, more entries after Py resume
    ts_end = extract_cache_stats(ts_end_fork_id) if ts_end_fork_id else []

    # Find NEW entries (not inherited from parent)
    orig_uuids = {s["uuid"] for s in orig}
    ts_uuids = {s["uuid"] for s in ts_fork}

    log(f"\n\n{'=' * 70}")
    log(f"  RESULTS (system prompt = '{TINY_PROMPT}')")
    log(f"{'=' * 70}")
    log(f"  {'Scenario':<40} {'CacheRd':>9} {'CacheCr':>9} {'Fresh':>7} {'Total':>9} {'Rate':>7}")
    log(f"  {'-'*40} {'-'*9} {'-'*9} {'-'*7} {'-'*9} {'-'*7}")

    for i, s in enumerate(orig):
        log(f"  {'Py orig turn ' + str(i+1):<40} {s['cache_read']:>9,} {s['cache_creation']:>9,} {s['input_tokens']:>7,} {s['total_input']:>9,} {s['cache_rate']:>6.1%}")

    if py_fork:
        s = py_fork[-1]
        log(f"  {'Py fork (end)':<40} {s['cache_read']:>9,} {s['cache_creation']:>9,} {s['input_tokens']:>7,} {s['total_input']:>9,} {s['cache_rate']:>6.1%}")

    if ts_fork:
        for s in ts_fork:
            tag = "(inherited)" if s["uuid"] in orig_uuids else "(NEW)"
            log(f"  {'TS fork msg ' + tag:<40} {s['cache_read']:>9,} {s['cache_creation']:>9,} {s['input_tokens']:>7,} {s['total_input']:>9,} {s['cache_rate']:>6.1%}")

    # Py resume of TS fork — entries beyond what TS fork had
    if ts_resume:
        for s in ts_resume:
            if s["uuid"] not in ts_uuids and s["uuid"] not in orig_uuids:
                log(f"  {'Py resume of TS fork':<40} {s['cache_read']:>9,} {s['cache_creation']:>9,} {s['input_tokens']:>7,} {s['total_input']:>9,} {s['cache_rate']:>6.1%}")

    if ts_end:
        for s in ts_end:
            if s["uuid"] not in orig_uuids:
                log(f"  {'TS fork end ' + ('(NEW)' if s['uuid'] not in orig_uuids else ''):<40} {s['cache_read']:>9,} {s['cache_creation']:>9,} {s['input_tokens']:>7,} {s['total_input']:>9,} {s['cache_rate']:>6.1%}")

    log()
    log("ANALYSIS:")
    log("  With minimal system prompt, cache_read on TS fork NEW entry")
    log("  approximates how many CONVERSATION tokens hit cache.")
    if ts_fork:
        new_ts = [s for s in ts_fork if s["uuid"] not in orig_uuids]
        if new_ts:
            s = new_ts[-1]
            log(f"  TS fork conversation cache_read: {s['cache_read']:,}")
            log(f"  TS fork total: {s['total_input']:,}")
            log(f"  If cache_read ≈ 0: TS gets NO conversation cache from Python")
            log(f"  If cache_read ≈ total: TS gets FULL conversation cache from Python")
    log()
    flush_log()
    sys.stderr.write(f"\nDone. Results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
