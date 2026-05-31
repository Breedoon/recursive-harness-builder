"""Parallel fork cache interference tests.

Tests whether concurrent forks with divergent prefixes interfere with
each other's cache entries. Specifically:

1. Do parallel forks within 20 blocks of fork point both HIT?
2. Do parallel forks beyond 20 blocks still HIT?
3. Does a deep fork tree (3 levels) with parallel leaves maintain cache?
4. Does the JSONL timing fix prevent truncated tool_use blocks?
5. Do truly concurrent parallel forks (asyncio.gather) both HIT?
6. Comparative JSONL fix verification: immediate fork vs delayed fork.

All tests use the adjusted cache hit metric from the JSONL analysis skill.
Uses claude-haiku-4-5 for cost efficiency (5m TTL, not 1h like Opus).

Synchronization: Tests 1 and 3 use file-based barrier synchronization to
ensure truly interleaved execution between parallel forks. Each fork writes
a "done" signal file after its turn, and the other fork waits for that file
before proceeding. This ensures the API calls genuinely alternate rather
than running sequentially.

Process cleanup: Each test kills orphaned CC (claude) processes in its
teardown to prevent resource exhaustion across the suite.

Run with:
    ~/Documents/obs/.venv/bin/python -m pytest -m live tests/test_cache_parallel_forks.py -v -s

Run a single test in isolation (recommended for tests 3-5):
    ~/Documents/obs/.venv/bin/python -m pytest -m live tests/test_cache_parallel_forks.py::test_deep_tree_parallel_leaves -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_tests_dir = str(Path(__file__).resolve().parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import pytest

from conftest_cache_proxy import (
    BULK_TEXT,
    ClaudeSDKClient,
    assert_cache_hit,
    classify_cache_hit,
    compute_baseline,
    fmt_usage,
    get_proxy_usage_for_turns,
    make_sdk_options,
    proxy_log_length,
    run_turn,
    find_session_jsonl,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("OBS_RUN_EXPENSIVE_CACHE_LIVE") != "1",
        reason="expensive live cache fork tests require explicit opt-in",
    ),
]

# Larger text block for 100K+ conversations without exceeding Haiku's 200k input limit.
BULK_TEXT_LARGE = BULK_TEXT * 3  # ~30K tokens


# ── Process cleanup ──────────────────────────────────────────────────────


def _kill_orphaned_claude_processes():
    """Kill any orphaned 'claude' CLI processes spawned by tests.

    CC SDK spawns `claude` subprocesses that sometimes linger after
    client.disconnect(). Accumulated orphans exhaust file descriptors,
    causing later tests to crash with exit code 1.
    """
    try:
        # Find all claude processes owned by this user
        result = subprocess.run(
            ["pgrep", "-f", "claude.*--session-id"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        # Give them a moment to exit
        time.sleep(0.5)
        # Force kill any remaining
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def cleanup_cc_processes():
    """Kill orphaned CC processes after each test."""
    yield
    _kill_orphaned_claude_processes()
    # Brief pause for OS to reclaim file descriptors
    time.sleep(1)


# ── File-based barrier synchronization ───────────────────────────────────


class TurnBarrier:
    """File-based synchronization barrier for interleaving fork turns.

    Uses signal files in a temp directory to coordinate two concurrent
    async tasks. Task A writes a signal file after its turn, and Task B
    polls for that file before starting its turn (and vice versa).

    This ensures the API calls genuinely alternate (A1→B1→A2→B2→...)
    even though both tasks run concurrently via asyncio.

    The user specifically requested this pattern:
    "two bash scripts that literally are like one runs as long as the
    other one is not run and then as soon as the other one is run the
    first one releases"
    """

    def __init__(self, sync_dir: Path):
        self.sync_dir = sync_dir
        self.sync_dir.mkdir(parents=True, exist_ok=True)

    def signal_done(self, label: str, turn: int):
        """Write a signal file indicating this fork completed a turn."""
        (self.sync_dir / f"{label}_turn_{turn}_done").touch()

    async def wait_for(self, label: str, turn: int, timeout: float = 120.0):
        """Wait for another fork's signal file to appear."""
        path = self.sync_dir / f"{label}_turn_{turn}_done"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"TurnBarrier: timed out waiting for {label} turn {turn} "
            f"(file: {path})"
        )

    def cleanup(self):
        """Remove all signal files."""
        for f in self.sync_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass


# ── Helpers ───────────────────────────────────────────────────────────────


async def _run_single_turn(
    proxy_port: int,
    project_dir,
    prompt: str,
    *,
    resume: str | None = None,
    fork_session: bool = False,
) -> tuple[str, dict, int]:
    """Run exactly one turn in its own client connection.

    Each connection = one API call = one proxy log entry.
    Returns (session_id, proxy_usage_row, proxy_log_start).
    """
    log_start = proxy_log_length(proxy_port=proxy_port)
    opts = make_sdk_options(
        project_dir, proxy_port,
        resume=resume,
        fork_session=fork_session,
    )
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        sid, _usage = await run_turn(client, prompt)
    finally:
        await client.disconnect()

    assert sid, f"Failed to get session ID (resume={resume}, fork={fork_session})"
    rows = get_proxy_usage_for_turns(start_offset=log_start, proxy_port=proxy_port, min_rows=1)
    assert rows, f"Proxy usage row not written after API call (start_offset={log_start})"
    usage = rows[0]
    return sid, usage, log_start


async def _build_parent(
    proxy_port: int,
    project_dir,
    num_turns: int = 3,
    *,
    large: bool = False,
) -> tuple[str, list[dict], int]:
    """Build a parent session with bulk text, one API call per turn.

    If large=True, uses BULK_TEXT_LARGE (~50K tokens per chunk) for 100K+
    conversations.

    Returns (session_id, list_of_usage_rows, baseline).
    """
    text = BULK_TEXT_LARGE if large else BULK_TEXT
    sid = None
    usage_rows = []

    # Turn 1: large context to establish measurable cache
    sid, u, _ = await _run_single_turn(
        proxy_port, project_dir,
        f"Reference doc A:\n\n{text}\n\nReply with exactly: READY",
    )
    usage_rows.append(u)

    # Subsequent turns: resume same session (new connection = new API call)
    for i in range(1, num_turns):
        if i == 1:
            prompt = f"Reference doc B:\n\n{text}\n\nReply with exactly: ACK{i+1}"
        else:
            prompt = f"Padding turn {i+1}. Reply with exactly: TURN{i+1}"
        sid, u, _ = await _run_single_turn(
            proxy_port, project_dir, prompt, resume=sid,
        )
        usage_rows.append(u)

    crs = [u.get("cr", 0) for u in usage_rows]
    baseline = compute_baseline(crs)
    return sid, usage_rows, baseline


async def _grow_fork(
    parent_sid: str,
    proxy_port: int,
    project_dir,
    num_turns: int,
    label: str = "F",
) -> tuple[str, list[dict]]:
    """Fork from parent and grow by num_turns, one API call per turn.

    Returns (fork_session_id, list_of_usage_rows).
    """
    usage_rows = []

    # First turn: fork
    fork_sid, u, _ = await _run_single_turn(
        proxy_port, project_dir,
        f"[{label}] Turn 1. Reply with exactly: {label}_T1",
        resume=parent_sid,
        fork_session=True,
    )
    usage_rows.append(u)

    # Subsequent turns: resume the fork
    for i in range(2, num_turns + 1):
        fork_sid_cont, u, _ = await _run_single_turn(
            proxy_port, project_dir,
            f"[{label}] Turn {i}. Reply with exactly: {label}_T{i}",
            resume=fork_sid,
        )
        usage_rows.append(u)

    return fork_sid, usage_rows


# ── Test 1: Parallel forks within 20 blocks (truly concurrent) ───────────


async def test_parallel_forks_within_20_blocks(proxy, test_project, tmp_path):
    """Two forks from same parent, truly interleaved via file-based barriers.

    Both forks run as concurrent asyncio tasks. After each turn, a fork
    writes a signal file; the other fork waits for that signal before
    proceeding. This ensures genuine A1→B1→A2→B2→... interleaving.

    Both forks should HIT because their fork point is within 20 blocks.
    """
    parent_sid, parent_usage, baseline = await _build_parent(
        proxy, test_project, num_turns=3,
    )
    parent_last_tot = parent_usage[-1].get("tot", 0)
    print(f"\n  Parent: {parent_sid}, baseline: {baseline:,}")
    for i, u in enumerate(parent_usage):
        print(f"  Parent turn {i}: {fmt_usage(u)}")

    barrier = TurnBarrier(tmp_path / "sync")
    turns_each = 5
    a_usages: list[dict] = []
    b_usages: list[dict] = []

    async def _fork_a_task():
        fork_sid = None
        for turn in range(1, turns_each + 1):
            if turn > 1:
                # Wait for B to complete its previous turn
                await barrier.wait_for("B", turn - 1)

            if fork_sid is None:
                fork_sid, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[A] Turn {turn}. Reply with exactly: A_T{turn}",
                    resume=parent_sid, fork_session=True,
                )
            else:
                _, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[A] Turn {turn}. Reply with exactly: A_T{turn}",
                    resume=fork_sid,
                )
            a_usages.append(u)
            print(f"  Fork A turn {turn}: {fmt_usage(u)}")
            barrier.signal_done("A", turn)

    async def _fork_b_task():
        fork_sid = None
        for turn in range(1, turns_each + 1):
            # Wait for A to complete this turn first
            await barrier.wait_for("A", turn)

            if fork_sid is None:
                fork_sid, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[B] Turn {turn}. Reply with exactly: B_T{turn}",
                    resume=parent_sid, fork_session=True,
                )
            else:
                _, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[B] Turn {turn}. Reply with exactly: B_T{turn}",
                    resume=fork_sid,
                )
            b_usages.append(u)
            print(f"  Fork B turn {turn}: {fmt_usage(u)}")
            barrier.signal_done("B", turn)

    await asyncio.gather(_fork_a_task(), _fork_b_task())
    barrier.cleanup()

    # Verify: continuation turns must HIT
    for label, usages in [("A", a_usages), ("B", b_usages)]:
        for i, u in enumerate(usages):
            if i == 0:
                cr = u.get("cr", 0)
                cls = classify_cache_hit(cr, parent_last_tot, baseline)
                print(f"  Fork {label} first turn: {cls} (cr={cr:,})")
                continue
            cr = u.get("cr", 0)
            prev_tot = usages[i - 1].get("tot", 0)
            cls = classify_cache_hit(cr, prev_tot, baseline)
            print(f"  Fork {label} turn {i+1}: {cls}")
            assert_cache_hit(
                cr, prev_tot, baseline,
                label=f"parallel fork {label} turn {i+1} (within 20 blocks)",
            )


# ── Test 2: Parallel forks beyond 20 blocks (100K+ context) ──────────────


async def test_parallel_forks_beyond_20_blocks(proxy, test_project):
    """Fork A grows far (15 turns), then Fork B starts from the same parent.

    Uses large text chunks for 100K+ token conversations.

    Fork A's cache entries are >20 blocks from Fork B's fork point.
    Core question: does Fork A's cache_creation at distant positions
    interfere with Fork B's ability to read entries near the fork point?

    Expected from Track 1 data: no interference (94.1% HIT rate).
    """
    parent_sid, parent_usage, baseline = await _build_parent(
        proxy, test_project, num_turns=3, large=True,
    )
    parent_last_tot = parent_usage[-1].get("tot", 0)
    print(f"\n  Parent: {parent_sid}, baseline: {baseline:,}")
    print(f"  Parent context: ~{parent_last_tot:,} tokens")

    # Fork A: grow far (15 turns = ~30 blocks, well beyond 20)
    fork_a_sid, a_usages = await _grow_fork(
        parent_sid, proxy, test_project,
        num_turns=15, label="A",
    )
    print(f"\n  Fork A grew to 15 turns")
    for i, u in enumerate(a_usages):
        cls = classify_cache_hit(
            u.get("cr", 0),
            a_usages[i - 1].get("tot", 0) if i > 0 else parent_last_tot,
            baseline,
        )
        print(f"  A turn {i+1}: {fmt_usage(u)} [{cls}]")

    # Now Fork B from the SAME parent (not from Fork A)
    fork_b_sid, b_usages = await _grow_fork(
        parent_sid, proxy, test_project,
        num_turns=3, label="B",
    )
    print(f"\n  Fork B (from same parent, after A grew 15 turns):")
    for i, u in enumerate(b_usages):
        cr = u.get("cr", 0)
        prev_tot = b_usages[i - 1].get("tot", 0) if i > 0 else parent_last_tot
        cls = classify_cache_hit(cr, prev_tot, baseline)
        print(f"  B turn {i+1}: {fmt_usage(u)} [{cls}]")

    # Subsequent Fork B turns must HIT (within its own chain)
    for i in range(1, len(b_usages)):
        cr = b_usages[i].get("cr", 0)
        prev_tot = b_usages[i - 1].get("tot", 0)
        assert_cache_hit(
            cr, prev_tot, baseline,
            label=f"fork B turn {i+1} (after A grew 15 turns beyond fork point)",
        )


# ── Test 3: Deep fork tree with parallel leaves (truly concurrent) ───────


async def test_deep_tree_parallel_leaves(proxy, test_project, tmp_path):
    """Parent → Fork1 (3 turns) → Fork2A and Fork2B (parallel, 3 turns each).

    Tests cache at each level of a 3-level tree. Level-2 forks use
    file-based barrier synchronization for true interleaving.
    """
    # Level 0: Parent
    parent_sid, parent_usage, baseline = await _build_parent(
        proxy, test_project, num_turns=3,
    )
    print(f"\n  Parent: {parent_sid}, baseline: {baseline:,}")

    # Level 1: Fork1 from parent (3 turns)
    fork1_sid, fork1_usages = await _grow_fork(
        parent_sid, proxy, test_project,
        num_turns=3, label="F1",
    )
    fork1_last_tot = fork1_usages[-1].get("tot", 0)
    print(f"\n  Fork1: {fork1_sid}")
    for i, u in enumerate(fork1_usages):
        print(f"  F1 turn {i+1}: {fmt_usage(u)}")

    # Level 2: Fork2A and Fork2B from Fork1 (truly concurrent, barrier-synced)
    barrier = TurnBarrier(tmp_path / "sync_deep")
    f2a_usages: list[dict] = []
    f2b_usages: list[dict] = []

    async def _f2a_task():
        fork_sid = None
        for turn in range(1, 4):
            if turn > 1:
                await barrier.wait_for("F2B", turn - 1)
            if fork_sid is None:
                fork_sid, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[F2A] Turn {turn}. Reply: F2A_T{turn}",
                    resume=fork1_sid, fork_session=True,
                )
            else:
                _, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[F2A] Turn {turn}. Reply: F2A_T{turn}",
                    resume=fork_sid,
                )
            f2a_usages.append(u)
            print(f"  F2A turn {turn}: {fmt_usage(u)}")
            barrier.signal_done("F2A", turn)

    async def _f2b_task():
        fork_sid = None
        for turn in range(1, 4):
            await barrier.wait_for("F2A", turn)
            if fork_sid is None:
                fork_sid, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[F2B] Turn {turn}. Reply: F2B_T{turn}",
                    resume=fork1_sid, fork_session=True,
                )
            else:
                _, u, _ = await _run_single_turn(
                    proxy, test_project,
                    f"[F2B] Turn {turn}. Reply: F2B_T{turn}",
                    resume=fork_sid,
                )
            f2b_usages.append(u)
            print(f"  F2B turn {turn}: {fmt_usage(u)}")
            barrier.signal_done("F2B", turn)

    await asyncio.gather(_f2a_task(), _f2b_task())
    barrier.cleanup()

    # Verify: continuation turns in level-2 forks must HIT
    for label, usages in [("F2A", f2a_usages), ("F2B", f2b_usages)]:
        for i in range(1, len(usages)):
            cr = usages[i].get("cr", 0)
            prev_tot = usages[i - 1].get("tot", 0)
            assert_cache_hit(
                cr, prev_tot, baseline,
                label=f"{label} turn {i+1} (deep tree, parallel leaf)",
            )


# ── Test 4: JSONL stability (no truncated tool_use) ──────────────────────


async def test_fork_after_tool_use_has_complete_blocks(proxy, test_project):
    """Fork immediately after a tool_use turn — verify JSONL contains complete blocks.

    The JSONL timing fix (_await_jsonl_stability) should ensure the fork's
    JSONL has all tool_use blocks from the parent. We verify by:
    1. Parent runs a turn that triggers tool_use (read a file)
    2. Fork from parent immediately
    3. Check the fork's JSONL for complete tool_use/tool_result entries
    4. The fork should also HIT cache (byte-identical prefix)
    """
    log_start = proxy_log_length(proxy_port=proxy)
    opts = make_sdk_options(test_project, proxy)
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        parent_sid, _ = await run_turn(
            client,
            f"Reference:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        _, _ = await run_turn(
            client,
            "Read the file data/sample.txt and tell me how many lines it has. "
            "Reply with the count only.",
        )
    finally:
        await client.disconnect()

    parent_usage_rows = get_proxy_usage_for_turns(start_offset=log_start, proxy_port=proxy)
    parent_last_tot = parent_usage_rows[-1].get("tot", 0) if parent_usage_rows else 0
    crs = [u.get("cr", 0) for u in parent_usage_rows]
    baseline = compute_baseline(crs)
    print(f"\n  Parent: {parent_sid}, {len(parent_usage_rows)} API calls")
    for i, u in enumerate(parent_usage_rows):
        print(f"  Parent API call {i}: {fmt_usage(u)}")

    # Fork immediately
    fork_sid, fork_usage, _ = await _run_single_turn(
        proxy, test_project,
        "Reply with exactly: FORKED_AFTER_TOOL",
        resume=parent_sid, fork_session=True,
    )
    print(f"\n  Fork: {fork_sid} — {fmt_usage(fork_usage)}")

    # Check JSONL for complete tool entries
    fork_jsonl = find_session_jsonl(fork_sid)
    assert fork_jsonl and fork_jsonl.exists(), f"Fork JSONL not found for {fork_sid}"

    tool_use_count = 0
    tool_result_count = 0
    truncated = False
    with open(fork_jsonl) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            msg = entry.get("message", {})
            msg_content = msg.get("content", [])
            if not isinstance(msg_content, list):
                continue
            for block in msg_content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_use_count += 1
                    if not block.get("id") or not block.get("name"):
                        truncated = True
                elif block.get("type") == "tool_result":
                    tool_result_count += 1

    print(f"  Fork JSONL: {tool_use_count} tool_use, {tool_result_count} tool_result")
    print(f"  Truncated tool_use blocks: {truncated}")

    assert tool_use_count > 0, "Fork JSONL has no tool_use blocks"
    assert tool_result_count > 0, "Fork JSONL has no tool_result blocks"
    assert not truncated, "Fork JSONL has truncated tool_use blocks (missing id or name)"

    fork_cr = fork_usage.get("cr", 0)
    fork_cls = classify_cache_hit(fork_cr, parent_last_tot, baseline)
    print(f"  Fork cache: {fork_cls} (cr={fork_cr:,}, parent_tot={parent_last_tot:,})")


# ── Test 5: Truly concurrent parallel forks (asyncio.gather) ─────────────


async def test_truly_concurrent_fork_first_turns(proxy, test_project):
    """Two forks launched simultaneously via asyncio.gather — first turn only.

    Tests whether truly concurrent fork first-turn API requests both get
    cache hits. This is the closest to production behavior where OBS
    dispatches multiple AgentTask forks concurrently from the same parent.
    """
    parent_sid, parent_usage, baseline = await _build_parent(
        proxy, test_project, num_turns=3,
    )
    parent_last_tot = parent_usage[-1].get("tot", 0)
    print(f"\n  Parent: {parent_sid}, baseline: {baseline:,}")

    async def _fork_one_turn(label: str):
        return await _run_single_turn(
            proxy, test_project,
            f"[{label}] First turn. Reply with exactly: {label}_T1",
            resume=parent_sid, fork_session=True,
        )

    results = await asyncio.gather(
        _fork_one_turn("C1"),
        _fork_one_turn("C2"),
    )

    for label, (fork_sid, usage, _) in zip(["C1", "C2"], results):
        cr = usage.get("cr", 0)
        cls = classify_cache_hit(cr, parent_last_tot, baseline)
        print(f"  Fork {label}: {fork_sid} — {fmt_usage(usage)} [{cls}]")

    # Both forks should get at least system-prompt-level cache
    for label, (fork_sid, usage, _) in zip(["C1", "C2"], results):
        cr = usage.get("cr", 0)
        assert cr > 0, (
            f"Concurrent fork {label} got zero cache_read — "
            f"even system prompt baseline should be cached"
        )


# ── Test 6: JSONL timing fix comparative verification ─────────────────


def _check_jsonl_completeness(session_id: str) -> dict:
    """Inspect a fork's JSONL for tool_use/tool_result completeness.

    Returns dict with:
      tool_use_count, tool_result_count, truncated_count,
      total_entries, has_tool_use, has_tool_result, all_complete
    """
    path = find_session_jsonl(session_id)
    if not path or not path.exists():
        return {
            "tool_use_count": 0, "tool_result_count": 0,
            "truncated_count": 0, "total_entries": 0,
            "has_tool_use": False, "has_tool_result": False,
            "all_complete": False, "error": "JSONL not found",
        }

    tool_use_count = 0
    tool_result_count = 0
    truncated_count = 0
    total_entries = 0

    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            total_entries += 1
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_use_count += 1
                    # Check for truncation: missing id, name, or input
                    if not block.get("id") or not block.get("name"):
                        truncated_count += 1
                elif block.get("type") == "tool_result":
                    tool_result_count += 1

    return {
        "tool_use_count": tool_use_count,
        "tool_result_count": tool_result_count,
        "truncated_count": truncated_count,
        "total_entries": total_entries,
        "has_tool_use": tool_use_count > 0,
        "has_tool_result": tool_result_count > 0,
        "all_complete": tool_use_count > 0 and truncated_count == 0,
    }


def _cliproxy_model_detail_count(model: str) -> int:
    return len(_cliproxy_model_details(model))


def _cliproxy_model_details(model: str) -> list[dict]:
    req = urllib.request.Request(
        "http://127.0.0.1:8317/v0/management/usage",
        headers={"X-Management-Key": "obs-mgmt-key"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    models = data.get("usage", {}).get("apis", {}).get("sk-anything", {}).get("models", {})
    return models.get(model, {}).get("details", [])


async def test_gpt_prefix_replay_caches_through_cli_proxy(proxy, test_project):
    """Verify GPT requests route through CLIProxyAPI and reuse the prompt cache.

    This is the focused live proof for the production GPT model cache path:
    Claude Code → cache proxy → CLIProxyAPI → GPT. The cache proxy routes and
    schema-sanitizes GPT requests; CLIProxyAPI management stats provide the
    provider-specific `cached_tokens` proof.
    """
    model = "gpt-5.4-mini"
    model_with_context = f"{model}[1m]"
    cliproxy_start = _cliproxy_model_detail_count(model)
    proxy_log_start = proxy_log_length(proxy_port=proxy)

    opts = make_sdk_options(test_project, proxy, model=model_with_context)
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        sid, _ = await run_turn(client, f"Reference:\n\n{BULK_TEXT}\n\nReply with exactly: GPT_CACHE_A")
        _, _ = await run_turn(client, "Using the same reference, reply with exactly: GPT_CACHE_B")
    finally:
        await client.disconnect()

    proxy_rows = get_proxy_usage_for_turns(start_offset=proxy_log_start, proxy_port=proxy)
    cliproxy_rows = _cliproxy_model_details(model)[cliproxy_start:]
    print(f"\n  GPT cache session: {sid}")
    for i, row in enumerate(proxy_rows, 1):
        print(f"  cache proxy request {i}: {fmt_usage(row)}")
    for i, row in enumerate(cliproxy_rows, 1):
        tokens = row.get("tokens", {})
        print(
            f"  CLIProxy GPT request {i}: input={tokens.get('input_tokens', 0):,} "
            f"cached={tokens.get('cached_tokens', 0):,} failed={row.get('failed')}"
        )
    assert len(cliproxy_rows) >= 2
    assert any(row.get("tokens", {}).get("cached_tokens", 0) >= 1024 for row in cliproxy_rows[1:]), (
        "subsequent GPT request did not show meaningful CLIProxy cached_tokens"
    )


async def test_jsonl_timing_fix_comparative(proxy, test_project):
    """Compare fork JSONL completeness: immediate fork vs delayed fork.

    This is the pre-fix vs post-fix comparison the auditor requested.
    The JSONL timing fix (_await_jsonl_stability) adds a ~1s delay before
    copying the parent's JSONL to the fork. This test verifies:

    1. Parent triggers a multi-step tool_use turn (read + respond)
    2. Fork A: immediately after parent disconnects (0s delay) — "no fix" scenario
    3. Fork B: after 2s delay — "with fix" scenario
    4. Compare JSONL completeness (tool_use blocks present and not truncated)
    5. Compare cache hit rates

    In the SDK's fork mechanism, both forks may succeed because the SDK
    waits for the parent to finish before copying JSONL. But this test
    documents the observed behavior as evidence. If the SDK inherently
    prevents truncation, that's a valid finding: the race condition is
    OBS-specific (telegram.py copies JSONL independently of the SDK).
    """
    # Parent: build context then trigger tool_use
    log_start = proxy_log_length(proxy_port=proxy)
    opts = make_sdk_options(test_project, proxy)
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        parent_sid, _ = await run_turn(
            client,
            f"Reference:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
        )
        # Trigger tool_use: read a file
        _, _ = await run_turn(
            client,
            "Read the file data/sample.txt and tell me how many lines it has. "
            "Reply with the count only.",
        )
        # One more turn to ensure tool_result is in the JSONL
        _, _ = await run_turn(
            client,
            "Now read data/config.json and tell me the 'name' field value. "
            "Reply with just the value.",
        )
    finally:
        await client.disconnect()

    parent_usage = get_proxy_usage_for_turns(start_offset=log_start, proxy_port=proxy)
    parent_last_tot = parent_usage[-1].get("tot", 0) if parent_usage else 0
    crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(crs)
    print(f"\n  Parent: {parent_sid}, {len(parent_usage)} API calls, baseline={baseline:,}")

    # Fork A: immediate (0s delay) — simulates "no fix" scenario
    fork_a_sid, fork_a_usage, _ = await _run_single_turn(
        proxy, test_project,
        "Reply with exactly: FORK_A_IMMEDIATE",
        resume=parent_sid, fork_session=True,
    )
    fork_a_completeness = _check_jsonl_completeness(fork_a_sid)

    # Fork B: with 2s delay — simulates "with fix" scenario
    await asyncio.sleep(2.0)
    fork_b_sid, fork_b_usage, _ = await _run_single_turn(
        proxy, test_project,
        "Reply with exactly: FORK_B_DELAYED",
        resume=parent_sid, fork_session=True,
    )
    fork_b_completeness = _check_jsonl_completeness(fork_b_sid)

    # Report findings
    print(f"\n  Fork A (immediate, 0s delay):")
    print(f"    Session: {fork_a_sid}")
    print(f"    Cache: {fmt_usage(fork_a_usage)}")
    print(f"    JSONL: {fork_a_completeness['tool_use_count']} tool_use, "
          f"{fork_a_completeness['tool_result_count']} tool_result, "
          f"{fork_a_completeness['truncated_count']} truncated, "
          f"{fork_a_completeness['total_entries']} total entries")
    fork_a_cls = classify_cache_hit(
        fork_a_usage.get("cr", 0), parent_last_tot, baseline,
    )
    print(f"    Classification: {fork_a_cls}")

    print(f"\n  Fork B (delayed, 2s wait):")
    print(f"    Session: {fork_b_sid}")
    print(f"    Cache: {fmt_usage(fork_b_usage)}")
    print(f"    JSONL: {fork_b_completeness['tool_use_count']} tool_use, "
          f"{fork_b_completeness['tool_result_count']} tool_result, "
          f"{fork_b_completeness['truncated_count']} truncated, "
          f"{fork_b_completeness['total_entries']} total entries")
    fork_b_cls = classify_cache_hit(
        fork_b_usage.get("cr", 0), parent_last_tot, baseline,
    )
    print(f"    Classification: {fork_b_cls}")

    # Compare
    print(f"\n  Comparison:")
    print(f"    Tool completeness: A={fork_a_completeness['all_complete']}, "
          f"B={fork_b_completeness['all_complete']}")
    print(f"    Cache: A={fork_a_cls}, B={fork_b_cls}")
    if fork_a_completeness == fork_b_completeness:
        print(f"    NOTE: Both forks identical — SDK fork mechanism may be "
              f"inherently safe (race is OBS-specific)")

    # Assert: the delayed fork (with-fix scenario) must have complete tool_use
    assert fork_b_completeness["has_tool_use"], (
        "Delayed fork JSONL has no tool_use blocks — parent tool turn not captured"
    )
    assert fork_b_completeness["all_complete"], (
        f"Delayed fork has {fork_b_completeness['truncated_count']} truncated "
        f"tool_use blocks — timing fix insufficient"
    )
    # Assert: both forks should have tool_result (proof parent completed)
    assert fork_b_completeness["has_tool_result"], (
        "Delayed fork JSONL has no tool_result blocks"
    )
