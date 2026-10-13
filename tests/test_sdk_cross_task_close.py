"""Regression: closing an SDK Query from a task other than the one that started it.

OBS runs ``ClaudeSDKClient.connect()`` in a detached task (session.py), so the
SDK's anyio task group is entered in that short-lived task. Every later
``disconnect()`` runs in another task. On the unpatched SDK this:

1. raised RuntimeError from the task group's ``__aexit__`` (cross-task cancel
   scope exit), which skipped ``transport.close()``;
2. left the cancelled scope scheduling ``_deliver_cancellation`` on every loop
   iteration forever (350 such scopes pegged the prod event loop at 100% CPU
   on 2026-10-13);
3. cancelled the reader before it could send its ``end`` sentinel, so a turn
   consuming ``receive_messages()`` waited forever. That held the route lock and
   made topics 16234 (Work Registry) and 83366 permanently "dead" after a stop
   escalation killed their stuck CLI.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from claude_agent_sdk._internal.query import Query

from obs_agent._sdk_patch import ensure_query_close_patch


class _HangingTransport:
    """Transport whose stdout never produces data (like a stuck CLI)."""

    def __init__(self) -> None:
        self.closed = False
        self._never = asyncio.Event()

    async def connect(self) -> None:  # pragma: no cover - unused
        return None

    async def write(self, data: str) -> None:
        return None

    async def end_input(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        await self._never.wait()
        if False:  # pragma: no cover
            yield {}


def _deliver_cancellation_handles(loop: asyncio.AbstractEventLoop) -> int:
    count = 0
    for handle in list(getattr(loop, "_ready", [])):
        cb = getattr(handle, "_callback", None)
        if "deliver_cancellation" in getattr(cb, "__qualname__", ""):
            count += 1
    return count


async def _scenario() -> tuple[str, bool, int]:
    transport = _HangingTransport()
    query = Query(transport=transport, is_streaming_mode=True)
    # Start the reader in a detached task, exactly like session.py does with
    # ``await asyncio.create_task(client.connect())``.
    await asyncio.create_task(query.start())

    async def consume() -> str:
        try:
            async for _message in query.receive_messages():
                pass
        except Exception as exc:  # the SDK raises a plain Exception for "error"
            return f"error:{exc}"
        return "ended"

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(query.close(), timeout=2.0)
    except Exception:
        pass  # callers in OBS swallow disconnect errors too
    try:
        outcome = await asyncio.wait_for(consumer, timeout=2.0)
    except asyncio.TimeoutError:
        consumer.cancel()
        outcome = "hung"
    await asyncio.sleep(0.05)
    spinning = _deliver_cancellation_handles(asyncio.get_running_loop())
    return outcome, transport.closed, spinning


def test_cross_task_close_wakes_consumer_closes_transport_and_stops_spin() -> None:
    ensure_query_close_patch()
    outcome, transport_closed, spinning = asyncio.run(_scenario())
    assert outcome.startswith("error:"), outcome
    assert "closed" in outcome
    assert transport_closed is True
    assert spinning == 0


def test_same_task_close_still_works() -> None:
    ensure_query_close_patch()

    async def run() -> tuple[bool, int]:
        transport = _HangingTransport()
        query = Query(transport=transport, is_streaming_mode=True)
        await query.start()
        await query.close()
        await asyncio.sleep(0.05)
        return transport.closed, _deliver_cancellation_handles(asyncio.get_running_loop())

    closed, spinning = asyncio.run(run())
    assert closed is True
    assert spinning == 0


@pytest.mark.parametrize("patched", [False])
def test_unpatched_sdk_reproduces_the_hang(patched: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Documents the original failure on the pristine SDK close()."""
    from obs_agent import _sdk_patch

    original = getattr(Query.close, "_obs_original", Query.close)
    monkeypatch.setattr(Query, "close", original)
    outcome, transport_closed, spinning = asyncio.run(_scenario())
    assert outcome == "hung"
    assert transport_closed is False
    assert spinning >= 1
    assert _sdk_patch  # imported for the original reference
