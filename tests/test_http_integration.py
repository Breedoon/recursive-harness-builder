"""Real HTTP integration tests for OBS Agent daemon.

These tests start a REAL uvicorn server and send REAL HTTP requests over TCP.
Only the SDK query() is mocked — everything else (FastAPI routing, middleware,
session manager, serialization) runs for real.

This is the test layer that would have caught the 404 bug from using
the module-level `app` instead of `create_app()`.
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import uvicorn
from claude_agent_sdk import TextBlock

from obs_agent.daemon import create_app


def _free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_mock_client(messages: list[MagicMock]) -> AsyncMock:
    """Create a mock ClaudeSDKClient that yields the given messages."""
    client = AsyncMock()

    async def mock_receive():
        for msg in messages:
            yield msg

    client.receive_response = mock_receive
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    return client


@pytest.fixture
async def live_server(config):
    """Start a real uvicorn server on a random port.

    Yields the base URL (e.g. http://127.0.0.1:PORT).
    The server is shut down after the test.
    """
    port = _free_port()
    app = create_app(config)

    server_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(server_config)

    # Run server in a background task
    serve_task = asyncio.create_task(server.serve())

    # Wait for server to be ready (up to 5 seconds)
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
        pytest.fail("Uvicorn server failed to start within 5 seconds")

    yield base_url

    # Shutdown
    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        serve_task.cancel()


class TestLiveHealth:
    """GET /health over real HTTP."""

    async def test_live_health(self, live_server):
        """GET /health returns 200 with status ok."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{live_server}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestLiveChatResponse:
    """POST /chat over real HTTP with mocked SDK."""

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_live_chat_returns_response(self, mock_get_client, live_server):
        """POST /chat returns 200 with a response body."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello from the agent!")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": "hi"},
                timeout=10.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data
        assert data["response"] == "Hello from the agent!"


class TestLiveChatRejectsEmpty:
    """POST /chat validation over real HTTP."""

    async def test_live_chat_rejects_empty(self, live_server):
        """POST /chat with empty message returns 422."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": ""},
                timeout=10.0,
            )
        assert resp.status_code == 422


class TestLiveChatCapturesSessionId:
    """Session ID capture over real HTTP."""

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_live_chat_captures_session_id(self, mock_get_client, live_server):
        """POST /chat includes session_id in response when SDK provides one."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Response with session")]
        mock_msg.session_id = "sess-live-test-42"

        mock_get_client.return_value = _make_mock_client([mock_msg])

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat",
                json={"message": "hello"},
                timeout=10.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("session_id") == "sess-live-test-42"


class TestLiveUnknownRoute:
    """Unknown routes over real HTTP."""

    async def test_live_unknown_route_404(self, live_server):
        """GET /nonexistent returns 404."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{live_server}/nonexistent")
        assert resp.status_code == 404


class TestLiveEnqueue:
    """POST /chat/enqueue over real HTTP."""

    async def test_live_enqueue_returns_200(self, live_server):
        """POST /chat/enqueue returns 200 with queued status."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat/enqueue",
                json={"message": "queued message"},
                timeout=10.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert data["queue_size"] == 1

    async def test_live_enqueue_rejects_empty(self, live_server):
        """POST /chat/enqueue rejects empty message over real HTTP."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat/enqueue",
                json={"message": ""},
                timeout=10.0,
            )
        assert resp.status_code == 422

    async def test_live_enqueue_accumulates(self, live_server):
        """Multiple enqueues accumulate over real HTTP."""
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{live_server}/chat/enqueue",
                json={"message": "first"},
                timeout=10.0,
            )
            resp = await client.post(
                f"{live_server}/chat/enqueue",
                json={"message": "second"},
                timeout=10.0,
            )
        data = resp.json()
        assert data["queue_size"] == 2


class TestLiveInterrupt:
    """POST /chat/interrupt over real HTTP."""

    async def test_live_interrupt_returns_200(self, live_server):
        """POST /chat/interrupt returns 200."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/chat/interrupt",
                timeout=10.0,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["interrupted"] is True

    async def test_live_interrupt_idempotent(self, live_server):
        """Multiple interrupts are idempotent over real HTTP."""
        async with httpx.AsyncClient() as client:
            await client.post(f"{live_server}/chat/interrupt", timeout=10.0)
            resp = await client.post(f"{live_server}/chat/interrupt", timeout=10.0)
        assert resp.status_code == 200


class TestLiveCommands:
    """GET /commands over real HTTP."""

    async def test_live_commands_returns_list(self, live_server):
        """GET /commands returns available commands."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{live_server}/commands", timeout=10.0)
        assert resp.status_code == 200
        data = resp.json()
        names = [c["name"] for c in data["commands"]]
        assert "stop" in names
        assert "enqueue" in names
