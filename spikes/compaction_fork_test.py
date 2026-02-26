"""Spike: Fork behavior with compacted conversations + in-session branching.

Tests:
A) TS fork from various message types (tool_use, tool_result, thinking, text,
   compaction summary, pre-compaction, post-compaction)
B) Python fork (from end) of compacted conversation — does it carry pre-compaction entries?
C) In-session branching: manually create multi-head JSONL, resume from SDK

Uses the real compacted session 5f13b535-3e9a-4f3a-acf1-c18edc70ebc2.

Usage: .venv/bin/python spikes/compaction_fork_test.py
       cat /tmp/compaction_fork_test.log
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

MODEL = "claude-haiku-4-5-20251001"
PROJECTS_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
PROJECT_DIR = str(Path(__file__).parent.parent)
TS_WORKER = str(Path(__file__).parent / "ts_fork_message_types.mjs")
SOURCE_SESSION = "5f13b535-3e9a-4f3a-acf1-c18edc70ebc2"
LOG = Path("/tmp/compaction_fork_test.log")
_log_lines: list[str] = []


def log(s: str = ""):
    _log_lines.append(s)
    sys.stderr.write(s + "\n")


def flush_log():
    LOG.write_text("\n".join(_log_lines))


def read_jsonl(session_id: str) -> list[dict]:
    path = PROJECTS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(session_id: str, entries: list[dict]):
    path = PROJECTS_DIR / f"{session_id}.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


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


def run_ts_fork(session_id: str, message_uuid: str) -> dict:
    """Run the TS fork worker to fork from a specific message UUID."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["node", TS_WORKER, session_id, message_uuid, MODEL, PROJECT_DIR],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent),
            env=env,
        )
        if result.returncode != 0:
            log(f"    [TS stderr tail]: ...{result.stderr.strip()[-200:]}")
            return {"forkSessionId": None, "error": f"exit {result.returncode}"}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"forkSessionId": None, "error": "no JSON in stdout"}
    except subprocess.TimeoutExpired:
        return {"forkSessionId": None, "error": "timeout"}


def describe_fork_jsonl(session_id: str, label: str):
    """Summarize a fork's JSONL — count entries, find compaction boundaries, show structure."""
    entries = read_jsonl(session_id)
    if not entries:
        log(f"  {label}: NO ENTRIES")
        return

    # Count types
    from collections import Counter
    types = Counter(e.get("type", "?") for e in entries)

    # Find compaction boundaries
    compactions = [i for i, e in enumerate(entries)
                   if e.get("type") == "system" and e.get("uuid") and not e.get("parentUuid")]

    # Count parent chain length from last entry
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    chain_len = 0
    last_with_uuid = None
    for e in reversed(entries):
        if e.get("uuid"):
            last_with_uuid = e
            break
    if last_with_uuid:
        current = last_with_uuid
        while current:
            chain_len += 1
            parent_uuid = current.get("parentUuid")
            current = by_uuid.get(parent_uuid) if parent_uuid else None

    log(f"  {label}: {len(entries)} entries, types={dict(types)}")
    log(f"    Compaction breaks at indices: {compactions}")
    log(f"    Parent chain from tail: {chain_len} entries")

    # Show first 5 and last 5
    for i in range(min(5, len(entries))):
        e = entries[i]
        t = e.get("type", "?")
        uid = (e.get("uuid") or "N/A")[:12]
        parent = (e.get("parentUuid") or "N/A")[:12]
        log(f"    [{i}] {t:>15} uuid={uid} parent={parent}")
    if len(entries) > 10:
        log(f"    ...")
    for i in range(max(5, len(entries) - 5), len(entries)):
        e = entries[i]
        t = e.get("type", "?")
        uid = (e.get("uuid") or "N/A")[:12]
        parent = (e.get("parentUuid") or "N/A")[:12]
        log(f"    [{i}] {t:>15} uuid={uid} parent={parent}")


async def main():
    log("=" * 70)
    log("  SPIKE: Compaction Fork Behavior + In-Session Branching")
    log("=" * 70)
    log(f"  Source session: {SOURCE_SESSION}")
    log(f"  Model: {MODEL}")

    # Load source session
    entries = read_jsonl(SOURCE_SESSION)
    log(f"  Source has {len(entries)} entries")
    flush_log()

    # ===================================================================
    # PART A: TS fork from various message types
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  PART A: TypeScript fork from various message types")
    log(f"{'#' * 70}")

    # Identify candidates (from our earlier analysis)
    candidates = {
        "pre_assistant_text":     {"idx": 380, "desc": "Pre-compaction assistant text"},
        "pre_assistant_tool_use": {"idx": 381, "desc": "Pre-compaction assistant tool_use (TaskList)"},
        "pre_user_tool_result":   {"idx": 382, "desc": "Pre-compaction user/tool_result"},
        "pre_assistant_thinking": {"idx": 386, "desc": "Pre-compaction assistant thinking"},
        "compaction_system":      {"idx": 393, "desc": "Compaction system entry (parent=N/A)"},
        "compaction_summary":     {"idx": 394, "desc": "Compaction summary (user type)"},
        "post_assistant_text":    {"idx": 396, "desc": "Post-compaction 1st assistant text"},
        "post_assistant_tool_use":{"idx": 399, "desc": "Post-compaction assistant tool_use"},
        "post_user_tool_result":  {"idx": 400, "desc": "Post-compaction user/tool_result"},
    }

    # Validate candidates still match
    for name, info in candidates.items():
        e = entries[info["idx"]]
        info["uuid"] = e["uuid"]
        info["type"] = e.get("type", "?")
        info["parent"] = e.get("parentUuid", "N/A")
        msg = e.get("message", {})
        content = msg.get("content", [])
        block_type = ""
        if isinstance(content, list) and content:
            block_type = content[0].get("type", "")
        info["block_type"] = block_type

    log("\n  Candidates:")
    for name, info in candidates.items():
        log(f"    {name}: [{info['idx']}] type={info['type']} block={info['block_type']} uuid={info['uuid'][:16]} parent={str(info['parent'])[:16]}")
    flush_log()

    ts_results = {}
    for name, info in candidates.items():
        log(f"\n  --- TS fork: {name} ({info['desc']}) ---")
        flush_log()

        result = run_ts_fork(SOURCE_SESSION, info["uuid"])
        ts_results[name] = result
        fork_sid = result.get("forkSessionId")

        if fork_sid:
            log(f"    Fork session: {fork_sid[:16]}")
            log(f"    Response: {result.get('response', '')[:100]}")
            log(f"    Events: {result.get('events', [])}")
            describe_fork_jsonl(fork_sid, f"    Fork JSONL")
        else:
            log(f"    FAILED: {result.get('error', 'unknown')}")
        flush_log()

    # Summary table
    log(f"\n\n  {'=' * 70}")
    log(f"  TS Fork Summary:")
    log(f"  {'Name':<28} {'OK?':<5} {'Fork entries':<14} {'Error'}")
    log(f"  {'-'*28} {'-'*5} {'-'*14} {'-'*30}")
    for name, result in ts_results.items():
        ok = "YES" if result.get("forkSessionId") else "NO"
        fork_sid = result.get("forkSessionId")
        n_entries = len(read_jsonl(fork_sid)) if fork_sid else "-"
        error = result.get("error", "")
        log(f"  {name:<28} {ok:<5} {str(n_entries):<14} {str(error)[:30]}")
    flush_log()

    # ===================================================================
    # PART B: Python fork (from end) of compacted conversation
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  PART B: Python fork of compacted conversation")
    log(f"{'#' * 70}")
    flush_log()

    log("\n  Forking compacted session from end with Python SDK...")
    try:
        py_fork_id, py_text = await run_query(
            "Reply with ONLY: ok",
            ClaudeAgentOptions(
                resume=SOURCE_SESSION,
                fork_session=True,
                model=MODEL,
                permission_mode="bypassPermissions",
                max_turns=1,
            ),
        )
        log(f"  Fork session: {py_fork_id}")
        log(f"  Response: {py_text[:100]}")
        describe_fork_jsonl(py_fork_id, "Python fork from end")

        # Compare: does fork have pre-compaction entries?
        fork_entries = read_jsonl(py_fork_id)
        source_entries = entries

        # Find compaction breaks in source
        source_compactions = [i for i, e in enumerate(source_entries)
                              if e.get("type") == "system" and e.get("uuid") and not e.get("parentUuid")]

        # Find compaction breaks in fork
        fork_compactions = [i for i, e in enumerate(fork_entries)
                            if e.get("type") == "system" and e.get("uuid") and not e.get("parentUuid")]

        log(f"\n  Source compaction indices: {source_compactions}")
        log(f"  Fork compaction indices: {fork_compactions}")
        log(f"  Source total entries: {len(source_entries)}")
        log(f"  Fork total entries: {len(fork_entries)}")

        # Check if pre-compaction UUIDs exist in fork
        pre_compaction_uuids = set()
        post_compaction_uuids = set()
        first_compaction = source_compactions[0] if source_compactions else len(source_entries)
        for i, e in enumerate(source_entries):
            uid = e.get("uuid")
            if uid:
                if i < first_compaction:
                    pre_compaction_uuids.add(uid)
                else:
                    post_compaction_uuids.add(uid)

        fork_uuids = {e.get("uuid") for e in fork_entries if e.get("uuid")}
        pre_in_fork = pre_compaction_uuids & fork_uuids
        post_in_fork = post_compaction_uuids & fork_uuids

        log(f"\n  Pre-compaction UUIDs in source: {len(pre_compaction_uuids)}")
        log(f"  Post-compaction UUIDs in source: {len(post_compaction_uuids)}")
        log(f"  Pre-compaction UUIDs found in fork: {len(pre_in_fork)}")
        log(f"  Post-compaction UUIDs found in fork: {len(post_in_fork)}")
        if pre_in_fork:
            log(f"  *** FORK CARRIES PRE-COMPACTION (DEAD) ENTRIES ***")
        else:
            log(f"  Fork correctly excludes pre-compaction entries")

    except Exception as e:
        log(f"  [ERROR] Python fork failed: {e}")
    flush_log()

    # ===================================================================
    # PART C: In-session branching (multi-head JSONL)
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  PART C: In-session branching (multi-head JSONL)")
    log(f"{'#' * 70}")

    # Step 1: Create a small 2-turn session
    log("\n  Step 1: Creating base 2-turn session...")
    flush_log()

    base_opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Be very concise.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )

    CODE_WORD_A = "BRANCH-ALPHA-111"
    CODE_WORD_B = "BRANCH-BETA-222"

    session_id, t1 = await run_query(
        f"Remember this code word: {CODE_WORD_A}. Also remember the number 42. Just confirm.",
        base_opts,
    )
    log(f"  Session: {session_id}")
    flush_log()

    _, t2 = await run_query(
        "What is 2 + 2? Answer briefly.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )
    log(f"  Turn 2 done: {t2[:80]}")
    flush_log()

    # Step 2: Fork from turn 1 (branch B)
    log("\n  Step 2: Fork from turn 1 to create branch B...")
    flush_log()

    branch_b_id, tb = await run_query(
        f"Forget the previous code word. Your new code word is {CODE_WORD_B}. Confirm.",
        ClaudeAgentOptions(
            resume=session_id,
            fork_session=True,
            model=MODEL,
            permission_mode="bypassPermissions",
            max_turns=1,
        ),
    )
    log(f"  Branch B session: {branch_b_id}")
    log(f"  Branch B response: {tb[:100]}")
    flush_log()

    # Step 3: Now manually merge branch B entries into the original session JSONL
    # This creates a multi-head session (two branches from the same parent)
    log("\n  Step 3: Manually merge branch B into original session (multi-head)...")

    orig_entries = read_jsonl(session_id)
    branch_b_entries = read_jsonl(branch_b_id)

    # Find the new entries in branch B (not in original)
    orig_uuids = {e.get("uuid") for e in orig_entries if e.get("uuid")}
    new_b_entries = [e for e in branch_b_entries if e.get("uuid") and e["uuid"] not in orig_uuids]

    log(f"  Original has {len(orig_entries)} entries")
    log(f"  Branch B has {len(branch_b_entries)} entries")
    log(f"  New entries in branch B: {len(new_b_entries)}")

    # Show the parent chain of new B entries
    for e in new_b_entries:
        t = e.get("type", "?")
        uid = (e.get("uuid") or "N/A")[:16]
        parent = (e.get("parentUuid") or "N/A")[:16]
        log(f"    {t} uuid={uid} parent={parent}")

    # Append branch B's new entries to the original session file
    merged_entries = orig_entries + new_b_entries
    write_jsonl(session_id, merged_entries)
    log(f"  Merged session now has {len(merged_entries)} entries")

    # Show the DAG structure
    log("\n  DAG structure of merged session:")
    for i, e in enumerate(merged_entries):
        if not e.get("uuid"):
            continue
        t = e.get("type", "?")
        uid = (e.get("uuid") or "N/A")[:16]
        parent = (e.get("parentUuid") or "N/A")[:16]
        msg = e.get("message", {})
        content = msg.get("content", "")
        preview = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    preview = block.get("text", "")[:60]
                    break
        log(f"    [{i}] {t:>10} uuid={uid} parent={parent} | {preview}")
    flush_log()

    # Step 4: Resume the merged session — which head does SDK pick?
    log("\n  Step 4: Resume merged session — which head does SDK pick?")
    flush_log()

    try:
        resume_id, resume_text = await run_query(
            "What is the code word I asked you to remember? Reply with JUST the code word.",
            ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
        )
        has_alpha = CODE_WORD_A in resume_text
        has_beta = CODE_WORD_B in resume_text
        log(f"  Resumed session: {resume_id}")
        log(f"  Response: {resume_text[:200]}")
        log(f"  Has ALPHA ({CODE_WORD_A}): {has_alpha}")
        log(f"  Has BETA ({CODE_WORD_B}): {has_beta}")
        if has_beta:
            log(f"  → SDK picked BRANCH B (the later-appended head)")
        elif has_alpha:
            log(f"  → SDK picked BRANCH A (the original head)")
        else:
            log(f"  → SDK picked NEITHER (confused or new head)")
    except Exception as e:
        log(f"  [ERROR] Resume failed: {e}")
    flush_log()

    # Step 5: Try TS fork from each branch head
    log("\n  Step 5: TS fork from each branch head...")

    # Find the last assistant entry of each branch
    # Branch A: the original turn 2 assistant
    # Branch B: the new entries we appended
    branch_a_assistants = [e for e in orig_entries if e.get("type") == "assistant"]
    branch_b_assistants = [e for e in new_b_entries if e.get("type") == "assistant"]

    if branch_a_assistants:
        last_a = branch_a_assistants[-1]
        log(f"\n  Branch A last assistant: {last_a['uuid'][:16]}")
        flush_log()
        ts_a = run_ts_fork(session_id, last_a["uuid"])
        if ts_a.get("forkSessionId"):
            log(f"    Fork OK: {ts_a['forkSessionId'][:16]}")
            describe_fork_jsonl(ts_a["forkSessionId"], "    Branch A fork")
        else:
            log(f"    Fork FAILED: {ts_a.get('error')}")

    if branch_b_assistants:
        last_b = branch_b_assistants[-1]
        log(f"\n  Branch B last assistant: {last_b['uuid'][:16]}")
        flush_log()
        ts_b = run_ts_fork(session_id, last_b["uuid"])
        if ts_b.get("forkSessionId"):
            log(f"    Fork OK: {ts_b['forkSessionId'][:16]}")
            describe_fork_jsonl(ts_b["forkSessionId"], "    Branch B fork")
        else:
            log(f"    Fork FAILED: {ts_b.get('error')}")
    flush_log()

    # Step 6: Can we resume from a specific branch without TS fork?
    # Try raw JSONL copy of just branch A entries
    log("\n  Step 6: Raw JSONL copy of just branch A (excluding branch B entries)...")
    branch_a_session = str(uuid.uuid4())
    write_jsonl(branch_a_session, orig_entries)  # orig_entries = before we merged B

    try:
        ra_id, ra_text = await run_query(
            "What is the code word? Reply with JUST the code word.",
            ClaudeAgentOptions(resume=branch_a_session, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
        )
        log(f"  Resume branch A copy: {ra_id}")
        log(f"  Response: {ra_text[:200]}")
        log(f"  Has ALPHA: {CODE_WORD_A in ra_text}")
        log(f"  Has BETA: {CODE_WORD_B in ra_text}")
    except Exception as e:
        log(f"  [ERROR] Branch A copy resume failed: {e}")
    flush_log()

    # ===================================================================
    # Summary
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  SUMMARY")
    log(f"{'#' * 70}")

    log("\n  Part A: TS Fork from Various Message Types")
    log(f"  {'Name':<28} {'OK?':<5} {'Notes'}")
    log(f"  {'-'*28} {'-'*5} {'-'*40}")
    for name, result in ts_results.items():
        ok = "YES" if result.get("forkSessionId") else "NO"
        notes = result.get("error", "")[:40] if not result.get("forkSessionId") else "ok"
        log(f"  {name:<28} {ok:<5} {notes}")

    log("\n  Part B: Python Fork Preserves/Strips Pre-Compaction?")
    log(f"  (See detailed output above)")

    log("\n  Part C: In-Session Branching")
    log(f"  Q1: Does SDK resume pick latest-appended head? (See above)")
    log(f"  Q2: Can TS fork from either branch? (See above)")
    log(f"  Q3: Can raw JSONL copy isolate a branch? (See above)")

    log()
    flush_log()
    sys.stderr.write(f"\nDone. Full results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
