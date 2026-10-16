"""Schedule fires that hit a provider credit limit (vault-ns5) and interrupted fires (ops-29).

No test here may reach a real kill/signal path; the autouse fixture makes any attempt raise.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import credit_limit as cl
from obs_agent.telegram import TelegramBot, TelegramRoute, _RunOutcome, _TopicScheduleRecord

_GAP = 0.05
_LIMIT = "You've hit your limit · resets 11am (UTC)"


@pytest.fixture(autouse=True)
def _no_real_kills(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError(f"test reached a real kill path: {args!r}")

    monkeypatch.setattr(os, "kill", _refuse)
    monkeypatch.setattr(os, "killpg", _refuse)
    monkeypatch.setattr("obs_agent.maintenance_restart.kill_process_tree", _refuse)


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


# --- credit_limit parsing -------------------------------------------------------------


class TestParsing:
    def test_recognizes_cli_limit_lines(self):
        for text in (
            _LIMIT,
            "You've hit your limit - resets 6:30pm (America/Los_Angeles)",
            "Claude AI usage limit reached|1760000000",
            "5-hour limit reached ∙ resets 3pm",
            '{"type":"error","error":{"type":"rate_limit_error","message":"x"}}',
            "You've hit your weekly limit · resets Oct 17, 3pm (Europe/Warsaw)",
        ):
            assert cl.is_credit_limit_error(text), text

    def test_ignores_ordinary_errors(self):
        for text in (None, "", "API Error: 500 overloaded", "Prompt is too long", "TimeoutError: x"):
            assert not cl.is_credit_limit_error(text), text

    def test_clock_reset_today_and_tomorrow(self):
        now = _ts("2026-10-16T09:00:00")
        assert cl.parse_reset_at(_LIMIT, now) == _ts("2026-10-16T11:00:00")
        late = _ts("2026-10-16T13:30:00")
        assert cl.parse_reset_at(_LIMIT, late) == _ts("2026-10-17T11:00:00")

    def test_clock_reset_with_zone(self):
        now = _ts("2026-10-16T09:00:00")
        text = "You've hit your limit - resets 6:30pm (America/Los_Angeles)"
        # 18:30 PDT == 01:30Z next day
        assert cl.parse_reset_at(text, now) == _ts("2026-10-17T01:30:00")

    def test_dated_reset(self):
        now = _ts("2026-10-16T09:00:00")
        text = "You've hit your weekly limit · resets Oct 17, 3pm (UTC)"
        assert cl.parse_reset_at(text, now) == _ts("2026-10-17T15:00:00")

    def test_epoch_and_duration_resets(self):
        now = 1_760_000_000.0 - 100
        assert cl.parse_reset_at("Claude AI usage limit reached|1760000000", now) == 1_760_000_000.0
        assert cl.parse_reset_at("usage limit reached, try again in 2h 5m", 1000.0) == 1000.0 + 7500

    def test_unparsed_reset_backs_off_bounded(self):
        limit = cl.parse_credit_limit("You've hit your limit", 1000.0)
        assert limit is not None and limit.reset_at is None
        delays = [cl.rearm_at(limit, now=1000.0, consecutive_limit_fires=n) - 1000.0 for n in range(1, 8)]
        assert delays == [300, 600, 1200, 2400, 3600, 3600, 3600]

    def test_parsed_reset_adds_slack(self):
        now = _ts("2026-10-16T09:00:00")
        limit = cl.parse_credit_limit(_LIMIT, now)
        assert cl.rearm_at(limit, now=now, consecutive_limit_fires=5) == _ts("2026-10-16T11:01:00")

    def test_turn_limit_text(self):
        assert cl.turn_limit_text(failed=True, error_text=_LIMIT, assistant_text="") == _LIMIT
        assert cl.turn_limit_text(failed=True, error_text="boom", assistant_text=_LIMIT) is None
        assert cl.turn_limit_text(failed=False, error_text=None, assistant_text=_LIMIT) == _LIMIT
        long_reply = "Status: " + "x" * 500 + " you've hit your limit"
        assert cl.turn_limit_text(failed=False, error_text=None, assistant_text=long_reply) is None
        assert cl.turn_limit_text(failed=False, error_text=None, assistant_text="done") is None


# --- scheduler behavior --------------------------------------------------------------


def _record(route, *, schedule_id="sched-limit", max_runs=3, trigger_kind="interval", **extra):
    return _TopicScheduleRecord(
        schedule_id=schedule_id,
        route=route,
        description="LimitJob",
        cron_expr="*/1 * * * *",
        trigger_kind=trigger_kind,
        interval_seconds=600,
        prompt="do the check",
        schedule_mode="interval",
        reset_session=False,
        max_runs=max_runs,
        next_run_at=0.0,
        **extra,
    )


def _bot(config, thread_id):
    bot = TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=thread_id)
    state = bot._get_state(route)
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    state.last_bot = fake_bot
    return bot, route


async def _fire(bot, record, outcome, now):
    with patch.object(bot, "_run_and_send", new=AsyncMock(return_value=outcome)) as run_mock, patch.object(
        bot, "_send_system_message", new=AsyncMock()
    ) as send_mock, patch("obs_agent.telegram.time.time", return_value=now):
        ran = await bot._execute_topic_schedule(record=record, trigger_kind=record.trigger_kind)
    texts = [str(call.kwargs.get("text", "")) for call in send_mock.await_args_list]
    return ran, run_mock, texts


class TestScheduler:
    async def test_limit_failure_does_not_count_and_rearms_after_reset(self, config):
        bot, route = _bot(config, 8101)
        record = _record(route, max_runs=1)
        bot._register_topic_schedule(record)
        now = _ts("2026-10-16T09:00:00")

        ran, run_mock, texts = await _fire(
            bot, record, _RunOutcome(assistant_text=_LIMIT, failed=True, error=_LIMIT), now
        )

        assert ran is False
        run_mock.assert_awaited_once()
        updated = bot._topic_schedules_by_id["sched-limit"]
        assert updated.run_count == 0
        assert updated.enabled is True
        assert updated.next_run_at == _ts("2026-10-16T11:01:00")
        assert updated.last_error.startswith("credit limit; fire not counted")
        assert any("schedule deferred: LimitJob" in text for text in texts)
        assert not any(text.startswith("schedule failed") for text in texts)

    async def test_silent_limit_reply_is_not_counted_as_success(self, config):
        bot, route = _bot(config, 8102)
        record = _record(route)
        bot._register_topic_schedule(record)
        now = _ts("2026-10-16T09:00:00")

        ran, _run_mock, _texts = await _fire(bot, record, _RunOutcome(assistant_text=_LIMIT), now)

        assert ran is False
        assert record.run_count == 0
        assert record.last_success_at is None
        assert record.next_run_at == _ts("2026-10-16T11:01:00")

    async def test_route_hold_skips_fires_without_running_or_counting(self, config):
        bot, route = _bot(config, 8103)
        first = _record(route, schedule_id="a")
        second = _record(route, schedule_id="b")
        bot._register_topic_schedule(first)
        bot._register_topic_schedule(second)
        now = _ts("2026-10-16T09:00:00")
        await _fire(bot, first, _RunOutcome(assistant_text=_LIMIT, failed=True, error=_LIMIT), now)
        # _run_and_send is mocked, so seed the route hold it would have recorded.
        bot._note_route_credit_limit(route=route, failed=True, error_text=_LIMIT, now=now)

        ran, run_mock, _texts = await _fire(bot, second, _RunOutcome(assistant_text="ok"), now + 60)

        assert ran is False
        run_mock.assert_not_awaited()  # nothing appended to the session: no poisoned tail
        assert second.run_count == 0
        assert second.next_run_at == _ts("2026-10-16T11:01:00")

    async def test_after_reset_fire_runs_and_counts(self, config):
        bot, route = _bot(config, 8104)
        record = _record(route, max_runs=1)
        bot._register_topic_schedule(record)
        now = _ts("2026-10-16T09:00:00")
        bot._note_route_credit_limit(route=route, failed=True, error_text=_LIMIT, now=now)
        await _fire(bot, record, _RunOutcome(assistant_text=_LIMIT, failed=True, error=_LIMIT), now)

        after = _ts("2026-10-16T11:01:30")
        ran, run_mock, _texts = await _fire(bot, record, _RunOutcome(assistant_text="ok"), after)

        assert ran is True
        run_mock.assert_awaited_once()
        assert record.run_count == 1
        assert record.enabled is False  # max_runs reached by a real run only

    async def test_outage_burns_no_slots_across_many_fires(self, config):
        bot, route = _bot(config, 8105)
        record = _record(route, max_runs=10)
        bot._register_topic_schedule(record)
        now = _ts("2026-10-16T09:00:00")
        limit_outcome = _RunOutcome(assistant_text="You've hit your limit", failed=True, error="You've hit your limit")
        runs = 0
        for _ in range(8):
            now = max(now, record.next_run_at or now)
            _ran, run_mock, _texts = await _fire(bot, record, limit_outcome, now)
            runs += run_mock.await_count
        assert record.run_count == 0
        assert record.enabled is True
        assert runs == 8

    async def test_non_limit_failure_still_counts(self, config):
        bot, route = _bot(config, 8106)
        record = _record(route, max_runs=1)
        bot._register_topic_schedule(record)

        ran, _run_mock, texts = await _fire(
            bot, record, _RunOutcome(assistant_text="", failed=True, error="API Error: 500"), 1000.0
        )

        assert ran is False
        assert record.run_count == 1
        assert record.enabled is False
        assert any(text.startswith("schedule failed") for text in texts)

    async def test_limit_with_until_passed_ends_schedule(self, config):
        bot, route = _bot(config, 8107)
        now = _ts("2026-10-16T09:00:00")
        record = _record(route, until_ts=now + 30)
        bot._register_topic_schedule(record)

        _ran, _run_mock, texts = await _fire(
            bot, record, _RunOutcome(assistant_text=_LIMIT, failed=True, error=_LIMIT), now
        )

        assert record.run_count == 0
        assert record.enabled is False
        assert record.next_run_at is None
        assert any("schedule ended: LimitJob" in text for text in texts)

    async def test_successful_turn_clears_route_hold(self, config):
        bot, route = _bot(config, 8108)
        bot._note_route_credit_limit(route=route, failed=True, error_text=_LIMIT, now=1000.0)
        assert route in bot._route_credit_limit_until
        bot._note_route_credit_limit(route=route, failed=False, error_text=None, assistant_text="hi")
        assert route not in bot._route_credit_limit_until

    async def test_ordinary_failure_keeps_route_hold(self, config):
        bot, route = _bot(config, 8109)
        bot._note_route_credit_limit(route=route, failed=True, error_text=_LIMIT, now=1000.0)
        bot._note_route_credit_limit(route=route, failed=True, error_text="boom", now=1100.0)
        assert route in bot._route_credit_limit_until


class TestInterruptedFire:
    async def test_start_is_persisted_and_restart_marks_interrupted(self, config):
        bot, route = _bot(config, 8201)
        record = _record(route)
        bot._register_topic_schedule(record)
        seen: dict[str, str | None] = {}

        async def _run(**_kwargs):
            seen["during"] = bot._topic_schedules_by_id["sched-limit"].last_error
            restored = TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)
            restored._state_store.initialize()
            restored._restore_state_from_store()
            seen["restored"] = restored._topic_schedules_by_id["sched-limit"].last_error
            return _RunOutcome(assistant_text="ok")

        with patch.object(bot, "_run_and_send", new=_run), patch.object(
            bot, "_send_system_message", new=AsyncMock()
        ):
            ran = await bot._execute_topic_schedule(record=record, trigger_kind="interval")
        assert ran is True, record.last_error

        assert record.last_error is None, record.last_error
        assert seen["during"].startswith("run in progress, started ")
        assert seen["restored"].startswith("interrupted: run started ")
        assert "did not finish before OBS restarted" in seen["restored"]
        assert record.last_error is None
        assert record.run_count == 1

    async def test_cancelled_fire_is_marked_interrupted_and_not_counted(self, config):
        bot, route = _bot(config, 8202)
        record = _record(route)
        bot._register_topic_schedule(record)

        async def _run(**_kwargs):
            raise asyncio.CancelledError()

        with patch.object(bot, "_run_and_send", new=_run), patch.object(
            bot, "_send_system_message", new=AsyncMock()
        ), pytest.raises(asyncio.CancelledError):
            await bot._execute_topic_schedule(record=record, trigger_kind="interval")

        assert record.run_count == 0
        assert record.last_error.startswith("interrupted: run started ")
        assert "was cancelled" in record.last_error
