"""Real API E2E tests for OBS Agent daemon.

These tests start a REAL uvicorn server and send REAL HTTP requests,
hitting the REAL Claude API. No mocks, no patches, no fakes.

Requirements:
- ANTHROPIC_API_KEY env var must be set
- All tests marked @pytest.mark.e2e
- Uses e2e_config fixture from conftest.py

Run with: .venv/bin/pytest tests/test_real_api.py -v -m e2e
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import uvicorn

from obs_agent.daemon import create_app

# NOTE: SDK uses subscription auth, NOT API key. No skip gating needed.


def _free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_e2e_server(e2e_config):
    """Start a real uvicorn server on a random port with the e2e vault config.

    No mocks -- lets all requests hit the real Claude API.
    Yields the base URL (e.g. http://127.0.0.1:PORT).
    """
    port = _free_port()
    app = create_app(e2e_config)
    server_config = uvicorn.Config(
        app=app, host="127.0.0.1", port=port, log_level="error"
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
        pytest.fail("Server failed to start")

    yield base_url

    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        serve_task.cancel()


@pytest.mark.e2e
class TestRealAPI:
    """Tests that hit the real Claude API through a live uvicorn server."""

    async def test_real_chat_response(self, live_e2e_server):
        """Send a message via real HTTP, get non-empty response from real API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": "Say hello in exactly one word."},
                timeout=60.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data
        assert len(data["response"]) > 0

    async def test_real_session_id_captured(self, live_e2e_server):
        """First message returns a session_id from the real API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": "Say hi."},
                timeout=60.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("session_id") is not None
        assert len(data["session_id"]) > 0

    async def test_real_sse_streaming(self, live_e2e_server):
        """SSE stream returns data: lines with real content."""
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{live_e2e_server}/chat/stream",
                json={"message": "Say hello in one word."},
                timeout=60.0,
            ) as resp:
                assert resp.status_code == 200
                lines = []
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        lines.append(line)

        assert len(lines) >= 2  # at least one content line + [DONE]
        assert any(l != "data: [DONE]" for l in lines)  # has real content
        assert lines[-1] == "data: [DONE]"

    async def test_real_session_resume(self, live_e2e_server):
        """Second message works (session resume)."""
        async with httpx.AsyncClient() as client:
            # First message
            resp1 = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": "Remember the word 'banana'. Just say OK."},
                timeout=60.0,
            )
            assert resp1.status_code == 200

            # Second message
            resp2 = await client.post(
                f"{live_e2e_server}/chat",
                json={"message": "What word did I ask you to remember?"},
                timeout=60.0,
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert len(data2["response"]) > 0
