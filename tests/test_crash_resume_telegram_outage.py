"""vault-scg2: a crash snapshot must survive a Telegram outage at process start.

On 2026-10-21 (power cut) the first runtime died in app.initialize() with
TimedOut, the in-process restart ran with first_start=False, so the previous
process's crash snapshot was never consumed and its routes never resumed.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obs_agent.telegram as tg


def _fake_app(fail_initialize: bool, calls: list):
    app = MagicMock()
    bot = MagicMock()
    bot.initialize_runtime = AsyncMock()
    bot._ensure_background_poller = AsyncMock()

    def _resume(*, include_crash=True):
        calls.append(include_crash)

    bot.start_maintenance_resume = MagicMock(side_effect=_resume)
    bot.start_crash_snapshot_writer = MagicMock()
    app.bot_data = {"obs_telegram_bot": bot}
    app.initialize = AsyncMock(side_effect=TimeoutError("telegram unreachable") if fail_initialize else None)
    app.start = AsyncMock()
    app.updater.start_polling = AsyncMock()
    app.running = False  # ends the health loop with RuntimeError right after resume
    return app


@pytest.mark.asyncio
async def test_first_runtime_failing_before_resume_still_lets_next_runtime_consume_crash(monkeypatch):
    monkeypatch.setattr(tg, "_PROCESS_CRASH_RESUME_ATTEMPTED", False, raising=False)
    calls: list = []
    apps = iter([_fake_app(True, calls), _fake_app(False, calls), _fake_app(False, calls)])
    with patch.object(tg, "create_telegram_app", lambda config: next(apps)), \
         patch.object(tg, "_clear_secondary_bot_commands", AsyncMock()), \
         patch.object(tg, "_set_bot_commands", AsyncMock()), \
         patch.object(tg, "_install_maintenance_signal_handler", MagicMock()), \
         patch.object(tg, "_start_orphan_cli_reaper", MagicMock()) as reaper, \
         patch.object(tg, "_shutdown_telegram_runtime", AsyncMock()), \
         patch.object(tg, "_TELEGRAM_RUNTIME_HEALTH_POLL_SECONDS", 0):
        with pytest.raises(TimeoutError):
            await tg._run_telegram_bot_once(MagicMock(), first_start=True)
        assert calls == []  # never reached the resume step
        with pytest.raises(RuntimeError):
            await tg._run_telegram_bot_once(MagicMock(), first_start=False)
        assert calls == [True]  # the previous process's snapshot is consumed now
        assert reaper.call_count == 1
        with pytest.raises(RuntimeError):
            await tg._run_telegram_bot_once(MagicMock(), first_start=False)
        assert calls == [True, False]  # a later in-process restart never replays its own snapshot
        assert reaper.call_count == 1
