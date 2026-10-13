"""Hung-CLI watchdog (2026-10-13 dead topics 83366 and 16234)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from obs_agent import maintenance_restart as maint
from obs_agent.telegram import TelegramBot, TelegramRoute


def _bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)


def _busy_with_cli(bot: TelegramBot, route: TelegramRoute, *, pid: int):
    state = bot._get_state(route, topic_title="Worker")
    state.session_manager.set_session_id(f"sid-{route.thread_id}")
    state.busy = True
    client = MagicMock()
    client._transport._process.pid = pid
    client._transport._process.returncode = None
    state.session_manager._client = client
    state.last_bot = MagicMock()
    return state


@pytest.fixture
def harness(config, monkeypatch):
    monkeypatch.setenv("OBS_CLI_HANG_WATCHDOG_SECONDS", "900")
    bot = _bot(config)
    killed: list[int] = []
    monkeypatch.setattr(maint, "kill_process_tree", lambda pid: killed.append(pid) or [pid])
    sent = AsyncMock()
    monkeypatch.setattr(bot, "_send_system_message", sent)
    mtimes: dict[str, float] = {}
    monkeypatch.setattr(bot, "_session_jsonl_mtime", lambda sid: mtimes.get(sid, 1.0))
    return bot, killed, sent, mtimes


async def _sweep(bot, now, cpu):
    return await bot._cli_hang_sweep(now=now, read_cpu=lambda pid: cpu)


@pytest.mark.asyncio
async def test_spinning_cli_without_progress_is_killed_and_resumed(harness):
    bot, killed, sent, _ = harness
    state = _busy_with_cli(bot, TelegramRoute(chat_id=1, thread_id=83366), pid=4242)
    disconnect = AsyncMock(return_value=True)
    state.session_manager.disconnect_idle_client = disconnect
    assert await _sweep(bot, 1000.0, 0.0) == 0
    assert await _sweep(bot, 1500.0, 500.0) == 0  # under the window
    assert await _sweep(bot, 1960.0, 955.0) == 1  # 960 s at ~99% CPU
    assert killed == [4242]
    disconnect.assert_awaited_once_with(direct_kill=True)
    assert state.hook_state.stop_requested_at is None  # runner reconnects and resumes
    assert "resumed" in sent.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_idle_waiting_cli_is_not_killed(harness):
    bot, killed, _, _ = harness
    _busy_with_cli(bot, TelegramRoute(chat_id=1, thread_id=2), pid=4243)
    await _sweep(bot, 1000.0, 10.0)
    assert await _sweep(bot, 5000.0, 30.0) == 0  # long tool/API wait, ~0.5% CPU
    assert killed == []


@pytest.mark.asyncio
async def test_jsonl_progress_resets_the_window(harness):
    bot, killed, _, mtimes = harness
    state = _busy_with_cli(bot, TelegramRoute(chat_id=1, thread_id=3), pid=4244)
    await _sweep(bot, 1000.0, 0.0)
    mtimes[state.session_id] = 2.0  # the CLI wrote to its JSONL
    assert await _sweep(bot, 1960.0, 960.0) == 0
    assert await _sweep(bot, 2500.0, 1500.0) == 0
    assert killed == []


@pytest.mark.asyncio
async def test_second_hang_within_three_hours_ends_the_turn(harness):
    bot, killed, sent, _ = harness
    state = _busy_with_cli(bot, TelegramRoute(chat_id=1, thread_id=16234), pid=4245)
    state.session_manager.disconnect_idle_client = AsyncMock(return_value=True)
    await _sweep(bot, 1000.0, 0.0)
    await _sweep(bot, 1960.0, 960.0)
    assert state.hook_state.stop_requested_at is None
    # The resumed turn hangs again on a new CLI process.
    state.session_manager._client._transport._process.pid = 4246
    await _sweep(bot, 3000.0, 0.0)
    await _sweep(bot, 3960.0, 960.0)
    assert killed == [4245, 4246]
    assert state.hook_state.stop_requested_at is not None
    assert "/clear" in sent.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_watchdog_disabled_by_env(config, monkeypatch):
    monkeypatch.setenv("OBS_CLI_HANG_WATCHDOG_SECONDS", "0")
    bot = _bot(config)
    _busy_with_cli(bot, TelegramRoute(chat_id=1, thread_id=4), pid=4247)
    assert await bot._cli_hang_sweep(now=1.0, read_cpu=lambda pid: 0.0) == 0
    assert bot._cli_hang_samples == {}
