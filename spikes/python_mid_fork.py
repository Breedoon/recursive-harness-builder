"""Spike: Python-only mid-conversation fork via JSONL manipulation.

Goal: Fork from turn 3 of a 5-turn session using Python only (no TypeScript).
Cache_read on the forked session MUST match the original at that point.

Strategy:
1. Reuse existing 5-turn session from fork_only_handoff spike
2. Python fork_session=True from end — inspect JSONL for what gets added
3. Truncate the forked JSONL to turn 3 (the desired branch point)
4. Resume the truncated fork from Python
5. Verify cache_read matches original turn 3

If Python fork adds unwanted messages, we also try:
- Raw JSONL copy with fresh UUID (no SDK fork at all)

Usage: .venv/bin/python spikes/python_mid_fork.py
       cat /tmp/python_mid_fork.log
"""

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

sys.path.insert(0, str(Path(__file__).parent))
from cache_utils import extract_cache_stats, extract_message_uuids

MODEL = "claude-haiku-4-5-20251001"
PROJECTS_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
LOG = Path("/tmp/python_mid_fork.log")
_log_lines: list[str] = []


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


def read_jsonl(session_id: str) -> list[dict]:
    path = PROJECTS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]


def write_jsonl(session_id: str, entries: list[dict]):
    path = PROJECTS_DIR / f"{session_id}.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def describe_jsonl(entries: list[dict], label: str):
    log(f"\n  === {label} ({len(entries)} entries) ===")
    for i, obj in enumerate(entries):
        t = obj.get("type", "?")
        uid = (obj.get("uuid") or "N/A")[:12]
        parent = (obj.get("parentUuid") or "N/A")[:12]
        msg = obj.get("message", {})
        usage = msg.get("usage", {})
        cr = usage.get("cache_read_input_tokens", "-")
        cc = usage.get("cache_creation_input_tokens", "-")
        inp = usage.get("input_tokens", "-")

        content = msg.get("content", "")
        preview = ""
        if isinstance(content, str):
            preview = content[:50]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    preview = block.get("text", "")[:50]
                    break
        log(f"    [{i}] {t:>15} uuid={uid} parent={parent} cr={cr} cc={cc} in={inp} | {preview}")


async def main():
    log("=" * 70)
    log("  SPIKE: Python-Only Mid-Conversation Fork")
    log("=" * 70)

    # ===================================================================
    # Phase 1: Create a fresh 5-turn session
    # ===================================================================
    log("\n--- Phase 1: Creating fresh 5-turn session ---")

    base_opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Be concise.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )

    CODE_WORD = "MANGO-THUNDER-9918"

    log(f"\n[1/5] Turn 1 — planting code word {CODE_WORD}...")
    flush_log()
    session_id, t1 = await run_query(
        f"Remember this code word: {CODE_WORD}. Just confirm.",
        base_opts,
    )
    log(f"  Session: {session_id}")
    flush_log()

    resume_opts = ClaudeAgentOptions(
        resume=session_id, model=MODEL,
        permission_mode="bypassPermissions", max_turns=1,
    )

    log("\n[2/5] Turn 2...")
    flush_log()
    await run_query(
        "Tell me about the history of programming languages. Cover at least 5 major ones. 500+ words.",
        resume_opts,
    )

    log("\n[3/5] Turn 3 (FORK POINT)...")
    flush_log()
    await run_query(
        "Compare compiled vs interpreted languages. Discuss tradeoffs. 500+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )

    log("\n[4/5] Turn 4...")
    flush_log()
    await run_query(
        "Discuss the rise of functional programming. 300+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )

    log("\n[5/5] Turn 5...")
    flush_log()
    await run_query(
        "What does the future of programming languages look like? 300+ words.",
        ClaudeAgentOptions(resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1),
    )

    # Inspect original JSONL
    orig_entries = read_jsonl(session_id)
    describe_jsonl(orig_entries, f"Original session {session_id[:16]}")
    flush_log()

    # Find the turn 3 boundary
    # Turn 3 assistant is the 3rd assistant entry
    assistant_indices = [i for i, e in enumerate(orig_entries) if e.get("type") == "assistant"]
    if len(assistant_indices) < 3:
        log("[!] Not enough assistant entries. Aborting.")
        flush_log()
        return

    turn3_asst_idx = assistant_indices[2]  # 0-indexed
    turn3_asst_entry = orig_entries[turn3_asst_idx]
    turn3_uuid = turn3_asst_entry["uuid"]
    turn3_usage = turn3_asst_entry.get("message", {}).get("usage", {})
    turn3_cache_read = turn3_usage.get("cache_read_input_tokens", 0)
    turn3_cache_create = turn3_usage.get("cache_creation_input_tokens", 0)
    turn3_input = turn3_usage.get("input_tokens", 0)
    turn3_total = turn3_cache_read + turn3_cache_create + turn3_input

    log(f"\n  Turn 3 assistant: index={turn3_asst_idx}, uuid={turn3_uuid[:12]}")
    log(f"  Turn 3 cache_read={turn3_cache_read}, cache_create={turn3_cache_create}, fresh={turn3_input}, total={turn3_total}")
    flush_log()

    # ===================================================================
    # Phase 2: Python fork from end — inspect what gets added
    # ===================================================================
    log("\n\n--- Phase 2: Python fork from end ---")
    flush_log()

    py_fork_id, py_fork_text = await run_query(
        "What is the code word?",
        ClaudeAgentOptions(
            resume=session_id, fork_session=True, model=MODEL,
            permission_mode="bypassPermissions", max_turns=1,
        ),
    )
    log(f"  Fork session: {py_fork_id}")

    fork_entries = read_jsonl(py_fork_id)
    describe_jsonl(fork_entries, f"Python fork (end) {py_fork_id[:16]}")

    # Compare: which entries are inherited vs new?
    orig_uuids = {e.get("uuid") for e in orig_entries if e.get("uuid")}
    new_entries = [(i, e) for i, e in enumerate(fork_entries) if e.get("uuid") and e["uuid"] not in orig_uuids]
    log(f"\n  New entries added by Python fork: {len(new_entries)}")
    for i, e in new_entries:
        log(f"    [{i}] type={e['type']} uuid={e.get('uuid', 'N/A')[:12]}")
    flush_log()

    # ===================================================================
    # Phase 3: Truncate forked JSONL to turn 3
    # ===================================================================
    log("\n\n--- Phase 3: Truncate forked JSONL to turn 3 ---")

    # The fork JSONL has: [metadata...] [turn1] [turn2] [turn3] [turn4] [turn5] [fork_user] [fork_asst]
    # We want: [metadata...] [turn1] [turn2] [turn3]
    # That means keep entries up to and including turn3_asst_idx equivalent in the fork

    # Find turn 3 assistant in the fork by UUID match
    fork_turn3_idx = None
    for i, e in enumerate(fork_entries):
        if e.get("uuid") == turn3_uuid:
            fork_turn3_idx = i
            break

    if fork_turn3_idx is None:
        log("  [!] Turn 3 UUID not found in fork JSONL. Aborting.")
        flush_log()
        return

    truncated_entries = fork_entries[:fork_turn3_idx + 1]
    log(f"  Fork had {len(fork_entries)} entries, truncating to {len(truncated_entries)} (up to index {fork_turn3_idx})")

    # Write truncated JSONL back
    write_jsonl(py_fork_id, truncated_entries)
    describe_jsonl(truncated_entries, f"Truncated fork {py_fork_id[:16]}")
    flush_log()

    # ===================================================================
    # Phase 4: Resume the truncated fork from Python
    # ===================================================================
    log("\n\n--- Phase 4: Resume truncated fork ---")
    flush_log()

    trunc_resume_id, trunc_text = await run_query(
        f"What is the code word I asked you to remember? Also list all topics we discussed.",
        ClaudeAgentOptions(
            resume=py_fork_id, model=MODEL,
            permission_mode="bypassPermissions", max_turns=1,
        ),
    )

    log(f"  Resumed session: {trunc_resume_id}")
    log(f"  Same as fork? {trunc_resume_id == py_fork_id}")
    log(f"  Code word found? {CODE_WORD in trunc_text}")
    log(f"  Response preview: {trunc_text[:300]}")

    # Get cache stats for the new turn
    trunc_entries = read_jsonl(py_fork_id)
    describe_jsonl(trunc_entries, f"After resume {py_fork_id[:16]}")

    # Find the NEW assistant entry (the one we just added)
    new_asst = [e for e in trunc_entries if e.get("type") == "assistant" and e.get("uuid") not in orig_uuids]
    flush_log()

    # ===================================================================
    # Phase 5: Also try raw JSONL copy (no SDK fork at all)
    # ===================================================================
    log("\n\n--- Phase 5: Raw JSONL copy with fresh UUID ---")

    raw_session_id = str(uuid.uuid4())
    log(f"  Creating raw copy: {raw_session_id[:16]}")

    # Copy original JSONL entries up to turn 3
    # Include the metadata entries (queue-operation at start)
    raw_entries = orig_entries[:turn3_asst_idx + 1]
    write_jsonl(raw_session_id, raw_entries)
    describe_jsonl(raw_entries, f"Raw copy {raw_session_id[:16]}")
    flush_log()

    # Resume the raw copy
    log("\n  Resuming raw copy...")
    flush_log()

    try:
        raw_resume_id, raw_text = await run_query(
            f"What is the code word I asked you to remember? Also list all topics we discussed.",
            ClaudeAgentOptions(
                resume=raw_session_id, model=MODEL,
                permission_mode="bypassPermissions", max_turns=1,
            ),
        )
        log(f"  Resumed session: {raw_resume_id}")
        log(f"  Same as raw? {raw_resume_id == raw_session_id}")
        log(f"  Code word found? {CODE_WORD in raw_text}")
        log(f"  Response preview: {raw_text[:300]}")

        raw_after = read_jsonl(raw_session_id)
        describe_jsonl(raw_after, f"Raw copy after resume {raw_session_id[:16]}")
    except Exception as e:
        log(f"  [ERROR] Raw copy resume failed: {e}")
    flush_log()

    # ===================================================================
    # Phase 6: Cache comparison
    # ===================================================================
    log(f"\n\n{'#' * 70}")
    log(f"  CACHE COMPARISON")
    log(f"{'#' * 70}")

    log(f"\n  BASELINE (original session turn 3):")
    log(f"    cache_read={turn3_cache_read:,}  cache_create={turn3_cache_create:,}  fresh={turn3_input}  total={turn3_total:,}")

    # Truncated fork resume
    if new_asst:
        for e in new_asst:
            u = e.get("message", {}).get("usage", {})
            cr = u.get("cache_read_input_tokens", 0)
            cc = u.get("cache_creation_input_tokens", 0)
            inp = u.get("input_tokens", 0)
            total = cr + cc + inp
            log(f"\n  TRUNCATED FORK resume (new turn after truncation):")
            log(f"    cache_read={cr:,}  cache_create={cc:,}  fresh={inp}  total={total:,}")
            delta = cr - turn3_cache_read
            log(f"    Delta vs original turn 3: cache_read {'+' if delta >= 0 else ''}{delta:,}")

    # Raw copy resume
    try:
        raw_after_entries = read_jsonl(raw_session_id)
        raw_new_asst = [e for e in raw_after_entries
                        if e.get("type") == "assistant" and e.get("uuid") not in orig_uuids]
        for e in raw_new_asst:
            u = e.get("message", {}).get("usage", {})
            cr = u.get("cache_read_input_tokens", 0)
            cc = u.get("cache_creation_input_tokens", 0)
            inp = u.get("input_tokens", 0)
            total = cr + cc + inp
            log(f"\n  RAW COPY resume (new turn after copy):")
            log(f"    cache_read={cr:,}  cache_create={cc:,}  fresh={inp}  total={total:,}")
            delta = cr - turn3_cache_read
            log(f"    Delta vs original turn 3: cache_read {'+' if delta >= 0 else ''}{delta:,}")
    except Exception:
        pass

    # Original turn 4 (for reference — what the next turn WOULD have been)
    if len(assistant_indices) >= 4:
        t4_entry = orig_entries[assistant_indices[3]]
        t4_usage = t4_entry.get("message", {}).get("usage", {})
        t4_cr = t4_usage.get("cache_read_input_tokens", 0)
        t4_cc = t4_usage.get("cache_creation_input_tokens", 0)
        t4_inp = t4_usage.get("input_tokens", 0)
        t4_total = t4_cr + t4_cc + t4_inp
        log(f"\n  ORIGINAL turn 4 (reference):")
        log(f"    cache_read={t4_cr:,}  cache_create={t4_cc:,}  fresh={t4_inp}  total={t4_total:,}")

    log(f"\n\n  VERDICT:")
    log(f"  If truncated fork cache_read ≈ original turn 3 cache_read ({turn3_cache_read:,}): SUCCESS")
    log(f"  If raw copy cache_read ≈ original turn 3 cache_read ({turn3_cache_read:,}): BONUS SUCCESS")
    log()
    flush_log()
    sys.stderr.write(f"\nDone. Full results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
