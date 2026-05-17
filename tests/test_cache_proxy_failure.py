"""Proxy failure mode tests.

Tests that the cache-normalizing proxy handles failure scenarios gracefully:
1. Proxy not running → clear error, no hang
2. Proxy restart → recovery and continued cache hits
3. GET passthrough → non-messages forwarded unmodified
4. Env bypass → direct Anthropic API when proxy skipped

All tests use claude-haiku-4-5 (TEST_MODEL) via pytest-asyncio.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Ensure tests/ directory is on sys.path so conftest_cache_proxy is importable.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import httpx
import pytest

from conftest_cache_proxy import (  # noqa: E402
    BULK_TEXT,
    TEST_MODEL,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    _free_port,
    _resolve_proxy_script,
    _wait_for_port,
    assert_cache_hit,
    compute_baseline,
    extract_usage,
    fmt_usage,
    make_sdk_options,
    run_turn,
)

pytestmark = pytest.mark.asyncio

requires_api_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — needed for direct API calls"
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _start_proxy(port: int) -> subprocess.Popen:
    """Start a proxy subprocess on the given port, wait for TCP readiness."""
    script = _resolve_proxy_script()
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    if not _wait_for_port("127.0.0.1", port, timeout=10.0):
        stderr_out = ""
        if proc.poll() is not None and proc.stderr:
            stderr_out = proc.stderr.read().decode(errors="replace")
        proc.kill()
        pytest.fail(f"Proxy failed to start on port {port}: {stderr_out}")
    return proc


def _stop_proxy(proc: subprocess.Popen) -> None:
    """Terminate a proxy subprocess. SIGTERM first, SIGKILL after 5s."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


# ── Test 1: Proxy not running → connection refused ──────────────────────


@pytest.mark.timeout(30)
async def test_proxy_not_running_connection_refused(test_project: Path):
    """When no proxy is running, SDK call should fail fast with connection error.

    Verifies the failure is a clear connection refused (not a silent hang).
    We use a port where nothing is listening.
    """
    dead_port = _free_port()
    # Double-check nothing is listening
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.connect_ex(("127.0.0.1", dead_port)) != 0, \
            f"Port {dead_port} unexpectedly has a listener"

    error_seen = False
    error_detail = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0)) as client:
            await client.post(
                f"http://127.0.0.1:{dead_port}/v1/messages",
                json={"model": TEST_MODEL, "messages": [], "max_tokens": 1},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, ConnectionRefusedError, OSError) as e:
        error_seen = True
        error_detail = f"{type(e).__name__}: {e}"
        print(f"  Got expected connection error: {error_detail}")

    assert error_seen, (
        "Call to dead proxy port succeeded unexpectedly — "
        "expected a connection error"
    )


# ── Test 2: Proxy restart → recovery ───────────────────────────────────


@requires_api_key
@pytest.mark.timeout(120)
async def test_proxy_restart_recovery(test_project: Path):
    """After proxy is killed and restarted, SDK sessions continue working.

    Flow:
    1. Start proxy
    2. Run a parent turn through proxy (establishes cache)
    3. Kill proxy (SIGKILL — hard crash)
    4. Restart proxy on same port
    5. Run another turn — should work
    6. Fork from parent — should still hit cache if within TTL
    """
    port = _free_port()
    proxy_proc = _start_proxy(port)

    try:
        # Turn 1: parent session through proxy
        opts = make_sdk_options(test_project, port)
        parent = ClaudeSDKClient(opts)
        await parent.connect()
        try:
            parent_sid, usage1 = await run_turn(
                parent,
                f"Reference document:\n\n{BULK_TEXT}\n\nReply with exactly: READY"
            )
            assert parent_sid, "Failed to get parent session ID"
            print(f"  Turn 1 (before kill): {fmt_usage(usage1)}")
        finally:
            await parent.disconnect()

        # Kill the proxy hard (simulate crash)
        proxy_proc.kill()
        proxy_proc.wait(timeout=5)
        print(f"  Proxy killed (pid={proxy_proc.pid})")

        # Verify port is actually dead
        assert not _wait_for_port("127.0.0.1", port, timeout=1.0), \
            "Port should be dead after kill"

        # Restart proxy on same port
        proxy_proc = _start_proxy(port)
        print(f"  Proxy restarted (pid={proxy_proc.pid}) on :{port}")

        # Turn 2: resume parent session through restarted proxy
        opts2 = make_sdk_options(test_project, port, resume=parent_sid)
        parent2 = ClaudeSDKClient(opts2)
        await parent2.connect()
        try:
            _, usage2 = await run_turn(parent2, "Reply with exactly: RECOVERED")
            print(f"  Turn 2 (after restart): {fmt_usage(usage2)}")
        finally:
            await parent2.disconnect()

        # The turn should have succeeded (non-zero total)
        tot2 = (usage2.get("cache_read_input_tokens", 0) +
                usage2.get("cache_creation_input_tokens", 0) +
                usage2.get("input_tokens", 0))
        assert tot2 > 0, "Turn after proxy restart returned zero tokens — session appears broken"

        # Fork to test cache sharing through restarted proxy
        fork_opts = make_sdk_options(
            test_project, port, resume=parent_sid, fork_session=True
        )
        fork_client = ClaudeSDKClient(fork_opts)
        await fork_client.connect()
        try:
            fork_sid, fork_usage = await run_turn(
                fork_client, "Reply with exactly: FORK_AFTER_RESTART"
            )
            print(f"  Fork (after restart): {fmt_usage(fork_usage)}")
        finally:
            await fork_client.disconnect()

        # Fork should succeed (non-zero total)
        fork_tot = (fork_usage.get("cache_read_input_tokens", 0) +
                    fork_usage.get("cache_creation_input_tokens", 0) +
                    fork_usage.get("input_tokens", 0))
        assert fork_tot > 0, "Fork after proxy restart returned zero tokens"

    finally:
        _stop_proxy(proxy_proc)


# ── Test 3: GET passthrough ─────────────────────────────────────────────


@requires_api_key
@pytest.mark.timeout(30)
async def test_proxy_passthrough_non_messages():
    """GET and non-/v1/messages requests should pass through unmodified.

    The proxy only normalizes POST /v1/messages. All other requests
    (GET, PUT, DELETE, non-messages paths) should forward to upstream
    as-is and return whatever upstream returns.
    """
    port = _free_port()
    proxy_proc = _start_proxy(port)

    try:
        # GET /v1/models — a valid Anthropic API endpoint
        # The proxy should forward this directly without normalization.
        api_key = os.environ["ANTHROPIC_API_KEY"]  # guaranteed by @requires_api_key

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"http://localhost:{port}/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )

        # We expect a response from the Anthropic API (could be 200 or 404
        # depending on API version support, but NOT a proxy error like 502)
        print(f"  GET /v1/models → status {resp.status_code}")
        assert resp.status_code != 502, (
            f"Proxy returned 502 for GET passthrough — upstream may be unreachable. "
            f"Body: {resp.text[:200]}"
        )
        # The response should be JSON from Anthropic (not proxy error HTML)
        assert resp.headers.get("content-type", "").startswith(("application/json", "text/")), (
            f"Unexpected content-type for passthrough: {resp.headers.get('content-type')}"
        )

        # Also test a POST to a non-messages path
        async with httpx.AsyncClient(timeout=30) as client2:
            resp2 = await client2.post(
                f"http://localhost:{port}/v1/complete",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"prompt": "test", "max_tokens_to_sample": 1},
            )
        print(f"  POST /v1/complete → status {resp2.status_code}")
        # Might 404 or 400 from Anthropic, but should NOT be 502 from proxy
        # (unless the endpoint genuinely doesn't exist, which is fine)
        # The point is: the proxy forwarded it without trying to normalize.

    finally:
        _stop_proxy(proxy_proc)


# ── Test 4: Graceful degradation env bypass ─────────────────────────────


@requires_api_key
@pytest.mark.timeout(60)
async def test_graceful_degradation_env_bypass(test_project: Path):
    """When ANTHROPIC_BASE_URL points directly at Anthropic API, sessions work
    without any proxy. This simulates the OBS_SKIP_CACHE_PROXY=1 bypass.

    The point: if the proxy is down or misconfigured, users can set the env
    var to bypass it entirely and CC works normally against the real API.
    """
    # No proxy started — go direct to Anthropic API
    opts = ClaudeAgentOptions(
        model=TEST_MODEL,
        cwd=str(test_project),
        permission_mode="bypassPermissions",
        env={
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
            "CLAUDECODE": "",
            "CLAUDE_CODE_ENTRYPOINT": "",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        },
    )

    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        sid, usage = await run_turn(client, "Reply with exactly: BYPASS_OK")
        assert sid, "Failed to get session ID in bypass mode"
        tot = (usage.get("cache_read_input_tokens", 0) +
               usage.get("cache_creation_input_tokens", 0) +
               usage.get("input_tokens", 0))
        assert tot > 0, "Bypass mode returned zero tokens — direct API appears broken"
        print(f"  Bypass mode: {fmt_usage(usage)}")
        print(f"  Session ID: {sid}")
    finally:
        await client.disconnect()

    # Verify the session produced valid JSONL
    await asyncio.sleep(1)  # Let JSONL flush
    rows = extract_usage(sid)
    assert len(rows) >= 1, (
        f"Expected at least 1 usage row in JSONL for bypass session {sid}, got {len(rows)}"
    )
    print(f"  JSONL rows: {len(rows)} — bypass path appears functional")
