"""Spike: Round-trip — TS fork from message, then Python resumes the TS fork.

Tests whether a fork created by TypeScript can be continued by Python,
and whether cache is preserved across the round-trip.

1. Reuses the existing Python session (c91bdd80) from the cross-SDK spike
2. TypeScript forks from turn 1 message
3. Python resumes the TS-created fork session
4. Python forks the TS-created fork session (double fork)
5. Compares cache across all scenarios

Usage: .venv/bin/python spikes/roundtrip_fork_cache.py
       cat /tmp/roundtrip_fork_cache.log
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

sys.path.insert(0, str(Path(__file__).parent))
from cache_utils import extract_cache_stats, extract_message_uuids, print_cache_report

MODEL = "claude-haiku-4-5-20251001"
TS_WORKER = str(Path(__file__).parent / "ts_fork_worker.mjs")
PROJECT_DIR = str(Path(__file__).parent.parent)
LOG = Path("/tmp/roundtrip_fork_cache.log")
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
    """Run the TypeScript fork worker as a subprocess."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["node", TS_WORKER, session_id, message_uuid, MODEL, PROJECT_DIR],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).parent),
        env=env,
    )
    log(f"  [TS stderr]: {result.stderr.strip()}")
    if result.returncode != 0:
        log(f"  [TS ERROR] Exit code {result.returncode}")
        log(f"  [TS stdout]: {result.stdout[:500]}")
        return None, ""
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                return data.get("forkSessionId"), data.get("response", "")
            except json.JSONDecodeError:
                continue
    log(f"  [TS ERROR] No JSON found in stdout: {result.stdout[:200]}")
    return None, ""


async def main():
    log("=" * 60)
    log("  SPIKE: Round-Trip Fork (Python → TS fork → Python resume)")
    log("=" * 60)

    # --- Step 1: Create a fresh Python session ---
    log("\n[1] Creating fresh Python session — turn 1...")
    flush_log()
    opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Answer concisely but thoroughly.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    session_id, text1 = await run_query(
        "Give me a detailed explanation of how compilers work, from lexing to code generation. "
        "Cover each phase in detail. At least 2000 words.",
        opts,
    )
    log(f"  Session: {session_id}")
    log(f"  Response: {len(text1)} chars")
    flush_log()

    # --- Turn 2 ---
    log("\n[2] Turn 2 (resume)...")
    flush_log()
    opts2 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text2 = await run_query(
        "Now explain how JIT compilation differs from AOT compilation. 500+ words.",
        opts2,
    )
    log(f"  Response: {len(text2)} chars")
    flush_log()

    # --- Turn 3 ---
    log("\n[3] Turn 3 (resume)...")
    flush_log()
    opts3 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text3 = await run_query(
        "Discuss LLVM's role in modern compiler infrastructure. 500+ words.",
        opts3,
    )
    log(f"  Response: {len(text3)} chars")
    flush_log()

    # --- Find fork point (end of turn 1) ---
    log("\n[4] Finding fork point...")
    messages = extract_message_uuids(session_id)
    user_msgs = [m for m in messages if m["type"] == "user"]
    if len(user_msgs) < 2:
        log("  [!] Not enough user messages. Aborting.")
        flush_log()
        return

    second_user_uuid = user_msgs[1]["uuid"]
    turn1_assistants = []
    for m in messages:
        if m["uuid"] == second_user_uuid:
            break
        if m["type"] == "assistant":
            turn1_assistants.append(m)

    if not turn1_assistants:
        log("  [!] No assistant in turn 1. Aborting.")
        flush_log()
        return

    fork_point = turn1_assistants[-1]
    log(f"  Fork point: {fork_point['uuid']}")
    flush_log()

    # --- Step 5: TypeScript forks from turn 1 ---
    log("\n[5] TypeScript fork from turn 1...")
    flush_log()
    ts_fork_id, ts_fork_text = run_ts_fork(session_id, fork_point["uuid"])
    log(f"  TS fork session: {ts_fork_id}")
    log(f"  Response: {len(ts_fork_text)} chars")
    flush_log()

    if not ts_fork_id:
        log("  [!] TS fork failed. Aborting.")
        flush_log()
        return

    # --- Step 6: Python RESUMES the TS-created fork ---
    log("\n[6] Python resumes the TS fork (no new fork, same session)...")
    flush_log()
    resume_opts = ClaudeAgentOptions(
        resume=ts_fork_id,
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    py_resume_id, py_resume_text = await run_query(
        "What have we discussed so far? List the topics in order.",
        resume_opts,
    )
    log(f"  Resumed session: {py_resume_id}")
    log(f"  Same as TS fork? {py_resume_id == ts_fork_id}")
    log(f"  Response: {len(py_resume_text)} chars")
    log(f"  Preview: {py_resume_text[:200]}")
    flush_log()

    # --- Step 7: Python FORKS the TS-created fork (double fork) ---
    log("\n[7] Python forks the TS fork (double fork)...")
    flush_log()
    double_fork_opts = ClaudeAgentOptions(
        resume=ts_fork_id,
        fork_session=True,
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    double_fork_id, double_fork_text = await run_query(
        "Give me a one-sentence summary of compiler optimization techniques.",
        double_fork_opts,
    )
    log(f"  Double fork session: {double_fork_id}")
    log(f"  Response: {len(double_fork_text)} chars")
    flush_log()

    # --- Reports ---
    import io, contextlib
    buf = io.StringIO()

    log("\n\n" + "#" * 60)
    log("  CACHE REPORTS")
    log("#" * 60)

    orig_stats = extract_cache_stats(session_id)
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"Original Python session ({session_id[:12]}...)", orig_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    ts_stats = extract_cache_stats(ts_fork_id) if ts_fork_id else []
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"TS fork from turn 1 ({(ts_fork_id or 'N/A')[:12]}...)", ts_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    # The resumed session is the SAME session as ts_fork_id, so its stats
    # include both the TS turn and the Python resume turn
    resume_stats = extract_cache_stats(ts_fork_id) if ts_fork_id else []
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"TS fork after Python resume ({(ts_fork_id or 'N/A')[:12]}...)", resume_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    double_stats = extract_cache_stats(double_fork_id) if double_fork_id else []
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"Double fork ({(double_fork_id or 'N/A')[:12]}...)", double_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    # --- Comparison ---
    log(f"\n{'=' * 75}")
    log(f"  COMPARISON")
    log(f"{'=' * 75}")
    log(f"  {'Scenario':<40} {'CacheRead':>10} {'CacheCreate':>11} {'Fresh':>7} {'Total':>10} {'Rate':>7}")
    log(f"  {'-'*40} {'-'*10} {'-'*11} {'-'*7} {'-'*10} {'-'*7}")

    for i, s in enumerate(orig_stats):
        log(f"  {'Orig turn ' + str(i+1):<40} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>7,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    # TS fork: show only new entries (not inherited from parent)
    orig_uuids = {s['uuid'] for s in orig_stats}
    if ts_stats:
        for i, s in enumerate(ts_stats):
            inherited = "(inherited)" if s['uuid'] in orig_uuids else "(NEW)"
            log(f"  {'TS fork entry ' + str(i+1) + ' ' + inherited:<40} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>7,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    # Python resume of TS fork: show entries beyond what TS fork had
    ts_uuids = {s['uuid'] for s in ts_stats} if ts_stats else set()
    if resume_stats:
        for i, s in enumerate(resume_stats):
            if s['uuid'] not in ts_uuids:
                log(f"  {'Py resume of TS fork':<40} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>7,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    if double_stats:
        ds_new = [s for s in double_stats if s['uuid'] not in ts_uuids and s['uuid'] not in orig_uuids]
        for s in ds_new:
            log(f"  {'Py double fork (TS→Py fork)':<40} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>7,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    log()
    log("KEY QUESTIONS:")
    log("  1. Does Python resume of TS fork work? (same session ID preserved)")
    log("  2. Does Python resume see the TS fork's context? (response references compilers + TS answer)")
    log("  3. Cache on Python resume of TS fork? (should be high if API cache works)")
    log("  4. Double fork works? (Python can fork a TS-created session)")
    log()
    flush_log()
    sys.stderr.write(f"\nDone. Results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
