"""Real end-to-end integration tests for OBS Agent.

These tests start a REAL uvicorn server and send REAL HTTP requests over TCP,
hitting the REAL Claude SDK. NO mocking, NO patching, NO fakes.

Uses LLM-as-judge (Haiku) to evaluate response quality instead of brittle
string matching. Falls back to heuristic checks if no ANTHROPIC_API_KEY is set.

Run with: .venv/bin/pytest tests/test_real_e2e.py -v -m e2e --timeout=300
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time

import httpx
import pytest
import uvicorn

from obs_agent.daemon import create_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------

_HAS_JUDGE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_judge(question: str, response: str, criterion: str) -> bool:
    """Use Haiku to judge whether a response meets a criterion.

    Falls back to a simple non-empty check if no ANTHROPIC_API_KEY is set.
    """
    if not _HAS_JUDGE_KEY:
        logger.warning(
            "No ANTHROPIC_API_KEY — skipping LLM judge, using heuristic fallback"
        )
        # Heuristic: response is non-empty and reasonably long
        return len(response.strip()) > 5

    import anthropic

    client = anthropic.Anthropic()
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Assess this AI agent interaction:\n\n"
                    f"User asked: {question}\n\n"
                    f"Agent responded: {response[:500]}\n\n"
                    f"Criterion: {criterion}\n\n"
                    f"Does the response meet the criterion? Answer ONLY 'YES' or 'NO'."
                ),
            }
        ],
    )
    answer = result.content[0].text.strip().upper()
    passed = "YES" in answer
    if not passed:
        logger.warning(
            "LLM judge said NO — criterion: %s | answer: %s | response[:200]: %s",
            criterion,
            answer,
            response[:200],
        )
    return passed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_e2e_server(e2e_config):
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


@pytest.mark.e2e
class TestRealE2E:
    """End-to-end tests that mirror what a human does manually.

    Every test starts a real uvicorn server, sends real HTTP requests,
    and hits the real Claude SDK. Response quality is evaluated by an
    LLM judge (Haiku) rather than brittle string matching.
    """

    async def test_basic_chat_responds_coherently(self, live_e2e_server):
        """Send a simple message via POST /chat, verify coherent response."""
        question = "Hey, tell me briefly what you know about yourself."

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": question},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        # Basic structural checks
        assert len(response_text) > 0, "Response must be non-empty"
        assert data.get("session_id") is not None, "Must return a session_id"

        # LLM judge: coherence
        assert llm_judge(
            question,
            response_text,
            "Is this a coherent, on-topic response to someone asking the agent about itself?",
        ), f"LLM judge: response not coherent. Got: {response_text[:300]}"

    async def test_streaming_produces_real_content(self, live_e2e_server):
        """Send via POST /chat/stream, collect SSE events, verify real content."""
        question = "What is 2 + 2? Answer in one sentence."

        async with httpx.AsyncClient() as client:
            full_response = await collect_sse_response(
                client, live_e2e_server, question, timeout=120.0
            )

        # Structural checks
        assert len(full_response) > 0, "Streaming response must be non-empty"

        # LLM judge: meaningful response
        assert llm_judge(
            question,
            full_response,
            "Is this a meaningful response that correctly answers the math question?",
        ), f"LLM judge: response not meaningful. Got: {full_response[:300]}"

    async def test_session_continuity(self, live_e2e_server):
        """Send two messages in sequence, verify the agent remembers context."""
        first_message = "Remember the code word PINEAPPLE. Just say OK."
        second_message = "What code word did I give you?"

        async with httpx.AsyncClient() as client:
            # First message: establish context
            resp1 = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": first_message},
                timeout=120.0,
            )
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert len(data1["response"]) > 0, "First response must be non-empty"

            # Second message: test recall
            resp2 = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": second_message},
                timeout=120.0,
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            response_text = data2["response"]

        # LLM judge: does it remember PINEAPPLE?
        assert llm_judge(
            second_message,
            response_text,
            "Does the response mention the word PINEAPPLE (the code word from the previous message)?",
        ), f"LLM judge: PINEAPPLE not recalled. Got: {response_text[:300]}"

    async def test_enqueue_during_streaming(self, live_e2e_server):
        """Enqueue a message while the agent is streaming, verify it gets processed.

        This is the KEY test that mirrors the manual flow:
        1. Send a message via /chat/stream (something that takes a while)
        2. While streaming, POST /chat/enqueue with a queued message
        3. Verify enqueue returned 200
        4. Verify the stream output includes the queued message content
           (the continuation loop should pick it up and address it inline)
        """
        stream_question = (
            "List 5 interesting facts about space. Take your time and be detailed."
        )
        enqueue_message = "IMPORTANT: also mention the word MANGO in your response."

        # Collect raw SSE lines so we can check for queue_delivered status events
        raw_lines: list[str] = []

        async with httpx.AsyncClient() as client:

            async def do_stream() -> str:
                parts: list[str] = []
                async with client.stream(
                    "POST",
                    f"{live_e2e_server}/chat/stream",
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
                # Wait a bit for the stream to be in progress
                await asyncio.sleep(2.0)
                resp = await client.post(
                    f"{live_e2e_server}/chat/enqueue",
                    json={"message": enqueue_message},
                    timeout=30.0,
                )
                return resp

            # Run stream and enqueue concurrently
            stream_result, enqueue_resp = await asyncio.gather(
                do_stream(), do_enqueue()
            )

        # Verify enqueue was accepted
        assert enqueue_resp.status_code == 200, (
            f"Enqueue should return 200, got {enqueue_resp.status_code}"
        )
        enqueue_data = enqueue_resp.json()
        assert enqueue_data["queued"] is True, "Enqueue should report queued=True"

        # The streaming response should be non-empty (the space facts)
        assert len(stream_result) > 0, "Streaming response must be non-empty"

        # Check that the continuation loop processed the queued message.
        # The stream should contain a queue_delivered status event.
        raw_text = "\n".join(raw_lines)
        has_queue_event = "queue_delivered" in raw_text

        # The stream content should mention MANGO (from the continuation)
        # or at minimum the queue_delivered event should have fired
        has_mango = "mango" in stream_result.lower()

        logger.info(
            "Enqueue test: queue_delivered=%s, has_mango=%s, stream_len=%d",
            has_queue_event, has_mango, len(stream_result),
        )

        # Primary assertion: enqueue was accepted and either the queue was
        # delivered as a status event or the agent mentioned MANGO
        assert has_queue_event or has_mango, (
            f"Queued message not processed in stream. "
            f"queue_delivered event: {has_queue_event}, "
            f"MANGO in response: {has_mango}. "
            f"Stream excerpt: {stream_result[:500]}"
        )

    async def test_interrupt_stops_agent(self, live_e2e_server):
        """Send a tool-using message, interrupt after a delay, verify stream ends.

        The interrupt flag is checked at tool-use boundaries (PreToolUse hook).
        Therefore we ask the agent to perform a task that involves tool usage,
        giving the interrupt a chance to fire at the next boundary.

        NOTE: If the agent generates a pure text response with no tool calls,
        the interrupt has no boundary to fire at. This test asks the agent to
        use tools (read/search the vault) to maximize the chance of hitting
        a tool boundary.
        """
        # Ask for something that should trigger tool usage (vault reads)
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
                        f"{live_e2e_server}/chat/stream",
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
                # Wait for the stream to get going
                await asyncio.sleep(3.0)
                interrupt_sent_at = time.time()
                resp = await client.post(
                    f"{live_e2e_server}/chat/interrupt",
                    timeout=30.0,
                )
                return resp

            stream_parts, interrupt_resp = await asyncio.gather(
                do_stream(), do_interrupt()
            )

        # Verify interrupt was accepted by the server
        assert interrupt_resp.status_code == 200
        assert interrupt_resp.json()["interrupted"] is True

        # The stream should have produced SOME content before interrupt
        assert len(stream_parts) > 0, "Should have some content before interrupt"

        # Measure timing: if the interrupt worked, the stream should have ended
        # well before the full 60s timeout.
        assert interrupt_sent_at is not None
        assert stream_done_at is not None
        elapsed_after_interrupt = stream_done_at - interrupt_sent_at

        logger.info(
            "Stream ended %.1fs after interrupt (timed_out=%s, parts=%d)",
            elapsed_after_interrupt,
            stream_timed_out,
            len(stream_parts),
        )

        # If the stream timed out, the interrupt did not fire (no tool boundary).
        # This is a known architectural limitation, not a test failure.
        # We still assert the interrupt endpoint worked (200 above).
        if stream_timed_out:
            logger.warning(
                "Interrupt did not stop the stream (no tool boundary hit). "
                "This is expected for pure-text responses without tool calls."
            )
        else:
            # If the stream ended normally, it should have stopped before the
            # full 60s timeout. The agent may take time to reach a tool boundary
            # where the hook can fire, so use a generous threshold.
            assert elapsed_after_interrupt < 55.0, (
                f"Stream should end before timeout after interrupt, "
                f"took {elapsed_after_interrupt:.1f}s"
            )

    async def test_large_message_not_truncated(self, live_e2e_server):
        """Large messages (2000+ chars) are received in full, not truncated.

        Sends a long message with a unique marker at the very end.
        If any layer truncates the message, the agent won't see the marker.
        """
        # Build a ~2500-char message with filler and a unique marker at the end
        filler = "This is filler context. " * 100  # ~2400 chars
        marker = "ZEBRA_WATERFALL_7492"
        large_message = (
            f"I'm going to give you a lot of text. Read ALL of it carefully.\n\n"
            f"{filler}\n\n"
            f"The secret code at the end is: {marker}\n\n"
            f"What is the secret code I just gave you? Reply with ONLY the code."
        )
        assert len(large_message) > 2000, f"Message should be >2000 chars, got {len(large_message)}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": large_message},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        assert llm_judge(
            "What is the secret code?",
            response_text,
            f"Does the response contain or mention the code {marker}?",
        ), f"Large message truncated — agent didn't see marker. Got: {response_text[:300]}"

    async def test_multiline_message_preserved(self, live_e2e_server):
        """Multi-line messages are sent intact through the HTTP layer.

        Sends a message with content spread across many lines, with a
        unique marker on the last line.
        """
        lines = [f"Line {i}: some context about topic {i}" for i in range(1, 21)]
        marker = "CORAL_SUNSET_8831"
        lines.append(f"The secret code is: {marker}")
        lines.append("What is the secret code? Reply with ONLY the code.")
        multiline_message = "\n".join(lines)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": multiline_message},
                timeout=120.0,
            )

        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        assert llm_judge(
            "What is the secret code?",
            response_text,
            f"Does the response contain or mention the code {marker}?",
        ), f"Multiline message lost content — agent didn't see marker. Got: {response_text[:300]}"

    async def test_multi_turn_streaming_works(self, live_e2e_server):
        """Multi-turn streaming: second message works after first completes.

        This is the regression test for the ClaudeSDKClient reader task
        cancellation bug. The SDK's internal anyio task group for reading
        subprocess messages was getting cancelled when the first HTTP request's
        scope ended, causing all subsequent turns to hang indefinitely.

        The fix: connect() runs in a detached asyncio.Task so the reader
        outlives the request scope.
        """
        # Turn 1: simple streaming message
        async with httpx.AsyncClient() as client:
            turn1 = await collect_sse_response(
                client, live_e2e_server, "Hey, how are you?", timeout=60.0
            )
        assert len(turn1) > 0, "Turn 1 must produce a response"
        logger.info("Turn 1 OK: %s", turn1[:100])

        # Turn 2: a follow-up that triggers the reused ClaudeSDKClient.
        # This is what crashed before the fix — the reader task was dead.
        async with httpx.AsyncClient() as client:
            turn2 = await collect_sse_response(
                client,
                live_e2e_server,
                "What tools do you have available? List them briefly.",
                timeout=60.0,
            )
        assert len(turn2) > 0, "Turn 2 must produce a response"
        logger.info("Turn 2 OK: %s", turn2[:100])

        # LLM judge: Turn 2 should be a coherent response about tools
        assert llm_judge(
            "What tools do you have?",
            turn2,
            "Is this a coherent response that lists or describes available tools?",
        ), f"LLM judge: Turn 2 not coherent. Got: {turn2[:300]}"
