"""Timeout behavior tests for Telegram eval platform response collection."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.evals.platform_telegram import TelegramPlatform


@pytest.mark.asyncio
async def test_collect_response_fails_fast_when_done_missing():
    platform = TelegramPlatform(
        api_id=1,
        api_hash="x",
        session_string="x",
        bot_username="x",
        first_message_timeout=0.2,
        done_timeout=5.0,
        idle_quiescence_timeout=0.05,
    )
    platform._response_queue.put_nowait("partial response")

    start = time.monotonic()
    output = await platform._collect_response(timeout=10.0, require_done=True)
    elapsed = time.monotonic() - start

    assert "missing (done) sentinel" in output
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_collect_response_returns_when_done_arrives():
    platform = TelegramPlatform(
        api_id=1,
        api_hash="x",
        session_string="x",
        bot_username="x",
        first_message_timeout=0.2,
        done_timeout=1.0,
        idle_quiescence_timeout=0.2,
    )
    platform._response_queue.put_nowait("chunk 1")

    async def _emit_done() -> None:
        await asyncio.sleep(0.01)
        platform._response_queue.put_nowait("(done)")

    task = asyncio.create_task(_emit_done())
    output = await platform._collect_response(timeout=2.0, require_done=True)
    await task

    assert "chunk 1" in output
    assert "(done)" in output
