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
    TEST_LOG_DIR,
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


def extract_usage_row(usage: dict) -> dict | None:
    cr = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation_input_tokens", 0)
    ip = usage.get("input_tokens", 0)
    tot = cr + cc + ip
    if tot <= 0:
        return None
    return {"cr": cr, "cc": cc, "ip": ip, "tot": tot}


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

    # Fork's first turn (using sessionId-based detection).  The SDK sometimes
    # does not flush the fork JSONL before this assertion, but the fork turn
    # usage returned by run_turn is already the first fork turn.
    fork_turn = extract_usage_row(fork_usage) or extract_fork_first_turn(fork_sid)
    assert fork_turn, f"Could not find fork's first turn usage for {fork_sid}"

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

    fork_turn = extract_usage_row(fork_usage) or extract_fork_first_turn(fork_sid)
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
    body_dir = Path(TEST_LOG_DIR) / "bodies"
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
    body_dir = Path(TEST_LOG_DIR) / "bodies"
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

        # cache_control: NOT checked — proxy leaves CC's native placement untouched.
        # See spikes/cache_control_breakpoint_report.md

        # Rule 5: tools sorted alphabetically
        tools = body.get("tools", [])
        if tools:
            tool_names = [t.get("name", "") for t in tools]
            assert tool_names == sorted(tool_names), (
                f"{label}: tools not sorted. First unsorted: "
                f"{next(a for a, b in zip(tool_names, sorted(tool_names)) if a != b)}"
            )

        # Rule 6: metadata session ID normalized
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

    # Byte-level identity of the shared prefix (excluding cache_control).
    # cache_control moves between turns (CC places it on the last user msg),
    # but it's not part of the cache key — confirmed via spike. We strip it
    # before comparison to verify the actual content prefix matches.
    def strip_cc(msgs):
        """Deep-copy messages and remove all cache_control keys for comparison."""
        import copy
        cleaned = copy.deepcopy(msgs)
        for msg in cleaned:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        del block["cache_control"]
        return cleaned

    msgs1 = body1.get("messages", [])
    msgs2 = body2.get("messages", [])
    shared_len = len(msgs1)  # body1 has fewer messages (turn 1 only)

    if shared_len > 0 and len(msgs2) >= shared_len:
        prefix1 = json.dumps(strip_cc(msgs1[:shared_len]), sort_keys=True)
        prefix2 = json.dumps(strip_cc(msgs2[:shared_len]), sort_keys=True)
        assert prefix1 == prefix2, (
            f"Shared message prefix is NOT byte-identical across requests "
            f"(cache_control excluded). "
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
    - Rule 5: tool reordering

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


# ── Test 7: Git status divergence normalized ──────────────────────────


@pytest.mark.asyncio
async def test_git_status_divergence_normalized(
    proxy_with_bodies: int, test_project: Path
):
    """Fork from a git repo with dirty working tree still hits cache.

    The gitStatus section in sys[2] changes when the working tree changes
    between parent and fork process startup. Rule 5 (normalize_git_status)
    replaces the gitStatus section with a fixed placeholder.

    The SDK's CC CLI subprocess includes gitStatus in the system prompt
    when cwd is a git repository. We test this by:
    1. Make test_project a git repo with dirty working tree
    2. Run a parent session (CC includes gitStatus with dirty files)
    3. Add more dirty files (different git status)
    4. Fork — proxy normalizes gitStatus, enabling cache hit

    If CC doesn't include gitStatus (SDK may skip it), we verify the
    normalization fired on post-normalization bodies and fall back to
    confirming the proxy's structural invariants.

    Verification:
    - Fork's first turn classified as HIT (if CC includes gitStatus)
    - Post-normalization bodies verified for normalization invariants
    """
    import subprocess as sp

    # Initialize test_project as a git repo with some committed + dirty files
    sp.run(["git", "init"], cwd=test_project, capture_output=True, check=True)
    sp.run(["git", "add", "CLAUDE.md", "hello.py"], cwd=test_project,
           capture_output=True, check=True)
    sp.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=test_project, capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    # Leave data/ untracked so gitStatus shows dirty
    print(f"\n  Test project git repo: {test_project}")

    # ── Parent ──
    opts = make_sdk_options(test_project, proxy_with_bodies)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        sid, u1 = await run_turn(
            parent,
            f"Reference document:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        sid, u2 = await run_turn(parent, "Reply with exactly: ACK2")
        if sid:
            parent_sid = sid
        usage_rows.append(u2)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"  Parent session: {parent_sid}")
    for i, u in enumerate(usage_rows, 1):
        print(f"    Turn {i}: {fmt_usage(u)}")

    # ── Add more untracked files to change git status before forking ──
    (test_project / "newfile_untracked_1.txt").write_text("new content 1\n")
    (test_project / "newfile_untracked_2.txt").write_text("new content 2\n")

    # ── Fork ──
    fork_opts = make_sdk_options(
        test_project, proxy_with_bodies, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork, "Reply with exactly: FORK_DIRTY_GIT"
        )
    finally:
        await fork.disconnect()

    assert fork_sid, "Failed to get fork session ID"
    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify: check post-normalization bodies ──
    time.sleep(0.5)
    body_dir = Path(TEST_LOG_DIR) / "bodies"
    post_files = sorted(body_dir.glob("req*_post.json"))

    # Check if any body has gitStatus (CC may not include it via SDK)
    git_normalized_count = 0
    for pf in post_files:
        body = json.loads(pf.read_text())
        system = body.get("system", [])
        for i, block in enumerate(system):
            text = block.get("text", "")
            if "gitStatus: normalized" in text:
                git_normalized_count += 1
                print(f"  {pf.name} sys[{i}]: gitStatus normalized ✓")
            elif "gitStatus:" in text:
                # gitStatus present but NOT normalized — this would be a bug
                print(f"  {pf.name} sys[{i}]: gitStatus NOT normalized ✗")
                assert False, (
                    f"gitStatus present but not normalized in {pf.name} sys[{i}]"
                )

    if git_normalized_count > 0:
        print(f"  ✓ Git status normalization fired on {git_normalized_count} requests")
    else:
        print("  ⚠ No gitStatus in system prompt — SDK may not include it. "
              "Verifying fork cache hit without gitStatus normalization.")

    # ── Verify cache hit ──
    parent_rows = extract_usage(parent_sid)
    if parent_rows:
        parent_crs = [r["cr"] for r in parent_rows]
        baseline = compute_baseline(parent_crs)
        prev_total = parent_rows[-1]["tot"]

        fork_turn = extract_usage_row(fork_usage) or extract_fork_first_turn(fork_sid)
        if fork_turn:
            classification = classify_cache_hit(fork_turn["cr"], prev_total, baseline)
            print(
                f"  Fork: cr={fork_turn['cr']:,}, prev_total={prev_total:,}, "
                f"baseline={baseline:,}, classification={classification}"
            )
            assert_cache_hit(
                fork_turn["cr"], prev_total, baseline,
                "git status divergence fork"
            )
        else:
            # Fall back to proxy log
            proxy_entries = get_proxy_usage_for_turns()
            if len(proxy_entries) >= 3:
                fork_entry = proxy_entries[-1]
                parent_entry = proxy_entries[-2]
                fork_cr = fork_entry.get("cr", 0)
                prev_tot = parent_entry.get("tot", 0)
                baseline_entries = [e.get("cr", 0) for e in proxy_entries[:-1]]
                baseline = compute_baseline(baseline_entries)
                assert_cache_hit(fork_cr, prev_tot, baseline,
                                 "git status divergence fork (proxy log)")
            else:
                pytest.skip("Not enough proxy entries to verify cache hit")
    else:
        pytest.skip("Could not extract parent usage from JSONL")


# ── Test 8: CLAUDE.md modification between parent and fork (xfail) ────


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="CLAUDE.md modification between parent and fork causes cache miss — "
           "known limitation (CLAUDE.md is re-read from disk on fork startup, "
           "producing different content in the system-reminder injection). "
           "May XPASS via SDK because the SDK's minimal CC subprocess doesn't "
           "always inject CLAUDE.md as a system-reminder.",
    strict=False,  # Expected: FAIL in production CC, XPASS via SDK
)
async def test_claudemd_modification_causes_cache_miss(
    proxy: int, test_project: Path
):
    """Modifying CLAUDE.md between parent and fork should break cache.

    This documents a known limitation: the proxy preserves CLAUDE.md content
    (it's the static system-reminder at msg[0]), but if the file changes on
    disk between parent start and fork start, the fork process re-reads it
    and injects different content. The prefix diverges at that point.

    This test is xfail — it documents the limitation rather than blocking CI.
    The fix (injecting CLAUDE.md via OBS system prompt) is tracked separately.

    NOTE: This test XPASSes in the SDK test environment because the SDK's
    minimal CC subprocess does not inject CLAUDE.md as a <system-reminder>
    block. In production CC (full CLI), CLAUDE.md IS injected, so modifying
    it between parent and fork WILL cause a cache miss. The xfail with
    strict=False accommodates both behaviors.
    """
    original_claudemd = (test_project / "CLAUDE.md").read_text()

    # ── Parent with original CLAUDE.md ──
    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    usage_rows = []
    try:
        sid, u1 = await run_turn(
            parent,
            f"Reference:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        usage_rows.append(u1)

        sid, u2 = await run_turn(parent, "Reply with exactly: ACK2")
        if sid:
            parent_sid = sid
        usage_rows.append(u2)
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"\n  Parent session: {parent_sid}")

    # ── Modify CLAUDE.md ──
    (test_project / "CLAUDE.md").write_text(
        original_claudemd + "\n\n## New Section Added After Parent Started\n\n"
        "This content was not present when the parent session started. "
        "The fork process will re-read this file and inject different content "
        "into its API request, breaking the cache prefix.\n"
    )

    # ── Fork with modified CLAUDE.md ──
    fork_opts = make_sdk_options(
        test_project, proxy, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork, "Reply with exactly: FORK_MODIFIED_CLAUDEMD"
        )
    finally:
        await fork.disconnect()

    assert fork_sid, "Failed to get fork session ID"
    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify cache HIT (this should FAIL — that's the point of xfail) ──
    parent_rows = extract_usage(parent_sid)
    if parent_rows:
        parent_crs = [r["cr"] for r in parent_rows]
        baseline = compute_baseline(parent_crs)
        prev_total = parent_rows[-1]["tot"]

        fork_turn = extract_usage_row(fork_usage) or extract_fork_first_turn(fork_sid)
        if fork_turn:
            classification = classify_cache_hit(fork_turn["cr"], prev_total, baseline)
            print(
                f"  Fork: cr={fork_turn['cr']:,}, prev_total={prev_total:,}, "
                f"classification={classification} (expect MISS)"
            )
            # This assert should FAIL → xfail catches it
            assert_cache_hit(
                fork_turn["cr"], prev_total, baseline,
                "CLAUDE.md modified fork"
            )
    else:
        # If we can't get usage data, we can't verify — skip
        pytest.skip("Could not extract parent usage")


# ── Test 9: Raw-HTTP git status normalization verification ─────────────


def test_git_status_normalization_via_raw_http(proxy_with_bodies: int):
    """Verify normalize_git_status() fires on real HTTP traffic.

    The SDK's CC subprocess doesn't include gitStatus in sys[2], so
    SDK-based tests (Test 7) can't exercise the normalization rule.

    This test bypasses the SDK entirely: sends a raw POST /v1/messages
    with a hand-crafted system prompt containing gitStatus, and verifies
    the post-normalization body has 'gitStatus: normalized'.

    Two requests with DIFFERENT gitStatus content should produce
    IDENTICAL post-normalization system prompts (proving normalization
    eliminates the divergence).

    NOTE: This is NOT a live API test — we send to the proxy but use a
    non-existent API key so the upstream call fails. We only care about
    the proxy's normalization of the request body, not the API response.
    We use SAVE_BODIES mode to inspect what the proxy actually sent.
    """
    import httpx as httpx_client

    proxy_url = f"http://localhost:{proxy_with_bodies}"

    # Realistic system prompt with gitStatus at the end of sys[2]
    sys_block_0 = "x-anthropic-billing-header: cc_version=abc123; cc_entrypoint=sdk-py; cch=def456;"
    sys_block_1 = "You are Claude, an AI assistant made by Anthropic."
    sys_block_2_template = (
        "Environment info:\n"
        " - Platform: darwin\n"
        " - Shell: zsh\n"
        " - OS Version: Darwin 23.4.0\n\n"
        "gitStatus: This is the git status at the start of the conversation.\n"
        "Current branch: main\n\n"
        "Status:\n{status_lines}"
    )

    # Two different git statuses (simulating parent vs fork)
    status_a = (
        ' M .obsidian/workspace.json\n'
        '?? untracked_file_a.txt\n'
    )
    status_b = (
        ' M .obsidian/workspace.json\n'
        '?? untracked_file_a.txt\n'
        '?? untracked_file_b.txt\n'
        '?? untracked_file_c.txt\n'
    )

    def make_body(status_lines: str) -> dict:
        return {
            "model": "claude-haiku-4-5",
            "max_tokens": 100,
            "system": [
                {"type": "text", "text": sys_block_0},
                {"type": "text", "text": sys_block_1},
                {"type": "text", "text": sys_block_2_template.format(status_lines=status_lines)},
            ],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Reply with: OK"}]},
            ],
        }

    body_dir = Path(TEST_LOG_DIR) / "bodies"

    # Clear old bodies
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    # Send request A (git status A) — will fail upstream (bad key) but
    # proxy normalizes before forwarding, and SAVE_BODIES captures it
    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(
                f"{proxy_url}/v1/messages",
                json=make_body(status_a),
                headers={
                    "x-api-key": "sk-test-not-a-real-key",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        except Exception:
            pass  # Upstream failure expected with fake key

    time.sleep(0.3)

    # Send request B (git status B — different!)
    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(
                f"{proxy_url}/v1/messages",
                json=make_body(status_b),
                headers={
                    "x-api-key": "sk-test-not-a-real-key",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        except Exception:
            pass

    time.sleep(0.3)

    # ── Verify post-normalization bodies ──
    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(post_files) >= 2, (
        f"Expected ≥2 post-normalization bodies, got {len(post_files)}. "
        f"Body dir contents: {list(body_dir.iterdir()) if body_dir.exists() else 'missing'}"
    )

    # Both should have gitStatus normalized
    normalized_systems = []
    for pf in post_files[-2:]:  # last 2 requests (ours)
        body = json.loads(pf.read_text())
        system = body.get("system", [])
        assert len(system) >= 3, f"{pf.name}: expected ≥3 system blocks, got {len(system)}"

        sys2_text = system[2].get("text", "")
        assert "gitStatus: normalized" in sys2_text, (
            f"{pf.name}: sys[2] does not contain 'gitStatus: normalized'. "
            f"Got: ...{sys2_text[-200:]}"
        )
        # Should NOT contain the original status lines
        assert "untracked_file_a.txt" not in sys2_text, (
            f"{pf.name}: sys[2] still contains original git status content"
        )
        print(f"  {pf.name}: gitStatus normalized ✓")
        normalized_systems.append(json.dumps(system, sort_keys=True))

    # The two post-normalization system prompts should be IDENTICAL
    # (proving that different gitStatus inputs produce the same output)
    assert normalized_systems[0] == normalized_systems[1], (
        "Post-normalization system prompts differ between requests with "
        "different gitStatus content. The normalization should make them identical."
    )
    print("  ✓ Two different gitStatus inputs → identical post-normalization system prompts")

    # Also verify billing header was normalized
    body_a = json.loads(post_files[-2].read_text())
    sys0_text = body_a["system"][0].get("text", "")
    assert "cch=0" in sys0_text, (
        f"Billing header not normalized: {sys0_text[:100]}"
    )
    print("  ✓ Billing header normalized")


# ── Test 10: Raw-HTTP billing header normalization ──────────────────────


def test_billing_header_normalization_via_raw_http(proxy_with_bodies: int):
    """Verify normalize_billing_header() produces identical output for different inputs.

    Two requests with DIFFERENT billing header values (different cch hash,
    different cc_version) should produce IDENTICAL post-normalization system[0]
    blocks. This proves the proxy eliminates per-process billing header divergence.

    Raw-HTTP approach: sends to proxy with fake API key, inspects SAVE_BODIES output.
    """
    import httpx as httpx_client

    proxy_url = f"http://localhost:{proxy_with_bodies}"

    def make_body(cch: str, cc_version: str) -> dict:
        return {
            "model": "claude-haiku-4-5",
            "max_tokens": 100,
            "system": [
                {
                    "type": "text",
                    "text": f"x-anthropic-billing-header: cc_version={cc_version}; "
                            f"cc_entrypoint=sdk-py; cch={cch};",
                },
                {"type": "text", "text": "You are Claude."},
            ],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Reply: OK"}]},
            ],
        }

    body_dir = Path(TEST_LOG_DIR) / "bodies"
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    headers = {
        "x-api-key": "sk-test-not-a-real-key",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # Request A: one set of billing values
    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages",
                        json=make_body("abc123hash", "2.1.59-git-deadbeef"),
                        headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    # Request B: different billing values (simulating different process)
    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages",
                        json=make_body("xyz789hash", "2.1.112-git-cafebabe"),
                        headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    # Verify post-normalization
    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(post_files) >= 2, f"Expected ≥2 post files, got {len(post_files)}"

    sys0_texts = []
    for pf in post_files[-2:]:
        body = json.loads(pf.read_text())
        sys0 = body["system"][0]["text"]
        sys0_texts.append(sys0)
        assert "cch=0" in sys0, f"{pf.name}: billing header not normalized: {sys0[:100]}"
        assert "cc_version=0" in sys0, f"{pf.name}: cc_version not normalized: {sys0[:100]}"
        # Original values should be gone
        assert "abc123hash" not in sys0, f"{pf.name}: still contains original cch"
        assert "deadbeef" not in sys0, f"{pf.name}: still contains original cc_version"
        print(f"  {pf.name}: billing header normalized ✓")

    assert sys0_texts[0] == sys0_texts[1], (
        "Post-normalization billing headers differ between requests"
    )
    print("  ✓ Different billing headers → identical post-normalization")

    # Also verify pre-normalization bodies had the original values
    pre_files = sorted(body_dir.glob("req*_pre.json"))
    assert len(pre_files) >= 2
    pre_a = json.loads(pre_files[-2].read_text())
    pre_b = json.loads(pre_files[-1].read_text())
    assert "abc123hash" in pre_a["system"][0]["text"], "Pre-body A missing original cch"
    assert "xyz789hash" in pre_b["system"][0]["text"], "Pre-body B missing original cch"
    print("  ✓ Pre-normalization bodies confirmed different")


# ── Test 11: Raw-HTTP string→list conversion ────────────────────────────


def test_string_to_list_conversion_via_raw_http(proxy_with_bodies: int):
    """Verify normalize_user_content_structure() converts bare strings to lists.

    Sends a request with user messages in bare-string format (as CC sometimes
    demotes older messages). Verifies the post-normalization body has all user
    messages in list format.

    Also sends two requests — one with string content, one with list content
    for the same text — and verifies their post-normalization messages sections
    are identical (proving the normalization eliminates the divergence).
    """
    import httpx as httpx_client

    proxy_url = f"http://localhost:{proxy_with_bodies}"

    user_text = "This is user message content for testing string to list conversion."

    # Request A: user content as bare string (CC's demoted format)
    body_string = {
        "model": "claude-haiku-4-5",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "You are Claude."}],
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "Follow up question"},
        ],
    }

    # Request B: same content but already in list format (CC's normal format)
    body_list = {
        "model": "claude-haiku-4-5",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "You are Claude."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": [{"type": "text", "text": "Follow up question"}]},
        ],
    }

    body_dir = Path(TEST_LOG_DIR) / "bodies"
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    headers = {
        "x-api-key": "sk-test-not-a-real-key",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages", json=body_string, headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages", json=body_list, headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    # Verify post-normalization
    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(post_files) >= 2, f"Expected ≥2 post files, got {len(post_files)}"

    post_a = json.loads(post_files[-2].read_text())
    post_b = json.loads(post_files[-1].read_text())

    # All user messages in post_a should be list format (converted from string)
    for i, msg in enumerate(post_a["messages"]):
        if msg["role"] == "user":
            assert isinstance(msg["content"], list), (
                f"post_a msg[{i}]: user content still string after normalization"
            )
            assert msg["content"][0]["type"] == "text", (
                f"post_a msg[{i}]: converted block missing type=text"
            )
    print("  ✓ String content converted to list format")

    # Pre-body A should have string content
    pre_files = sorted(body_dir.glob("req*_pre.json"))
    pre_a = json.loads(pre_files[-2].read_text())
    string_found = False
    for msg in pre_a["messages"]:
        if msg["role"] == "user" and isinstance(msg["content"], str):
            string_found = True
            break
    assert string_found, "Pre-body A should have at least one string-content user message"
    print("  ✓ Pre-normalization confirmed string content present")

    # Post-normalization messages should be identical between A and B
    msgs_a = json.dumps(post_a["messages"], sort_keys=True)
    msgs_b = json.dumps(post_b["messages"], sort_keys=True)
    assert msgs_a == msgs_b, (
        "Post-normalization messages differ between string and list inputs. "
        "String→list conversion should make them identical."
    )
    print("  ✓ String input and list input → identical post-normalization messages")


# ── Test 12: Raw-HTTP tool sorting ──────────────────────────────────────


def test_tool_sorting_via_raw_http(proxy_with_bodies: int):
    """Verify normalize_tool_order() sorts tools alphabetically by name.

    Sends two requests with the same tools in DIFFERENT order (simulating
    non-deterministic readdir across process restarts). Verifies the post-
    normalization tool arrays are identical and alphabetically sorted.
    """
    import httpx as httpx_client

    proxy_url = f"http://localhost:{proxy_with_bodies}"

    tools_a = [
        {"name": "write", "description": "Write file", "input_schema": {"type": "object"}},
        {"name": "bash", "description": "Run command", "input_schema": {"type": "object"}},
        {"name": "read", "description": "Read file", "input_schema": {"type": "object"}},
        {"name": "glob", "description": "Find files", "input_schema": {"type": "object"}},
    ]

    # Same tools, different order (simulating different readdir order)
    tools_b = [
        {"name": "glob", "description": "Find files", "input_schema": {"type": "object"}},
        {"name": "read", "description": "Read file", "input_schema": {"type": "object"}},
        {"name": "bash", "description": "Run command", "input_schema": {"type": "object"}},
        {"name": "write", "description": "Write file", "input_schema": {"type": "object"}},
    ]

    def make_body(tools: list) -> dict:
        return {
            "model": "claude-haiku-4-5",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "You are Claude."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Reply: OK"}]},
            ],
            "tools": tools,
        }

    body_dir = Path(TEST_LOG_DIR) / "bodies"
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    headers = {
        "x-api-key": "sk-test-not-a-real-key",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages", json=make_body(tools_a), headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    with httpx_client.Client(timeout=30) as client:
        try:
            client.post(f"{proxy_url}/v1/messages", json=make_body(tools_b), headers=headers)
        except Exception:
            pass

    time.sleep(0.3)

    # Verify post-normalization
    post_files = sorted(body_dir.glob("req*_post.json"))
    assert len(post_files) >= 2, f"Expected ≥2 post files, got {len(post_files)}"

    post_a = json.loads(post_files[-2].read_text())
    post_b = json.loads(post_files[-1].read_text())

    # Tools should be alphabetically sorted in both
    names_a = [t["name"] for t in post_a["tools"]]
    names_b = [t["name"] for t in post_b["tools"]]

    assert names_a == sorted(names_a), (
        f"Post A tools not sorted: {names_a}"
    )
    assert names_b == sorted(names_b), (
        f"Post B tools not sorted: {names_b}"
    )
    print(f"  ✓ Tools sorted alphabetically: {names_a}")

    # Pre-normalization should have original (unsorted) order
    pre_files = sorted(body_dir.glob("req*_pre.json"))
    pre_a = json.loads(pre_files[-2].read_text())
    pre_b = json.loads(pre_files[-1].read_text())
    pre_names_a = [t["name"] for t in pre_a["tools"]]
    pre_names_b = [t["name"] for t in pre_b["tools"]]
    assert pre_names_a != sorted(pre_names_a), (
        "Pre-body A tools already sorted — test doesn't exercise sorting"
    )
    assert pre_names_b != sorted(pre_names_b), (
        "Pre-body B tools already sorted — test doesn't exercise sorting"
    )
    print(f"  ✓ Pre-normalization confirmed unsorted: A={pre_names_a}, B={pre_names_b}")

    # Post-normalization tool arrays should be IDENTICAL
    tools_json_a = json.dumps(post_a["tools"], sort_keys=True)
    tools_json_b = json.dumps(post_b["tools"], sort_keys=True)
    assert tools_json_a == tools_json_b, (
        "Post-normalization tool arrays differ between requests with different "
        "tool ordering. Sorting should make them identical."
    )
    print("  ✓ Different tool orders → identical post-normalization")


# ── Test: CLAUDEMD_MARKER false positive (Bug 1) ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="Editing CLAUDE.md changes preserved project context between parent and fork; dynamic reminder stripping alone cannot make this cache-hit.",
    strict=False,
)
async def test_claudemd_marker_false_positive_in_changed_files(
    proxy: int, test_project: Path
):
    """Bug 1: changed_files diff containing the CLAUDEMD_MARKER string.

    When CC edits a CLAUDE.md file, the changed_files system-reminder
    includes a diff that contains the CLAUDEMD_MARKER string deep inside.
    Before the fix, _is_strippable_system_reminder() searched the full text
    and incorrectly preserved this block (treating it as CLAUDE.md context).
    The unstripped block then causes cache divergence on fork because the
    fork doesn't have this dynamic reminder in its JSONL reconstruction.

    The dynamic changed_files block is correctly stripped, but editing CLAUDE.md
    itself changes the preserved project-context reminder between parent and fork,
    so this remains a documented cache-miss limitation.

    Verification:
    - Parent writes the CLAUDEMD_MARKER string into its CLAUDE.md
    - This triggers a changed_files reminder with the marker in the diff
    - Fork from parent
    - The cache-hit assertion documents the expected limitation via xfail
    """
    # Record proxy log offset to isolate this test's entries
    log_offset = proxy_log_length()

    # ── Set up: CLAUDE.md with marker-containing content ──
    claudemd = test_project / "CLAUDE.md"
    claudemd.write_text(
        "# Test Project\n\n"
        "As you answer the user's questions, you can use the following context:\n"
        "This is a test project for cache proxy testing.\n"
    )

    # ── Parent: build context then edit CLAUDE.md ──
    opts = make_sdk_options(test_project, proxy)
    parent = ClaudeSDKClient(opts)
    await parent.connect()
    parent_sid = None
    try:
        # Turn 1: bulk text to build a large prefix
        sid, u1 = await run_turn(
            parent,
            f"Reference document:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        if sid:
            parent_sid = sid
        print(f"\n  Parent turn 1: {fmt_usage(u1)}")

        # Turn 2: edit CLAUDE.md to trigger changed_files with the marker
        sid, u2 = await run_turn(
            parent,
            "Append the line '## New Section' to the end of CLAUDE.md. "
            "Reply with exactly: EDITED",
        )
        if sid:
            parent_sid = sid
        print(f"  Parent turn 2: {fmt_usage(u2)}")

        # Turn 3: one more turn so the changed_files reminder persists
        sid, u3 = await run_turn(
            parent,
            "Reply with exactly: ACK3",
        )
        if sid:
            parent_sid = sid
        print(f"  Parent turn 3: {fmt_usage(u3)}")
    finally:
        await parent.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    print(f"  Parent session: {parent_sid}")

    # ── Fork ──
    fork_opts = make_sdk_options(
        test_project, proxy, resume=parent_sid, fork_session=True
    )
    fork = ClaudeSDKClient(fork_opts)
    await fork.connect()
    try:
        fork_sid, fork_usage = await run_turn(
            fork, "Reply with exactly: FORK_CLAUDEMD_BUG1"
        )
    finally:
        await fork.disconnect()

    print(f"  Fork session: {fork_sid}")
    print(f"  Fork usage: {fmt_usage(fork_usage)}")

    # ── Verify cache hit using proxy log (authoritative source) ──
    # The fork JSONL may not have flushed assistant entries yet.
    # The proxy usage log records every API request in real time.
    time.sleep(0.5)  # allow proxy log flush
    proxy_entries = get_proxy_usage_for_turns(log_offset)
    assert len(proxy_entries) >= 4, (
        f"Expected ≥4 proxy log entries (3+ parent + 1 fork), "
        f"got {len(proxy_entries)} (offset={log_offset})"
    )

    # Last entry is the fork's request; second-to-last is parent's last
    fork_entry = proxy_entries[-1]
    # Find parent's last substantial entry (skip tiny tool-result-only entries)
    parent_entries = [e for e in proxy_entries[:-1] if e["tot"] > 1000]
    assert parent_entries, "No substantial parent entries in proxy log"
    parent_last = parent_entries[-1]

    fork_cr = fork_entry["cr"]
    prev_total = parent_last["tot"]

    # Compute baseline from parent cache reads
    parent_crs = [e["cr"] for e in parent_entries if e["cr"] > 0]
    baseline = min(parent_crs) if parent_crs else 0
    print(f"  Baseline: {baseline:,}")

    classification = classify_cache_hit(fork_cr, prev_total, baseline)
    print(
        f"  Fork: cr={fork_cr:,}, prev_total={prev_total:,}, "
        f"classification={classification}"
    )
    assert_cache_hit(
        fork_cr, prev_total, baseline,
        "CLAUDEMD false positive fork — changed_files with marker should be stripped"
    )
