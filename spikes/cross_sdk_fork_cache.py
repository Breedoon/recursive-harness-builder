"""Spike: Cross-SDK fork — Python session, TypeScript message-level fork.

Uses query() for simplicity. Each query() spawns a CLI subprocess, but they
share the Anthropic API-level cache (content-hash based, ~5min TTL).
The key question: does TS resumeSessionAt from a mid-conversation message work?

1. Python creates a multi-turn session via query() with resume
2. Extracts a mid-conversation message UUID from the session JSONL
3. TypeScript forks from that specific message via resumeSessionAt
4. Python forks from the END of the same session (for comparison)
5. Compares cache behavior across all scenarios

Usage: .venv/bin/python spikes/cross_sdk_fork_cache.py
       cat /tmp/cross_sdk_fork_cache.log
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
LOG = Path("/tmp/cross_sdk_fork_cache.log")
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


PROJECT_DIR = str(Path(__file__).parent.parent)  # /Users/breedoon/Documents/obs


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
    log("  SPIKE: Cross-SDK Fork Cache (Python + TypeScript)")
    log("=" * 60)

    # --- Turn 1: long response ---
    log("\n[1] Turn 1 — long response to build context...")
    flush_log()
    opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Answer concisely but thoroughly.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    session_id, text1 = await run_query(
        "Give me a detailed history of the Internet from ARPANET to modern day. "
        "Cover key protocols, companies, and cultural shifts. At least 2000 words.",
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
        "Now explain how social media changed the Internet landscape. At least 500 words.",
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
        "Compare the early open Internet to today's walled gardens. 500+ words.",
        opts3,
    )
    log(f"  Response: {len(text3)} chars")
    flush_log()

    # --- Find mid-conversation message UUID ---
    log("\n[4] Extracting message UUIDs...")
    messages = extract_message_uuids(session_id)
    log(f"  Found {len(messages)} entries:")
    for m in messages:
        log(f"    {m['type']:>9} {m['uuid'][:12]}... {m['text_preview'][:60]}")

    user_msgs = [m for m in messages if m["type"] == "user"]
    if len(user_msgs) < 2:
        log("  [!] Not enough user messages. Aborting.")
        flush_log()
        return

    # Fork point: last assistant before second user message
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
    log(f"\n  Fork point: {fork_point['uuid']}")
    flush_log()

    # --- Python fork from END ---
    log("\n[5a] Python fork from END...")
    flush_log()
    py_fork_opts = ClaudeAgentOptions(
        resume=session_id, fork_session=True, model=MODEL,
        permission_mode="bypassPermissions", max_turns=1,
    )
    py_fork_id, py_fork_text = await run_query(
        "What is the single most important point from our conversation? 2-3 sentences.",
        py_fork_opts,
    )
    log(f"  Py fork session: {py_fork_id}")
    log(f"  Response: {len(py_fork_text)} chars")
    flush_log()

    # --- TypeScript fork from SPECIFIC MESSAGE ---
    log("\n[5b] TypeScript fork from mid-conversation message...")
    flush_log()
    ts_fork_id, ts_fork_text = run_ts_fork(session_id, fork_point["uuid"])
    log(f"  TS fork session: {ts_fork_id}")
    log(f"  Response: {len(ts_fork_text)} chars")
    flush_log()

    # --- Resume original (control) ---
    log("\n[6] Resume original (control)...")
    flush_log()
    opts6 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text6 = await run_query("Summarize everything in one sentence.", opts6)
    log(f"  Response: {len(text6)} chars")
    flush_log()

    # --- Reports ---
    import io, contextlib
    buf = io.StringIO()

    log("\n\n" + "#" * 60)
    log("  CACHE REPORTS")
    log("#" * 60)

    orig_stats = extract_cache_stats(session_id)
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"Original session ({session_id[:12]}...)", orig_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    py_fork_stats = []
    if py_fork_id:
        py_fork_stats = extract_cache_stats(py_fork_id)
        with contextlib.redirect_stdout(buf):
            print_cache_report(f"Py fork END ({py_fork_id[:12]}...)", py_fork_stats)
        log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    ts_fork_stats = []
    if ts_fork_id:
        ts_fork_stats = extract_cache_stats(ts_fork_id)
        with contextlib.redirect_stdout(buf):
            print_cache_report(f"TS fork MSG ({ts_fork_id[:12]}...)", ts_fork_stats)
        log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    # --- Comparison table ---
    log(f"\n{'=' * 70}")
    log(f"  COMPARISON")
    log(f"{'=' * 70}")
    log(f"  {'Scenario':<30} {'CacheRead':>10} {'CacheCreate':>11} {'Fresh':>8} {'Total':>10} {'Rate':>7}")
    log(f"  {'-'*30} {'-'*10} {'-'*11} {'-'*8} {'-'*10} {'-'*7}")

    for i, s in enumerate(orig_stats):
        label = f"Original turn {i + 1}"
        log(f"  {label:<30} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>8,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    if py_fork_stats:
        s = py_fork_stats[-1]
        log(f"  {'Py fork (end)':<30} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>8,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    if ts_fork_stats:
        s = ts_fork_stats[-1]
        log(f"  {'TS fork (msg)':<30} {s['cache_read']:>10,} {s['cache_creation']:>11,} {s['input_tokens']:>8,} {s['total_input']:>10,} {s['cache_rate']:>6.1%}")

    log()
    log("KEY QUESTIONS:")
    log("  1. Does TS fork work at all? (non-empty response)")
    log("  2. Does TS fork have LOWER total than Py fork? (message-level truncation)")
    log("  3. Is cache_read similar between Py fork and TS fork? (API cache sharing)")
    log()
    flush_log()
    sys.stderr.write(f"\nDone. Results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
