"""Legacy self_fork integration test.

ForkTask now owns delegated forking in Telegram topics, so the old HTTP
self_fork/cache behavior is no longer part of the supported surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import httpx
import pytest
import uvicorn

from obs_agent.daemon import create_app

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skip(reason="Legacy self_fork integration test removed; ForkTask is Telegram-only")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_server(e2e_config):
    """Start a real uvicorn server on a random port."""
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


@pytest.mark.integration
class TestForkCacheHits:
    """Retained only as skipped historical reference."""

    async def test_fork_produces_response(self, live_server):
        """Send a message, then ask the agent to fork — fork should respond."""
        async with httpx.AsyncClient() as client:
            # Turn 1: establish conversation context
            resp1 = await client.post(
                f"{live_server}/chat",
                json={"message": "Remember the secret word FLAMINGO. Just say OK."},
                timeout=120.0,
            )
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert len(data1["response"]) > 0

            # Turn 2: ask agent to fork and recall
            resp2 = await client.post(
                f"{live_server}/chat",
                json={
                    "message": (
                        "Use the self_fork tool to ask your fork: "
                        "'What secret word was I given? Reply with just the word.'"
                    )
                },
                timeout=180.0,
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert len(data2["response"]) > 0

            # The fork should have seen the conversation and returned FLAMINGO
            logger.info("Fork response: %s", data2["response"][:500])
