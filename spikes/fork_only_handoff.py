"""Spike: Can TypeScript create a fork-only session (no message) that Python resumes?

The question: TypeScript SDK has `resumeSessionAt` for message-level forking.
If TS forks from turn 3 of a 5-turn Python conversation but sends NO message,
does a usable session ID get created that Python can immediately resume?

Or does TS need to complete at least one turn for the fork to be "codified"?

Flow:
1. Python creates 5-turn session
2. TS forks from turn 3 (assistant response) in three modes:
   a. maxTurns: 0 (no turns at all)
   b. maxTurns: 1 but abort after first stream event
   c. maxTurns: 1 normal (control — known to work)
3. For each mode that produces a session ID, Python resumes it for turn 4
4. Reports: which modes work, context preservation, cache stats

Usage: .venv/bin/python spikes/fork_only_handoff.py
       cat /tmp/fork_only_handoff.log
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

sys.path.insert(0, str(Path(__file__).parent))
from cache_utils import extract_cache_stats, extract_message_uuids, print_cache_report

MODEL = "claude-haiku-4-5-20251001"
TS_WORKER = str(Path(__file__).parent / "ts_fork_only_worker.mjs")
PROJECT_DIR = str(Path(__file__).parent.parent)
LOG = Path("/tmp/fork_only_handoff.log")
_log_lines: list[str] = []

# Code word used to verify context survival across fork boundary
CODE_WORD = "PINEAPPLE-VOLCANO-7742"


def log(s: str = ""):
    _log_lines.append(s)
    sys.stderr.write(s + "\n")


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


def run_ts_fork(session_id: str, message_uuid: str, mode: str) -> dict:
    """Run the TypeScript fork-only worker in the specified mode."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["node", TS_WORKER, session_id, message_uuid, mode, MODEL, PROJECT_DIR],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent),
            env=env,
        )
        log(f"  [TS stderr tail]: ...{result.stderr.strip()[-300:]}")
        if result.returncode != 0:
            log(f"  [TS ERROR] Exit code {result.returncode}")
            return {"forkSessionId": None, "error": f"exit {result.returncode}", "mode": mode}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"forkSessionId": None, "error": "no JSON in stdout", "mode": mode}
    except subprocess.TimeoutExpired:
        return {"forkSessionId": None, "error": "timeout", "mode": mode}


async def main():
    log("=" * 70)
    log("  SPIKE: Fork-Only Handoff (TS forks, Python continues)")
    log("=" * 70)
    log(f"  Code word: {CODE_WORD}")
    log(f"  Model: {MODEL}")
    log()

    # ===================================================================
    # Phase 1: Build a 5-turn Python session
    # ===================================================================
    base_opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Be concise. When asked to remember something, confirm you will remember it.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )

    # Turn 1: plant the code word
    log("[1/5] Turn 1 — planting code word...")
    flush_log()
    session_id, t1 = await run_query(
        f"Remember this code word for later: {CODE_WORD}. Just confirm you've noted it.",
        base_opts,
    )
    log(f"  Session: {session_id}")
    log(f"  Response: {t1[:100]}")
    flush_log()

    # Turn 2
    log("\n[2/5] Turn 2...")
    flush_log()
    _, t2 = await run_query(
        "Tell me about the history of programming languages. Cover at least 5 major languages. 500+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )
    log(f"  Response: {len(t2)} chars")
    flush_log()

    # Turn 3: this is the fork point
    log("\n[3/5] Turn 3 (FORK POINT — TS will branch after this)...")
    flush_log()
    _, t3 = await run_query(
        "Now compare compiled vs interpreted languages. Discuss tradeoffs. 500+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )
    log(f"  Response: {len(t3)} chars")
    flush_log()

    # Turn 4
    log("\n[4/5] Turn 4...")
    flush_log()
    _, t4 = await run_query(
        "Discuss the rise of functional programming and its influence on modern languages. 300+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )
    log(f"  Response: {len(t4)} chars")
    flush_log()

    # Turn 5
    log("\n[5/5] Turn 5...")
    flush_log()
    _, t5 = await run_query(
        "What do you think the future of programming languages looks like? 300+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )
    log(f"  Response: {len(t5)} chars")
    flush_log()

    # ===================================================================
    # Phase 2: Find the fork point (end of turn 3 assistant response)
    # ===================================================================
    log("\n" + "=" * 70)
    log("  Finding fork point (end of turn 3)...")
    log("=" * 70)

    messages = extract_message_uuids(session_id)
    log(f"  Total messages: {len(messages)}")
    for m in messages:
        log(f"    {m['type']:>9} {m['uuid'][:12]}... {m['text_preview'][:50]}")

    # Find the 3rd assistant message (response to turn 3)
    assistant_msgs = [m for m in messages if m["type"] == "assistant"]
    if len(assistant_msgs) < 3:
        log(f"  [!] Only {len(assistant_msgs)} assistant messages. Need at least 3. Aborting.")
        flush_log()
        return

    fork_point = assistant_msgs[2]  # 0-indexed: 3rd assistant = response to turn 3
    log(f"\n  Fork point (turn 3 assistant): {fork_point['uuid']}")
    log(f"  Preview: {fork_point['text_preview'][:60]}")
    flush_log()

    # ===================================================================
    # Phase 3: TypeScript fork attempts (3 modes)
    # ===================================================================
    modes = ["zero", "abort", "control"]
    ts_results = {}

    for mode in modes:
        log(f"\n{'=' * 70}")
        log(f"  TS Fork — mode: {mode}")
        log(f"{'=' * 70}")
        flush_log()
        result = run_ts_fork(session_id, fork_point["uuid"], mode)
        ts_results[mode] = result
        log(f"  Session ID: {result.get('forkSessionId')}")
        log(f"  Events: {result.get('events', [])}")
        log(f"  Error: {result.get('error')}")
        log(f"  Response length: {len(result.get('response', ''))}")
        log(f"  New files: {result.get('newFiles', [])}")
        flush_log()

    # ===================================================================
    # Phase 4: Python resumes each successful TS fork
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  PYTHON RESUME ATTEMPTS")
    log(f"{'#' * 70}")

    resume_results = {}

    for mode in modes:
        fork_sid = ts_results[mode].get("forkSessionId")
        if not fork_sid:
            log(f"\n  [{mode}] No session ID — skipping Python resume")
            continue

        log(f"\n  [{mode}] Resuming TS fork session {fork_sid[:16]}...")
        flush_log()

        try:
            resume_opts = ClaudeAgentOptions(
                resume=fork_sid,
                model=MODEL,
                permission_mode="bypassPermissions",
                max_turns=1,
            )
            py_sid, py_text = await run_query(
                f"What is the code word I asked you to remember at the start? "
                f"Also, what topics have we discussed? List them in order.",
                resume_opts,
            )
            resume_results[mode] = {
                "session_id": py_sid,
                "text": py_text,
                "same_session": py_sid == fork_sid,
                "has_code_word": CODE_WORD in py_text,
                "success": True,
            }
            log(f"  Session: {py_sid}")
            log(f"  Same as fork? {py_sid == fork_sid}")
            log(f"  Code word found? {CODE_WORD in py_text}")
            log(f"  Response preview: {py_text[:300]}")
        except Exception as e:
            resume_results[mode] = {"success": False, "error": str(e)}
            log(f"  [ERROR] {e}")
        flush_log()

    # ===================================================================
    # Phase 5: Cache reports
    # ===================================================================
    import io, contextlib
    buf = io.StringIO()

    log(f"\n\n{'#' * 70}")
    log(f"  CACHE REPORTS")
    log(f"{'#' * 70}")

    orig_stats = extract_cache_stats(session_id)
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"Original Python session ({session_id[:12]}...)", orig_stats)
    log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    for mode in modes:
        fork_sid = ts_results[mode].get("forkSessionId")
        if not fork_sid:
            continue
        stats = extract_cache_stats(fork_sid)
        with contextlib.redirect_stdout(buf):
            print_cache_report(f"TS fork [{mode}] ({fork_sid[:12]}...)", stats)
        log(buf.getvalue()); buf.truncate(0); buf.seek(0)

    # ===================================================================
    # Phase 6: Summary
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  SUMMARY")
    log(f"{'#' * 70}")
    log(f"\n  {'Mode':<12} {'TS Session?':<14} {'Py Resume?':<12} {'Code Word?':<12} {'Context?':<10}")
    log(f"  {'-'*12} {'-'*14} {'-'*12} {'-'*12} {'-'*10}")

    for mode in modes:
        ts_sid = ts_results[mode].get("forkSessionId")
        has_sid = "YES" if ts_sid else "NO"
        py_ok = "N/A"
        code_ok = "N/A"
        ctx_ok = "N/A"
        if mode in resume_results:
            r = resume_results[mode]
            py_ok = "YES" if r.get("success") else "FAIL"
            code_ok = "YES" if r.get("has_code_word") else "NO"
            # Check if response mentions programming languages (turn 2-3 context)
            text = r.get("text", "").lower()
            ctx_ok = "YES" if ("compiled" in text or "interpreted" in text or "programming" in text) else "MAYBE"
        log(f"  {mode:<12} {has_sid:<14} {py_ok:<12} {code_ok:<12} {ctx_ok:<10}")

    log()
    log("KEY FINDINGS:")
    log("  Q1: Can TS create a fork session without sending a message (maxTurns=0)?")
    q1 = "YES" if ts_results["zero"].get("forkSessionId") else "NO"
    log(f"      → {q1}")

    log("  Q2: Can TS create a fork by aborting early (grab session ID, exit)?")
    q2 = "YES" if ts_results["abort"].get("forkSessionId") else "NO"
    log(f"      → {q2}")

    log("  Q3: Can Python resume a fork-only (no TS message) session?")
    zero_resume = resume_results.get("zero", {})
    q3 = "YES" if zero_resume.get("success") else ("NO" if "zero" in resume_results else "N/A (no session)")
    log(f"      → {q3}")

    log("  Q4: Does the fork preserve context up to the fork point (turns 1-3)?")
    for mode in modes:
        r = resume_results.get(mode, {})
        if r.get("success"):
            has_cw = r.get("has_code_word", False)
            text_lower = r.get("text", "").lower()
            has_ctx = "compiled" in text_lower or "interpreted" in text_lower or "programming" in text_lower
            log(f"      [{mode}] Code word: {has_cw}, Context: {has_ctx}")

    log("  Q5: Does the fork TRIM turns 4-5 (only keeps up to turn 3)?")
    for mode in modes:
        r = resume_results.get(mode, {})
        if r.get("success"):
            text_lower = r.get("text", "").lower()
            has_t4 = "functional" in text_lower  # turn 4 was about functional programming
            has_t5 = "future" in text_lower  # turn 5 was about the future
            log(f"      [{mode}] Turn 4 (functional) mentioned: {has_t4}, Turn 5 (future) mentioned: {has_t5}")

    log()
    flush_log()
    sys.stderr.write(f"\nDone. Full results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
