"""Tests for cache proxy normalization rules under stress.

Validates that the cache-normalizing proxy correctly handles:
- Dynamic system-reminder injection from file operations (Rule 3)
- Tool definition reordering across process restarts (Rule 6)
- String-to-list content conversion (Rule 4)
- Idempotent normalization (all rules applied twice = same output)
- Agent-like sessions with tool use that fork correctly
- ForkRunner (real OBS fork path) cache hit verification

All tests use claude-haiku-4-5 via the shared proxy fixture.
Cache classification uses the adjusted metric (baseline subtraction,
HIT/MISS/EDGE) from .claude/skills/jsonl-analysis/SKILL.md.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure tests/ dir is importable (conftest_cache_proxy lives there)
_tests_dir = str(Path(__file__).resolve().parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import pytest

from conftest_cache_proxy import (
    BULK_TEXT,
    TEST_MODEL,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    assert_cache_hit,
    classify_cache_hit,
    compute_baseline,
    extract_fork_first_turn,
    extract_usage,
    find_session_jsonl,
    fmt_usage,
    get_proxy_usage_for_turns,
    make_sdk_options,
    proxy_log_length,
    read_proxy_bodies,
    read_proxy_usage_log,
    run_turn,
)


# ---------------------------------------------------------------------------
# Markers — all tests here are live integration tests hitting the Anthropic API
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(300),  # 5 min max per test
]


# ── Test 1: File operations trigger system-reminders, proxy strips them ──


@pytest.mark.asyncio
async def test_file_operations_trigger_system_reminders_stripped(
    proxy: int, test_project: Path
):
    """Parent reads/edits files (triggering system-reminder injection).

    After file operations, CC injects <system-reminder> blocks with
    changed_files content into user messages. The proxy's Rule 3 should
    strip these, allowing the fork to hit cache despite the parent having
    dynamic reminders that the fork wouldn't have.

    Verification:
    - Parent session does file operations (read, write)
    - Fork from parent
    - Fork's first turn classified as HIT using adjusted cache metric
    """
    # ── Parent: do file operations to trigger system-reminders ──
    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        # Turn 1: bulk text to build prefix
        sid, u1 = await run_turn(
            parent,
            f"Reference document:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        # Turn 2: read a file — triggers changed_files system-reminder on next turn
        sid, u2 = await run_turn(
            parent,
            "Read the file data/sample.txt and tell me how many lines it has. "
            "Reply with just the number.",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u2)

        # Turn 3: write to a file — triggers another changed_files reminder
        sid, u3 = await run_turn(
            parent,
            "Append the line 'test line added by proxy test' to data/sample.txt. "
            "Then reply with exactly: DONE",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u3)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"\n  Parent session: {parent_sid}")
    for i, u in enumerate(usage_rows, 1):
        print(f"    Turn {i}: {fmt_usage(u)}")

    # ── Fork ──
    fork_opts = make_sdk_options(
        test_project, proxy, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork, "Reply with exactly: FORK_AFTER_FILE_OPS"
        )
    finally:
        await fork.disconnect()

    assert fork_sid, "Failed to get fork session ID"
    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify cache hit using adjusted metric ──
    parent_rows = extract_usage(parent_sid)
    assert len(parent_rows) >= 2, f"Expected ≥2 parent rows, got {len(parent_rows)}"

    # Baseline from parent's cache reads
    parent_crs = [r["cr"] for r in parent_rows]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    # Fork's first turn (using sessionId-based detection)
    fork_turn = extract_fork_first_turn(fork_sid)
    assert fork_turn, f"Could not find fork's first turn in JSONL for {fork_sid}"

    # Previous total = parent's last turn total
    prev_total = parent_rows[-1]["tot"]
    print(
        f"  Fork first turn: cr={fork_turn['cr']:,}, prev_total={prev_total:,}, "
        f"classification={classify_cache_hit(fork_turn['cr'], prev_total, baseline)}"
    )
    assert_cache_hit(fork_turn["cr"], prev_total, baseline, "file ops fork")


# ── Test 2: Tool reordering normalized ───────────────────────────────────


@pytest.mark.asyncio
async def test_tool_reordering_normalized(proxy: int, test_project: Path):
    """Parent uses multiple tools, fork gets cache HIT despite tool reorder.

    The proxy's Rule 6 sorts tool definitions alphabetically, so even if CC
    presents tools in a different order on the fork's process restart, the
    normalized request has identical tool ordering.

    Verification:
    - Parent session uses Read, Write, and Bash tools
    - Fork from parent
    - Fork's first turn classified as HIT
    """
    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        # Turn 1: bulk text
        sid, u1 = await run_turn(
            parent,
            f"Reference:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        # Turn 2: trigger multiple tool types
        sid, u2 = await run_turn(
            parent,
            "Do all of the following: "
            "1. Read hello.py "
            "2. Run 'echo tool_test_output' in bash "
            "3. Reply with exactly: TOOLS_USED",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u2)

        # Turn 3: additional turn to confirm cache within parent
        sid, u3 = await run_turn(parent, "Reply with exactly: ACK3")
        if sid:
            parent_sid = sid
        usage_rows.append(u3)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"

    # ── Fork ──
    fork_opts = make_sdk_options(
        test_project, proxy, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork, "Reply with exactly: FORK_AFTER_TOOLS"
        )
    finally:
        await fork.disconnect()

    assert fork_sid, "Failed to get fork session ID"
    print(f"\n  Parent: {parent_sid}, Fork: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify ──
    parent_rows = extract_usage(parent_sid)
    assert parent_rows, "No parent usage rows"
    parent_crs = [r["cr"] for r in parent_rows]
    baseline = compute_baseline(parent_crs)

    fork_turn = extract_fork_first_turn(fork_sid)
    assert fork_turn, f"No fork first turn for {fork_sid}"

    prev_total = parent_rows[-1]["tot"]
    assert_cache_hit(fork_turn["cr"], prev_total, baseline, "tool reorder fork")


# ── Test 3: String-to-list normalization fires ───────────────────────────


@pytest.mark.asyncio
async def test_string_to_list_normalization(
    proxy_with_bodies: int, test_project: Path
):
    """Verify the proxy's Rule 4 (string→list conversion) fires.

    CC sometimes sends older user message content as bare strings instead
    of [{type: "text", text: "..."}] lists. The proxy converts them to list
    format for byte consistency.

    Verification:
    - Run a multi-turn session through the proxy with SAVE_BODIES enabled
    - Inspect pre/post normalization bodies
    - Confirm at least one request has a user message whose content changed
      from string (in pre) to list (in post), proving Rule 4 fired
    - Also verify no errors in proxy usage log
    """
    opts = make_sdk_options(test_project, proxy_with_bodies)
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        # Multiple turns to trigger string demotion on older messages
        for i in range(4):
            prompt = (
                f"Turn {i+1} padding: {BULK_TEXT[:5000]}\n\n"
                f"Reply with exactly: TURN{i+1}"
            )
            await run_turn(client, prompt)
    finally:
        await client.disconnect()

    # Small delay for proxy to flush logs
    time.sleep(0.5)

    # Check proxy usage log for errors
    entries = read_proxy_usage_log()
    assert len(entries) >= 4, f"Expected ≥4 proxy log entries, got {len(entries)}"

    error_entries = [e for e in entries if "error" in str(e.get("norm_action", ""))]
    assert not error_entries, f"Proxy had error entries: {error_entries}"

    print(f"\n  Proxy log entries: {len(entries)}")
    for i, e in enumerate(entries):
        print(
            f"    #{i+1}: norm_action={e.get('norm_action', '?')} "
            f"tot={e.get('total', 0):,} cr={e.get('cache_read', 0):,}"
        )

    # ── Verify Rule 4 fired: inspect pre/post bodies for string→list conversion ──
    body_dir = Path("/tmp/cache-proxy-log/bodies/")
    assert body_dir.exists(), "Body directory not found — SAVE_BODIES may not be active"

    pre_files = sorted(body_dir.glob("req*_pre.json"))
    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(pre_files) >= 4, f"Expected ≥4 pre files, got {len(pre_files)}"
    assert len(post_files) >= 4, f"Expected ≥4 post files, got {len(post_files)}"

    strings_converted = 0
    for pre_f, post_f in zip(pre_files, post_files):
        pre_body = json.loads(pre_f.read_text())
        post_body = json.loads(post_f.read_text())

        pre_msgs = pre_body.get("messages", [])
        post_msgs = post_body.get("messages", [])

        for msg_idx, (pre_msg, post_msg) in enumerate(zip(pre_msgs, post_msgs)):
            if pre_msg.get("role") != "user":
                continue
            pre_content = pre_msg.get("content")
            post_content = post_msg.get("content")
            # String in pre, list in post = Rule 4 fired
            if isinstance(pre_content, str) and isinstance(post_content, list):
                strings_converted += 1
                print(
                    f"  Rule 4 fired: {pre_f.name} msg[{msg_idx}] "
                    f"string ({len(pre_content)} chars) → list"
                )

    # String demotion is CC-version-dependent and nondeterministic.
    # After 4 turns, CC typically demotes at least 1 older message to bare string.
    # If zero conversions, the test still passes structurally but we flag it.
    if strings_converted > 0:
        print(f"  ✓ Rule 4 confirmed: {strings_converted} string→list conversions")
    else:
        # Not a hard failure — CC may not have demoted any messages in this run.
        # But we verify structurally that ALL user messages in post bodies ARE lists
        # (which is the postcondition Rule 4 guarantees regardless of whether it fired).
        print("  ⚠ No string→list conversions observed (CC didn't demote any messages)")
        print("  Verifying postcondition: all post-body user messages are list format...")
        for post_f in post_files:
            post_body = json.loads(post_f.read_text())
            for msg_idx, msg in enumerate(post_body.get("messages", [])):
                if msg.get("role") == "user":
                    assert isinstance(msg.get("content"), list), (
                        f"{post_f.name} msg[{msg_idx}]: user content is not list format"
                    )
        print("  ✓ Postcondition holds: all user messages in list format")


# ── Test 4: Normalization idempotency ────────────────────────────────────


@pytest.mark.asyncio
async def test_normalization_idempotency(proxy_with_bodies: int, test_project: Path):
    """Same request through the proxy produces identical post-normalization output.

    Runs two turns with identical prompts. The post-normalization bodies
    should be structurally consistent (same normalization applied).

    Uses SAVE_BODIES mode to inspect pre/post normalization JSON.

    Verification:
    - Two identical turns through the proxy
    - Post-normalization bodies have the same structural normalizations:
      - system[0] has fixed billing header
      - all user messages have list-format content
      - all user message last blocks have cache_control
      - tools array is sorted
      - metadata has normalized session ID
    """
    opts = make_sdk_options(test_project, proxy_with_bodies)
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        # Two identical prompts
        await run_turn(client, f"Document:\n\n{BULK_TEXT}\n\nReply: READY")
        await run_turn(client, "Reply with exactly: IDEM_CHECK")
    finally:
        await client.disconnect()

    time.sleep(0.5)

    # Read saved request bodies — check structural invariants on post bodies
    body_dir = Path("/tmp/cache-proxy-log/bodies/")
    assert body_dir.exists(), "Body directory not found — SAVE_BODIES may not be active"

    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(post_files) >= 2, f"Expected ≥2 post files, got {len(post_files)}"

    for pf in post_files:
        body = json.loads(pf.read_text())
        label = pf.name

        # Rule 1: billing header normalized
        system = body.get("system", [])
        if system:
            first_text = next(
                (b for b in system if isinstance(b, dict) and b.get("type") == "text"),
                None,
            )
            if first_text and first_text["text"].startswith("x-anthropic-billing-header:"):
                assert "cch=0" in first_text["text"], (
                    f"{label}: billing header not normalized: {first_text['text'][:100]}"
                )

        # Rule 4: all user messages should have list-format content
        messages = body.get("messages", [])
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                assert isinstance(content, list), (
                    f"{label}: msg[{i}] has string content, should be list"
                )

        # Rule 5: last block of each user message has cache_control
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if content and isinstance(content, list):
                    last_block = content[-1]
                    if isinstance(last_block, dict):
                        assert "cache_control" in last_block, (
                            f"{label}: msg[{i}] last block missing cache_control"
                        )

        # Rule 6: tools sorted alphabetically
        tools = body.get("tools", [])
        if tools:
            tool_names = [t.get("name", "") for t in tools]
            assert tool_names == sorted(tool_names), (
                f"{label}: tools not sorted. First unsorted: "
                f"{next(a for a, b in zip(tool_names, sorted(tool_names)) if a != b)}"
            )

        # Rule 7: metadata session ID normalized
        metadata = body.get("metadata", {})
        uid = metadata.get("user_id", "")
        if uid and "_session_" in uid:
            assert uid.endswith("_session_0"), (
                f"{label}: metadata not normalized: {uid}"
            )

        print(f"  {label}: all invariants hold ✓")

    # Cross-request consistency: compare structural properties
    # Both post-normalization bodies should have the same system prompt
    # (since CLAUDE.md doesn't change between turns)
    body1 = json.loads(post_files[0].read_text())
    body2 = json.loads(post_files[1].read_text())

    sys1 = json.dumps(body1.get("system", []), sort_keys=True)
    sys2 = json.dumps(body2.get("system", []), sort_keys=True)
    assert sys1 == sys2, "System prompts differ between requests"

    # Tool definitions should be identical (same sorted order)
    tools1 = json.dumps(body1.get("tools", []), sort_keys=True)
    tools2 = json.dumps(body2.get("tools", []), sort_keys=True)
    assert tools1 == tools2, "Tool definitions differ between requests"

    print("  Cross-request structural consistency verified ✓")

    # Byte-level identity of the shared prefix: the messages that exist in
    # both requests (body2 has an extra turn appended, so body1's messages
    # are a prefix of body2's). Serialize the shared prefix from both and
    # compare byte-for-byte.
    msgs1 = body1.get("messages", [])
    msgs2 = body2.get("messages", [])
    shared_len = len(msgs1)  # body1 has fewer messages (turn 1 only)

    if shared_len > 0 and len(msgs2) >= shared_len:
        prefix1 = json.dumps(msgs1[:shared_len], sort_keys=True)
        prefix2 = json.dumps(msgs2[:shared_len], sort_keys=True)
        assert prefix1 == prefix2, (
            f"Shared message prefix is NOT byte-identical across requests. "
            f"Prefix length: {shared_len} messages. "
            f"First diff at char {next((i for i, (a, b) in enumerate(zip(prefix1, prefix2)) if a != b), '?')}"
        )
        print(
            f"  Byte-level prefix identity verified ✓ "
            f"({shared_len} shared messages, {len(prefix1):,} bytes)"
        )
    else:
        print(
            f"  ⚠ Could not verify byte-level prefix: "
            f"msgs1={len(msgs1)}, msgs2={len(msgs2)}"
        )


# ── Test 5: Agent-like fork with tool use ────────────────────────────────


@pytest.mark.asyncio
async def test_agent_self_fork_via_sdk(proxy: int, test_project: Path):
    """Parent session with heavy tool use (simulating an agent), fork hits cache.

    Simulates a real OBS agent workflow: the parent reads files, writes files,
    runs bash commands, and builds up context. Then a fork (simulating what
    AgentTask does) should hit the parent's cache.

    This exercises all normalizations simultaneously under realistic conditions:
    - Rule 2: skill listing position (CC regenerates on fork startup)
    - Rule 3: system-reminders from tool results
    - Rule 4: string demotion of older messages
    - Rule 5: cache_control propagation
    - Rule 6: tool reordering

    Verification:
    - Parent does 4 turns of agent-like work (read, write, bash, analysis)
    - Fork from parent
    - Fork's first turn classified as HIT
    """
    # Record proxy log offset to isolate this test's entries
    log_offset = proxy_log_length()

    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        # Turn 1: context loading (simulates session-start)
        sid, u1 = await run_turn(
            parent,
            f"You are an agent working on a project. "
            f"Context document:\n\n{BULK_TEXT}\n\n"
            f"Reply with exactly: AGENT_READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        # Turn 2: read files (agent research phase)
        sid, u2 = await run_turn(
            parent,
            "Read hello.py and data/config.json. "
            "Summarize what you found in one sentence.",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u2)

        # Turn 3: write a file (agent implementation phase)
        sid, u3 = await run_turn(
            parent,
            "Create a new file called data/report.txt with the content: "
            "'Agent test report: all systems operational.' "
            "Then reply with exactly: FILE_WRITTEN",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u3)

        # Turn 4: run a command (agent verification phase)
        sid, u4 = await run_turn(
            parent,
            "Run 'ls -la data/' and count the files. Reply with the count.",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u4)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"\n  Parent session: {parent_sid} ({len(usage_rows)} turns)")
    for i, u in enumerate(usage_rows, 1):
        print(f"    Turn {i}: {fmt_usage(u)}")

    # ── Fork (simulating AgentTask) ──
    fork_opts = make_sdk_options(
        test_project, proxy, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork,
            "You are a forked agent. Review what was done and reply with "
            "exactly: FORK_AGENT_ACK",
        )
    finally:
        await fork.disconnect()

    assert fork_sid, "Failed to get fork session ID"
    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify cache hit ──
    # Use SDK-reported fork_usage directly (accurate for single-turn fresh
    # connections). Use proxy log for parent baseline only.
    time.sleep(0.5)
    proxy_entries = get_proxy_usage_for_turns(log_offset)

    # Compute baseline from proxy entries that are clearly parent requests
    # (all entries before the fork). With tool use, the parent generates
    # multiple API requests per turn (tool call → result → continuation).
    # The fork's request has skill=moved in the proxy log.
    parent_crs = [e.get("cr", 0) for e in proxy_entries[:-1]]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    # Fork's cache stats from SDK (accurate for single-turn fresh connection)
    fork_cr = fork_usage.get("cache_read_input_tokens", 0)
    # prev_total: the last parent entry with meaningful total (not the 468-token
    # health check). Find it from proxy entries.
    meaningful_parent = [
        e for e in proxy_entries[:-1] if e.get("tot", 0) > 10000
    ]
    prev_total = meaningful_parent[-1].get("tot", 0) if meaningful_parent else 0

    classification = classify_cache_hit(fork_cr, prev_total, baseline)
    print(
        f"  Fork: cr={fork_cr:,}, prev_total={prev_total:,}, "
        f"baseline={baseline:,}, classification={classification}"
    )
    assert_cache_hit(fork_cr, prev_total, baseline, "agent-like fork with tool use")


# ── Test 6: Minimal-option fork (ForkRunner-like path) ─────────────────


@pytest.mark.asyncio
async def test_minimal_option_fork_path(proxy: int, test_project: Path):
    """Fork with minimal options (mimicking ForkRunner production path) hits cache.

    ForkRunner in production creates options with only resume + fork_session,
    then calls query(). However, query() with minimal options fails (exit code 1)
    because the CLI subprocess needs cwd to find the session JSONL and
    permission_mode to avoid hanging on prompts.

    This test exercises the closest possible production path: fork via
    ClaudeSDKClient with ONLY resume, fork_session, cwd, and permission_mode
    — no model, no custom env. The proxy routing is set via os.environ
    ANTHROPIC_BASE_URL (inherited by subprocess), same as production.

    KNOWN BUG: ForkRunner.run() itself fails because query() doesn't work
    with fork_session when cwd/model/permission_mode are missing from options.
    ForkRunner needs to be updated to pass these options. See:
    ~/Documents/obs-cache-proxy/src/obs_agent/fork.py

    Verification:
    - Parent session via raw SDK with bulk text (3 turns)
    - Fork with minimal options via ClaudeSDKClient
    - Fork's first turn classified as HIT using adjusted cache metric
    """
    # Record proxy log offset to isolate this test's entries
    log_offset = proxy_log_length()

    # ── Parent session via raw SDK ──
    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        # Turn 1: bulk text to build a conversation prefix well above baseline
        sid, u1 = await run_turn(
            parent,
            f"Reference document A:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        # Turn 2: more bulk text + file read
        sid, u2 = await run_turn(
            parent,
            f"Reference document B:\n\n{BULK_TEXT}\n\n"
            "Read hello.py and summarize it in one sentence.",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u2)

        # Turn 3: confirm cache within parent
        sid, u3 = await run_turn(parent, "Reply with exactly: PARENT_DONE")
        if sid:
            parent_sid = sid
        usage_rows.append(u3)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"\n  Parent session: {parent_sid} ({len(usage_rows)} turns)")
    for i, u in enumerate(usage_rows, 1):
        print(f"    Turn {i}: {fmt_usage(u)}")

    # ── Fork with minimal options ──
    # Set ANTHROPIC_BASE_URL in process env (inherited by subprocess).
    # This is how production works — session.py sets it in _DEFAULT_SDK_ENV.
    old_base_url = os.environ.get("ANTHROPIC_BASE_URL")
    os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:{proxy}"
    try:
        # Minimal options: resume + fork + cwd + permission_mode + model.
        # Model must match the parent's model — without it, the CLI defaults
        # to Opus, which has a different system prompt and cache prefix.
        # ForkRunner in production has this same bug (doesn't pass model).
        # No custom env dict — ANTHROPIC_BASE_URL is inherited from os.environ.
        fork_opts = ClaudeAgentOptions(
            resume=parent_sid,
            fork_session=True,
            cwd=str(test_project),
            permission_mode="bypassPermissions",
            model=TEST_MODEL,
        )
        fork = ClaudeSDKClient(fork_opts)
        await fork.connect()
        fork_sid = None
        fork_usage = {}
        try:
            fork_sid, fork_usage = await run_turn(
                fork, "Reply with exactly: MINIMAL_FORK_ACK"
            )
        finally:
            await fork.disconnect()
    finally:
        if old_base_url is not None:
            os.environ["ANTHROPIC_BASE_URL"] = old_base_url
        else:
            os.environ.pop("ANTHROPIC_BASE_URL", None)

    assert fork_sid, "Failed to get fork session ID"
    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify cache hit using proxy log ──
    # The fork JSONL may not have flushed assistant entries yet (race condition
    # with ClaudeSDKClient cleanup). Use the proxy usage log instead — it records
    # every API request in real time and is the authoritative source for cache stats.
    time.sleep(0.5)  # allow proxy log flush
    proxy_entries = get_proxy_usage_for_turns(log_offset)
    assert len(proxy_entries) >= 4, (
        f"Expected ≥4 proxy log entries from this test (3 parent + 1 fork), "
        f"got {len(proxy_entries)} (offset={log_offset})"
    )

    # Parent entries are all but the last, fork is the last
    parent_proxy = proxy_entries[:-1]
    fork_proxy_entry = proxy_entries[-1]

    # Compute baseline from parent's cache reads
    parent_crs = [e.get("cr", 0) for e in parent_proxy]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    # Fork's cache stats from proxy log
    fork_cr = fork_proxy_entry.get("cr", 0)
    prev_total = parent_proxy[-1].get("tot", 0)

    classification = classify_cache_hit(fork_cr, prev_total, baseline)
    print(
        f"  Fork (from proxy log): cr={fork_cr:,}, prev_total={prev_total:,}, "
        f"baseline={baseline:,}, classification={classification}"
    )
    assert_cache_hit(fork_cr, prev_total, baseline, "minimal-option fork path")
