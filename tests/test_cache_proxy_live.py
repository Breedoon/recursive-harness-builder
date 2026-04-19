"""Core cache hit tests for the cache-normalizing proxy.

Tests verify that the proxy enables reliable prompt cache sharing across
session forks. Uses claude-haiku-4-5 for cost efficiency.

All cache verification uses the adjusted metric from
.claude/skills/jsonl-analysis/SKILL.md: subtract the globally-cached
system prompt baseline, classify as HIT/MISS/EDGE.

Requires: proxy running (via `proxy` fixture), API keys in .env.
"""
from __future__ import annotations

import asyncio
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
    ClaudeSDKClient,
    assert_cache_hit,
    classify_cache_hit,
    compute_baseline,
    extract_fork_first_turn,
    extract_usage,
    fmt_usage,
    get_proxy_usage_for_turns,
    make_sdk_options,
    proxy_log_length,
    run_turn,
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


# ── Helpers ───────────────────────────────────────────────────────────────


async def _run_parent_session(
    proxy_port: int,
    project_dir,
    num_turns: int = 3,
) -> tuple[str, list[dict], int]:
    """Run a parent session with bulk text turns.

    Returns (session_id, proxy_usage_rows, proxy_log_start_offset).
    Usage comes from the proxy log (reliable) since the SDK only returns
    real usage on the first turn within a client connection.
    """
    log_start = proxy_log_length()
    opts = make_sdk_options(project_dir, proxy_port)
    client = ClaudeSDKClient(opts)
    await client.connect()
    parent_sid = None
    try:
        prompts = [
            f"Reference document A:\n\n{BULK_TEXT}\n\nReply with exactly one word: READY",
            f"Reference document B:\n\n{BULK_TEXT}\n\nReply with exactly one word: ACK",
            "Reply with exactly one word: DONE",
        ][:num_turns]
        for prompt in prompts:
            sid, _usage = await run_turn(client, prompt)
            if sid:
                parent_sid = sid
    finally:
        await client.disconnect()

    assert parent_sid, "Failed to get parent session ID"
    # Get usage from proxy log (reliable source)
    usage_rows = get_proxy_usage_for_turns(start_offset=log_start)
    return parent_sid, usage_rows, log_start


async def _run_fork_session(
    parent_sid: str,
    proxy_port: int,
    project_dir,
    prompt: str = "Reply with exactly one word: FORKED",
) -> tuple[str, dict]:
    """Fork from parent and run one turn. Returns (fork_session_id, usage).

    Usage comes from the proxy log for the fork's single API request.
    """
    log_start = proxy_log_length()
    opts = make_sdk_options(
        project_dir, proxy_port, resume=parent_sid, fork_session=True
    )
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        fork_sid, _usage = await run_turn(client, prompt)
    finally:
        await client.disconnect()

    assert fork_sid, f"Failed to get fork session ID (parent={parent_sid})"
    # Get usage from proxy log
    fork_usage_rows = get_proxy_usage_for_turns(start_offset=log_start)
    usage = fork_usage_rows[0] if fork_usage_rows else {}
    return fork_sid, usage


# ── Test 1: Within-process cache hits ─────────────────────────────────────


async def test_parent_session_proxy_passthrough(proxy, test_project):
    """Verify the proxy doesn't break normal session operation.

    Runs a parent session with bulk text through the proxy. Verifies:
    1. The session completes successfully
    2. At least one API request went through the proxy
    3. The proxy's normalizations don't cause errors

    NOTE: Within-process cache hits are CC's own mechanism. The proxy's
    value is cross-process (fork/resume) cache sharing — see subsequent tests.
    CC batches multiple turns into a single API call within one process,
    so the proxy only sees 1 request for a multi-turn session.
    """
    parent_sid, usage_rows, _ = await _run_parent_session(proxy, test_project)

    print(f"\n  Parent session: {parent_sid}")
    for i, u in enumerate(usage_rows):
        print(f"  Turn {i}: {fmt_usage(u)}")

    # At least 1 API request should have gone through the proxy
    assert len(usage_rows) >= 1, (
        f"Expected at least 1 proxy log entry, got {len(usage_rows)}"
    )

    # The first request should have meaningful token counts
    first = usage_rows[0]
    first_tot = first.get("tot", 0)
    assert first_tot > 0, f"First proxy request had zero tokens: {first}"
    print(f"  Proxy working: {first_tot:,} total tokens in first request")


# ── Test 2: Fork hits parent cache ────────────────────────────────────────


async def test_fork_hits_parent_cache(proxy, test_project):
    """Primary proxy validation: fork's first turn should hit parent's cache.

    Parent session runs 3 turns with bulk text. Fork from parent.
    The fork's first turn should read the parent's prefix from cache,
    confirmed via the adjusted metric (proxy log data).
    """
    parent_sid, parent_usage, _ = await _run_parent_session(proxy, test_project)

    print(f"\n  Parent session: {parent_sid}")
    for i, u in enumerate(parent_usage):
        print(f"  Parent turn {i}: {fmt_usage(u)}")

    # Parent's last turn total (= fork's expected prefix)
    parent_last_tot = parent_usage[-1].get("tot", 0)

    # Fork
    fork_sid, fork_usage = await _run_fork_session(parent_sid, proxy, test_project)
    print(f"\n  Fork session: {fork_sid}")
    print(f"  Fork turn 0: {fmt_usage(fork_usage)}")

    cr = fork_usage.get("cr", 0)

    # Baseline from parent cache reads
    parent_crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    assert_cache_hit(cr, parent_last_tot, baseline, label="fork first turn")


# ── Test 3: Fork from fork (3-level chain) ────────────────────────────────


async def test_fork_from_fork_hits_cache(proxy, test_project):
    """Three-level deep fork chain: Parent → Fork1 → Fork2.

    Fork2 should hit Fork1's cache (which was normalized from parent).
    Verifies normalization stability across one level of indirection.
    """
    parent_sid, parent_usage, _ = await _run_parent_session(proxy, test_project)
    print(f"\n  Parent: {parent_sid}")

    # Fork1 from parent
    fork1_sid, fork1_usage = await _run_fork_session(
        parent_sid, proxy, test_project, prompt="Reply with exactly: FORK1"
    )
    print(f"  Fork1: {fork1_sid} — {fmt_usage(fork1_usage)}")
    fork1_tot = fork1_usage.get("tot", 0)

    # Fork2 from Fork1
    fork2_sid, fork2_usage = await _run_fork_session(
        fork1_sid, proxy, test_project, prompt="Reply with exactly: FORK2"
    )
    print(f"  Fork2: {fork2_sid} — {fmt_usage(fork2_usage)}")

    cr = fork2_usage.get("cr", 0)
    parent_crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    assert_cache_hit(cr, fork1_tot, baseline, label="fork2 (3-level chain)")


# ── Test 4: Fork from fork from fork (4-level chain) ─────────────────────


async def test_fork_from_fork_from_fork_hits_cache(proxy, test_project):
    """Four-level deep fork chain: Parent → F1 → F2 → F3.

    Verifies normalization remains stable through deep chains.
    Each fork should hit its parent's cache.
    """
    parent_sid, parent_usage, _ = await _run_parent_session(proxy, test_project)
    print(f"\n  Parent: {parent_sid}")

    parent_crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(parent_crs)
    print(f"  Baseline: {baseline:,}")

    prev_sid = parent_sid
    prev_tot = parent_usage[-1].get("tot", 0)

    # Chain: F1, F2, F3
    for level in range(1, 4):
        fork_sid, fork_usage = await _run_fork_session(
            prev_sid, proxy, test_project,
            prompt=f"Reply with exactly: FORK{level}",
        )
        print(f"  F{level}: {fork_sid} — {fmt_usage(fork_usage)}")

        cr = fork_usage.get("cr", 0)
        assert_cache_hit(cr, prev_tot, baseline, label=f"F{level} (4-level chain)")

        prev_sid = fork_sid
        prev_tot = fork_usage.get("tot", 0)


# ── Test 5: Parallel forks from same parent ───────────────────────────────


async def test_parallel_forks_from_same_parent(proxy, test_project):
    """Two forks from the same parent should both hit cache independently.

    Verifies that the proxy's normalizations are deterministic — two
    independent forks from the same parent produce identical normalized
    requests and both match the parent's cache.
    """
    parent_sid, parent_usage, _ = await _run_parent_session(proxy, test_project)
    print(f"\n  Parent: {parent_sid}")

    parent_crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(parent_crs)
    parent_last_tot = parent_usage[-1].get("tot", 0)
    print(f"  Baseline: {baseline:,}, parent last total: {parent_last_tot:,}")

    # Launch two forks concurrently
    results = await asyncio.gather(
        _run_fork_session(
            parent_sid, proxy, test_project,
            prompt="Reply with exactly: FORK_A",
        ),
        _run_fork_session(
            parent_sid, proxy, test_project,
            prompt="Reply with exactly: FORK_B",
        ),
    )

    for label, (fork_sid, fork_usage) in zip(["A", "B"], results):
        print(f"\n  Fork {label}: {fork_sid} — {fmt_usage(fork_usage)}")
        cr = fork_usage.get("cr", 0)
        assert_cache_hit(
            cr, parent_last_tot, baseline,
            label=f"parallel fork {label}",
        )


# ── Test 6: Fork after delay within TTL ───────────────────────────────────


async def test_fork_after_delay_within_ttl(proxy, test_project):
    """Fork with a 15-second delay — cache should still be valid within TTL.

    In production, forks often happen seconds to minutes after the parent
    completes. The minimum cache TTL is 5 minutes, so a 15-second delay
    should be well within bounds.
    """
    parent_sid, parent_usage, _ = await _run_parent_session(proxy, test_project)
    print(f"\n  Parent: {parent_sid}")

    parent_crs = [u.get("cr", 0) for u in parent_usage]
    baseline = compute_baseline(parent_crs)
    parent_last_tot = parent_usage[-1].get("tot", 0)

    delay = 15
    print(f"  Waiting {delay}s before forking...")
    await asyncio.sleep(delay)

    fork_sid, fork_usage = await _run_fork_session(parent_sid, proxy, test_project)
    print(f"  Fork (after {delay}s): {fork_sid} — {fmt_usage(fork_usage)}")

    cr = fork_usage.get("cr", 0)
    assert_cache_hit(
        cr, parent_last_tot, baseline,
        label=f"fork after {delay}s delay",
    )
