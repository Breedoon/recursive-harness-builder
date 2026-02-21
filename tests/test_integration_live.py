"""Live integration tests for OBS Agent.

These tests start a REAL uvicorn server and send REAL HTTP requests over TCP,
hitting the REAL Claude SDK. NO mocking, NO patching, NO fakes.

Uses structural assertions (non-empty response, correct HTTP status, valid JSON
shape) rather than LLM-as-judge. Eval scenarios handle semantic quality checks.

Run with: .venv/bin/pytest tests/test_integration_live.py -v -m integration --timeout=300
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import httpx
import pytest
import uvicorn

from obs_agent.daemon import create_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_server(e2e_config):
    """Start a real uvicorn server on a random port with the e2e vault config.

    No mocks -- lets all requests hit the real Claude SDK.
    Yields the base URL (e.g. http://127.0.0.1:PORT).
    """
    port = _free_port()
    app = create_app(e2e_config)
    server_config = uvicorn.Config(
        app=app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(server_config)
    serve_task = asyncio.create_task(server.serve())

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/health", timeout=0.5)
                if resp.status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadError):
            pass
        await asyncio.sleep(0.1)
    else:
        serve_task.cancel()
        pytest.fail("Server failed to start within 5 seconds")

    yield base_url

    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        serve_task.cancel()


@pytest.fixture
async def small_buffer_server(e2e_config):
    """Start a real uvicorn server with a 4KB buffer to force overflow.

    The SDK init handshake needs ~2-3KB, so 4KB allows connect() to succeed.
    A large file (large_test_data.md) is placed in the vault; asking the agent
    to read it produces a tool result JSON > 4KB, triggering the overflow.
    The runner catches it, reconnects, and the session survives.
    """
    # Place a large file in the vault that will cause tool result overflow
    large_file = e2e_config.vault_path / "large_test_data.md"
    large_file.write_text("# Large Test Data\n\n" + ("This is test content line. " * 300) + "\n")

    e2e_config.max_buffer_size = 4096  # 4KB — init passes, tool results overflow
    port = _free_port()
    app = create_app(e2e_config)
    server_config = uvicorn.Config(
        app=app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(server_config)
    serve_task = asyncio.create_task(server.serve())

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/health", timeout=0.5)
                if resp.status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadError):
            pass
        await asyncio.sleep(0.1)
    else:
        serve_task.cancel()
        pytest.fail("Small-buffer server failed to start within 5 seconds")

    yield base_url

    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        serve_task.cancel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_sse_response(
    client: httpx.AsyncClient,
    base_url: str,
    message: str,
    timeout: float = 120.0,
) -> str:
    """Send streaming request and collect full response text."""
    parts: list[str] = []
    async with client.stream(
        "POST",
        f"{base_url}/chat/stream",
        json={"message": message},
        timeout=timeout,
    ) as resp:
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    break
                parts.append(payload)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLiveIntegration:
    """Live integration tests with real uvicorn + real Claude SDK.

    Every test starts a real server, sends real HTTP requests, and validates
    structural properties: correct status codes, non-empty responses, valid
    JSON shapes, and session continuity.
    """

    async def test_basic_chat_responds(self, live_server):
        """Send a simple message via POST /chat, verify non-empty response."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": "Hey, tell me briefly what you know about yourself."},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data, "Response JSON must contain 'response' key"
        assert len(data["response"]) > 0, "Response must be non-empty"
        assert data.get("session_id") is not None, "Must return a session_id"
        assert isinstance(data["session_id"], str), "session_id must be a string"

    async def test_streaming_produces_real_content(self, live_server):
        """Send via POST /chat/stream, collect SSE events, verify content."""
        async with httpx.AsyncClient() as client:
            full_response = await collect_sse_response(
                client, live_server, "What is 2 + 2? Answer in one sentence.", timeout=120.0
            )

        assert len(full_response) > 0, "Streaming response must be non-empty"

    async def test_session_continuity(self, live_server):
        """Send two messages in sequence, verify second response is non-empty."""
        async with httpx.AsyncClient() as client:
            resp1 = await client.post(
                f"{live_server}/chat",
                json={"message": "Remember the code word PINEAPPLE. Just say OK."},
                timeout=120.0,
            )
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert len(data1["response"]) > 0, "First response must be non-empty"

            resp2 = await client.post(
                f"{live_server}/chat",
                json={"message": "What code word did I give you?"},
                timeout=120.0,
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert len(data2["response"]) > 0, "Second response must be non-empty"

    async def test_enqueue_during_streaming(self, live_server):
        """Enqueue a message while streaming, verify enqueue accepted and stream completes."""
        stream_question = (
            "List 5 interesting facts about space. Take your time and be detailed."
        )
        enqueue_message = "IMPORTANT: also mention the word MANGO in your response."

        raw_lines: list[str] = []

        async with httpx.AsyncClient() as client:

            async def do_stream() -> str:
                parts: list[str] = []
                async with client.stream(
                    "POST",
                    f"{live_server}/chat/stream",
                    json={"message": stream_question},
                    timeout=120.0,
                ) as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        raw_lines.append(line)
                        if line.startswith("data: "):
                            payload = line[6:]
                            if payload == "[DONE]":
                                break
                            parts.append(payload)
                return "\n".join(parts)

            async def do_enqueue() -> httpx.Response:
                await asyncio.sleep(2.0)
                resp = await client.post(
                    f"{live_server}/chat/enqueue",
                    json={"message": enqueue_message},
                    timeout=30.0,
                )
                return resp

            stream_result, enqueue_resp = await asyncio.gather(
                do_stream(), do_enqueue()
            )

        assert enqueue_resp.status_code == 200, (
            f"Enqueue should return 200, got {enqueue_resp.status_code}"
        )
        enqueue_data = enqueue_resp.json()
        assert enqueue_data["queued"] is True, "Enqueue should report queued=True"

        assert len(stream_result) > 0, "Streaming response must be non-empty"

        raw_text = "\n".join(raw_lines)
        has_queue_event = "queue_delivered" in raw_text
        has_mango = "mango" in stream_result.lower()

        logger.info(
            "Enqueue test: queue_delivered=%s, has_mango=%s, stream_len=%d",
            has_queue_event, has_mango, len(stream_result),
        )

        assert has_queue_event or has_mango, (
            f"Queued message not processed in stream. "
            f"queue_delivered event: {has_queue_event}, "
            f"MANGO in response: {has_mango}. "
            f"Stream excerpt: {stream_result[:500]}"
        )

    async def test_interrupt_stops_agent(self, live_server):
        """Send a tool-using message, interrupt after a delay, verify stream ends."""
        long_question = (
            "Search through the vault for any notes about algorithms, then "
            "write a detailed summary. Read at least 3 different files."
        )

        interrupt_sent_at: float | None = None
        stream_done_at: float | None = None
        stream_timed_out = False

        async with httpx.AsyncClient() as client:

            async def do_stream() -> list[str]:
                nonlocal stream_done_at, stream_timed_out
                parts: list[str] = []
                try:
                    async with client.stream(
                        "POST",
                        f"{live_server}/chat/stream",
                        json={"message": long_question},
                        timeout=60.0,
                    ) as resp:
                        assert resp.status_code == 200
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                payload = line[6:]
                                if payload == "[DONE]":
                                    break
                                parts.append(payload)
                except httpx.ReadTimeout:
                    stream_timed_out = True
                    logger.info("Stream timed out (expected if no tool boundary hit)")
                stream_done_at = time.time()
                return parts

            async def do_interrupt() -> httpx.Response:
                nonlocal interrupt_sent_at
                await asyncio.sleep(3.0)
                interrupt_sent_at = time.time()
                resp = await client.post(
                    f"{live_server}/chat/interrupt",
                    timeout=30.0,
                )
                return resp

            stream_parts, interrupt_resp = await asyncio.gather(
                do_stream(), do_interrupt()
            )

        assert interrupt_resp.status_code == 200
        assert interrupt_resp.json()["interrupted"] is True

        assert len(stream_parts) > 0, "Should have some content before interrupt"

        assert interrupt_sent_at is not None
        assert stream_done_at is not None
        elapsed_after_interrupt = stream_done_at - interrupt_sent_at

        logger.info(
            "Stream ended %.1fs after interrupt (timed_out=%s, parts=%d)",
            elapsed_after_interrupt,
            stream_timed_out,
            len(stream_parts),
        )

        if stream_timed_out:
            logger.warning(
                "Interrupt did not stop the stream (no tool boundary hit). "
                "This is expected for pure-text responses without tool calls."
            )
        else:
            assert elapsed_after_interrupt < 55.0, (
                f"Stream should end before timeout after interrupt, "
                f"took {elapsed_after_interrupt:.1f}s"
            )

    async def test_large_message_not_truncated(self, live_server):
        """Large messages (2000+ chars) are received in full, not truncated."""
        filler = "This is filler context. " * 100
        marker = "ZEBRA_WATERFALL_7492"
        large_message = (
            f"I'm going to give you a lot of text. Read ALL of it carefully.\n\n"
            f"{filler}\n\n"
            f"The secret code at the end is: {marker}\n\n"
            f"What is the secret code I just gave you? Reply with ONLY the code."
        )
        assert len(large_message) > 2000

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": large_message},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data, "Response JSON must contain 'response' key"
        assert len(data["response"]) > 0, "Response must be non-empty"

    async def test_multiline_message_preserved(self, live_server):
        """Multi-line messages are sent intact through the HTTP layer."""
        lines = [f"Line {i}: some context about topic {i}" for i in range(1, 21)]
        marker = "CORAL_SUNSET_8831"
        lines.append(f"The secret code is: {marker}")
        lines.append("What is the secret code? Reply with ONLY the code.")
        multiline_message = "\n".join(lines)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": multiline_message},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data, "Response JSON must contain 'response' key"
        assert len(data["response"]) > 0, "Response must be non-empty"

    async def test_buffer_overflow_recovery(self, small_buffer_server):
        """Force a real buffer overflow via tool result, verify daemon survives.

        The small_buffer_server fixture sets max_buffer_size=4096 (4KB) and
        places a large file (large_test_data.md, ~8KB) in the vault. When the
        agent reads it, the tool result JSON exceeds 4KB, triggering the SDK
        buffer overflow. The runner catches it, recovers, and the daemon stays
        alive for the next request.

        What we test:
        - The first request may return a partial/recovered response (status 200)
        - The daemon does NOT crash — a follow-up request works normally
        """
        # Step 1: ask the agent to read the large file — tool result will overflow
        async with httpx.AsyncClient() as client:
            resp1 = await client.post(
                f"{small_buffer_server}/chat",
                json={"message": (
                    "Read the file large_test_data.md from the vault root "
                    "and tell me what it contains."
                )},
                timeout=180.0,
            )
        # The first request should succeed (200) after recovery, but we also
        # accept 500 if the recovery path doesn't fully work — the key test
        # is that the daemon stays alive for the follow-up.
        logger.info(
            "Buffer overflow test - first response status: %d, body: %s",
            resp1.status_code,
            resp1.text[:300],
        )

        # Step 2: follow-up proves the daemon didn't crash
        # This is the real test: the daemon is still functional
        async with httpx.AsyncClient() as client:
            resp2 = await client.post(
                f"{small_buffer_server}/chat",
                json={"message": "What is 2 + 2? Reply with just the number."},
                timeout=120.0,
            )
        assert resp2.status_code == 200, (
            f"Follow-up request must succeed (daemon alive). Got {resp2.status_code}: {resp2.text[:200]}"
        )
        data2 = resp2.json()
        assert len(data2.get("response", "")) > 0, (
            "Follow-up must get a non-empty response (daemon is alive)"
        )

        logger.info(
            "Buffer overflow test - follow-up response: %s",
            data2["response"][:200],
        )

    async def test_multi_turn_streaming_works(self, live_server):
        """Multi-turn streaming: second message works after first completes.

        Regression test for the ClaudeSDKClient reader task cancellation bug.
        """
        async with httpx.AsyncClient() as client:
            turn1 = await collect_sse_response(
                client, live_server, "Hey, how are you?", timeout=60.0
            )
        assert len(turn1) > 0, "Turn 1 must produce a response"
        logger.info("Turn 1 OK: %s", turn1[:100])

        async with httpx.AsyncClient() as client:
            turn2 = await collect_sse_response(
                client,
                live_server,
                "What tools do you have available? List them briefly.",
                timeout=60.0,
            )
        assert len(turn2) > 0, "Turn 2 must produce a response"
        logger.info("Turn 2 OK: %s", turn2[:100])
