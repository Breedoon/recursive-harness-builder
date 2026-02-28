"""Timeout behavior tests for Telegram eval platform response collection."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.evals.platform_telegram import TelegramPlatform


@pytest.mark.asyncio
async def test_collect_response_fails_fast_when_completion_missing():
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

    assert "missing completion marker" in output
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_collect_response_returns_when_completion_arrives():
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
        platform._response_queue.put_nowait("context: 24k / 200k")

    task = asyncio.create_task(_emit_done())
    output = await platform._collect_response(timeout=2.0, require_done=True)
    await task

    assert "chunk 1" in output
    assert "context: 24k / 200k" in output


@pytest.mark.asyncio
async def test_collect_response_accepts_completion_with_username_mention():
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
        platform._response_queue.put_nowait("context: 24k / 200k\n@test_user")

    task = asyncio.create_task(_emit_done())
    output = await platform._collect_response(timeout=2.0, require_done=True)
    await task

    assert "chunk 1" in output
    assert "context: 24k / 200k" in output
    assert "@test_user" in output


@pytest.mark.asyncio
async def test_collect_response_accepts_formatted_completion_wire_text():
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
        platform._response_queue.put_nowait("__context: 24k / 200k\n____@test_user__")

    task = asyncio.create_task(_emit_done())
    output = await platform._collect_response(timeout=2.0, require_done=True)
    await task

    assert "chunk 1" in output
    assert "__context: 24k / 200k" in output
