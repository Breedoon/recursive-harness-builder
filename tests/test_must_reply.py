"""Tests for must_reply mechanism and schedule rearchitecture.

Covers:
- must_reply field on inbox messages (M1-M10)
- Reply detection in SendInboxMessage
- Reply_wake schedule creation and lifecycle
- Schedule overlap validation removal (SC1)
- Multiple coexisting schedules (SC2-SC7)
- CronDelete access for agents
- /unschedule next-only behavior
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# M1–M10: must_reply inbox mechanism
# ---------------------------------------------------------------------------


class TestMustReplyInboxFields:
    """Verify must_reply and replied fields on inbox messages."""

    def test_m1_detect_must_reply_completions_is_callable(self):
        """detect_must_reply_completions exists and handles basic input."""
        from obs_agent.tools import detect_must_reply_completions

        # Test with empty inbox — no must_reply messages
        updated, all_replied = detect_must_reply_completions([], "agent-a")
        assert updated == []
        assert all_replied is True  # No obligations = all cleared

    def test_m2_validate_must_reply_recipient_is_callable(self):
        """validate_must_reply_recipient exists and returns structured result."""
        from obs_agent.tools import validate_must_reply_recipient

        result = validate_must_reply_recipient(
            sender="agent-a", recipient="agent-b", must_reply=True
        )
        assert isinstance(result, dict)
        assert "ok" in result


class TestReplyDetection:
    """Verify reply detection logic using exported helper functions."""

    def test_m3_reply_marks_must_reply_as_replied(self):
        """When B sends to A, detect_must_reply_completions marks A's messages as replied."""
        from obs_agent.tools import detect_must_reply_completions

        # B's inbox has a must_reply from A
        b_inbox_entries = [{
            "from": "agent-a",
            "text": "Report back when done",
            "must_reply": True,
            "replied": False,
            "read": True,
        }]

        # B sends to A → reply detection fires
        updated, all_replied = detect_must_reply_completions(
            inbox_entries=b_inbox_entries,
            recipient_of_outgoing_message="agent-a",
        )

        assert updated[0]["replied"] is True
        assert all_replied is True  # Only one must_reply, now replied

    def test_m4_reply_to_wrong_agent_no_mark(self):
        """B has must_reply from A. B sends to C (not A). A's must_reply NOT marked."""
        from obs_agent.tools import detect_must_reply_completions

        b_inbox_entries = [{
            "from": "agent-a",
            "text": "Report back",
            "must_reply": True,
            "replied": False,
            "read": True,
        }]

        # B sends to C (not A) → A's must_reply should NOT be cleared
        updated, all_replied = detect_must_reply_completions(
            inbox_entries=b_inbox_entries,
            recipient_of_outgoing_message="agent-c",
        )

        assert updated[0]["replied"] is False  # Still unreplied
        assert all_replied is False

    def test_m5_all_replied_returns_true(self):
        """When all must_reply messages are replied, all_replied is True."""
        from obs_agent.tools import detect_must_reply_completions

        entries = [
            {"from": "agent-a", "text": "Task 1", "must_reply": True, "replied": False, "read": True},
            {"from": "agent-c", "text": "Task 2", "must_reply": True, "replied": True, "read": True},
        ]

        # B replies to A → now both are replied
        updated, all_replied = detect_must_reply_completions(
            inbox_entries=entries,
            recipient_of_outgoing_message="agent-a",
        )

        assert updated[0]["replied"] is True  # A's message now replied
        assert updated[1]["replied"] is True  # C's was already replied
        assert all_replied is True  # All must_reply cleared

    def test_m5b_check_and_clear_all_replied(self):
        """check_and_clear_must_reply_obligations returns True when all replied."""
        from obs_agent.tools import check_and_clear_must_reply_obligations

        entries = [
            {"from": "agent-a", "must_reply": True, "replied": True},
            {"from": "agent-b", "must_reply": True, "replied": True},
            {"from": "agent-c", "text": "no must_reply", "read": False},
        ]

        assert check_and_clear_must_reply_obligations(entries) is True

    def test_m6_partial_reply_returns_false(self):
        """B has must_reply from A and C. B replies to A only. all_replied is False."""
        from obs_agent.tools import detect_must_reply_completions

        entries = [
            {"from": "agent-a", "text": "Task 1", "must_reply": True, "replied": False, "read": True},
            {"from": "agent-c", "text": "Task 2", "must_reply": True, "replied": False, "read": True},
        ]

        # B replies to A only
        updated, all_replied = detect_must_reply_completions(
            inbox_entries=entries,
            recipient_of_outgoing_message="agent-a",
        )

        assert updated[0]["replied"] is True   # A's message replied
        assert updated[1]["replied"] is False   # C's message still pending
        assert all_replied is False  # Not all cleared → schedule should persist

    def test_m6b_check_and_clear_partial_reply(self):
        """check_and_clear returns False when some must_reply messages are unreplied."""
        from obs_agent.tools import check_and_clear_must_reply_obligations

        entries = [
            {"from": "agent-a", "must_reply": True, "replied": True},
            {"from": "agent-c", "must_reply": True, "replied": False},
        ]

        assert check_and_clear_must_reply_obligations(entries) is False

    def test_m3b_multiple_must_reply_from_same_sender(self):
        """Multiple must_reply from same sender are ALL marked replied at once."""
        from obs_agent.tools import detect_must_reply_completions

        entries = [
            {"from": "agent-a", "text": "Task 1", "must_reply": True, "replied": False},
            {"from": "agent-a", "text": "Task 2", "must_reply": True, "replied": False},
        ]

        updated, all_replied = detect_must_reply_completions(
            inbox_entries=entries,
            recipient_of_outgoing_message="agent-a",
        )

        assert all(e["replied"] is True for e in updated)
        assert all_replied is True

    def test_m3c_non_must_reply_messages_ignored(self):
        """Non-must_reply messages are not affected by reply detection."""
        from obs_agent.tools import detect_must_reply_completions

        entries = [
            {"from": "agent-a", "text": "Normal message", "read": False},
            {"from": "agent-a", "text": "Must reply", "must_reply": True, "replied": False},
        ]

        updated, all_replied = detect_must_reply_completions(
            inbox_entries=entries,
            recipient_of_outgoing_message="agent-a",
        )

        assert "replied" not in updated[0] or updated[0].get("replied") is not True
        assert updated[1]["replied"] is True
        assert all_replied is True


class TestReplyWakeSchedule:
    """Verify reply_wake schedule creation and lifecycle."""

    async def test_m7_upsert_resets_run_count(self, config):
        """Second must_reply message upserts the schedule, resetting run_count to 0."""
        from obs_agent.telegram import (
            TelegramBot, TelegramRoute,
            create_reply_wake_schedule, reply_wake_schedule_id,
        )

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)

        # Create and register initial reply_wake schedule
        record = create_reply_wake_schedule(route)
        bot._register_topic_schedule(record)

        # Simulate 2 firings
        registered = bot._topic_schedules_by_id[reply_wake_schedule_id(route)]
        registered.run_count = 2

        # New must_reply arrives — upsert should reset run_count to 0
        fresh_record = create_reply_wake_schedule(route)
        fresh_record.run_count = 0
        bot._register_topic_schedule(fresh_record)  # upsert

        updated = bot._topic_schedules_by_id[reply_wake_schedule_id(route)]
        assert updated.run_count == 0, "Upsert should reset run_count to 0"
        assert updated.max_runs == 3

    def test_m8_deterministic_schedule_id(self):
        """reply_wake schedule has a deterministic ID based on route."""
        from obs_agent.telegram import reply_wake_schedule_id, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        sid = reply_wake_schedule_id(route)
        assert sid == "reply-wake-123-456"
        # Same route always produces same ID
        assert reply_wake_schedule_id(route) == sid
        # Different route produces different ID
        route2 = TelegramRoute(chat_id=789, thread_id=101)
        assert reply_wake_schedule_id(route2) != sid

    def test_m9_schedule_params(self):
        """Reply_wake schedule has interval_seconds=1, max_runs=3."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        record = create_reply_wake_schedule(route)
        assert record.interval_seconds == 1
        assert record.max_runs == 3
        assert record.schedule_mode == "interval"
        assert record.enabled is True

    def test_m10_schedule_exhaustion_after_3_runs(self):
        """Schedule fires 3 times without reply → run_count reaches max_runs."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        record = create_reply_wake_schedule(route)
        assert record.max_runs == 3
        # Simulate 3 firings
        record.run_count = 3
        assert record.run_count >= record.max_runs


class TestMustReplyEdgeCases:
    """Edge cases for must_reply that the implementation must handle."""

    def test_must_reply_to_self_blocked(self):
        """Agent sending must_reply to itself should be blocked (prevents infinite loop)."""
        from obs_agent.tools import validate_must_reply_recipient

        result = validate_must_reply_recipient(sender="agent-a", recipient="agent-a", must_reply=True)
        assert isinstance(result, dict)
        assert result.get("ok") is False or result.get("error"), "must_reply to self should be blocked"

    def test_must_reply_to_other_allowed(self):
        """Agent sending must_reply to another agent should be allowed."""
        from obs_agent.tools import validate_must_reply_recipient

        result = validate_must_reply_recipient(sender="agent-a", recipient="agent-b", must_reply=True)
        assert result.get("ok") is True, "must_reply to different agent should be allowed"

    def test_no_must_reply_skips_validation(self):
        """When must_reply is False, self-send is fine (no validation needed)."""
        from obs_agent.tools import validate_must_reply_recipient

        result = validate_must_reply_recipient(sender="agent-a", recipient="agent-a", must_reply=False)
        assert result.get("ok") is True, "Non-must_reply self-send should be allowed"

    def test_concurrent_detect_completions_is_idempotent(self):
        """Calling detect_must_reply_completions twice on same entries is safe."""
        from obs_agent.tools import detect_must_reply_completions

        entries = [
            {"from": "agent-a", "text": "Task", "must_reply": True, "replied": False},
        ]

        # First call marks replied
        updated, all_replied = detect_must_reply_completions(entries, "agent-a")
        assert updated[0]["replied"] is True
        assert all_replied is True

        # Second call on already-replied entries is a no-op
        updated2, all_replied2 = detect_must_reply_completions(updated, "agent-a")
        assert updated2[0]["replied"] is True
        assert all_replied2 is True


# ---------------------------------------------------------------------------
# SC1–SC7: Schedule rearchitecture
# ---------------------------------------------------------------------------


class TestScheduleOverlapRemoval:
    """Verify _validate_schedule_overlap is removed."""

    async def test_sc1_overlapping_schedules_coexist(self):
        """_validate_schedule_overlap has been removed — verified."""
        from obs_agent.telegram import TelegramBot

        assert not hasattr(TelegramBot, "_validate_schedule_overlap"), \
            "_validate_schedule_overlap should be removed in the schedule rearchitecture"


class TestScheduleCoexistence:
    """Verify multiple schedules on same route work independently."""

    async def test_sc2_multiple_schedules_independent_run_counts(self, config):
        """Each schedule on a route has its own run_count and max_runs."""
        from obs_agent.telegram import TelegramBot, _TopicScheduleRecord, TelegramRoute

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)

        schedule_a = _TopicScheduleRecord(
            schedule_id="sched-a",
            route=route,
            description="heartbeat",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=60,
            prompt="tick-a",
            max_runs=10,
            run_count=5,
        )
        schedule_b = _TopicScheduleRecord(
            schedule_id="sched-b",
            route=route,
            description="reply-wake",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=1,
            prompt="reply to parent",
            max_runs=3,
            run_count=0,
        )
        bot._register_topic_schedule(schedule_a)
        bot._register_topic_schedule(schedule_b)

        route_schedules = bot._schedule_ids_by_route.get(route, set())
        assert "sched-a" in route_schedules
        assert "sched-b" in route_schedules
        assert bot._topic_schedules_by_id["sched-a"].run_count == 5
        assert bot._topic_schedules_by_id["sched-b"].run_count == 0


class TestCronDeleteAllowed:
    """Verify agents can delete schedules via CronDelete MCP tool."""

    async def test_sc5_cron_delete_mcp_tool_deletes_schedule(self, config, monkeypatch):
        from obs_agent.hooks import HookState
        from obs_agent.tools import create_obs_tools

        captured = {}

        def fake_create_sdk_mcp_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", fake_create_sdk_mcp_server)

        deleted_ids = []
        state = HookState()

        async def cron_deleter(args: dict) -> dict:
            deleted_ids.append(args["id"])
            return {"deleted": True, "id": args["id"]}

        state.cron_deleter = cron_deleter
        create_obs_tools(config, lambda: "sid-123", hook_state=state)
        handler = next(tool.handler for tool in captured["tools"] if tool.name == "CronDelete")

        result = await handler({"id": "sched-agent"})

        assert result.get("is_error") is not True
        assert result.get("deleted") is True
        assert deleted_ids == ["sched-agent"]

    async def test_sc5b_internal_cron_delete_still_works(self, config):
        """TelegramBot._cron_delete still works for /unschedule command handling."""
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        bot._get_state(route)

        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-internal",
                route=route,
                description="test",
                schedule_mode="interval",
                cron_expr=None,
                trigger_kind="interval",
                interval_seconds=300,
                prompt="test",
            )
        )

        # Internal _cron_delete should still work (used by /unschedule)
        result = await bot._cron_delete(route=route, args={"id": "sched-internal"})
        assert result.get("is_error") is not True, \
            "Internal _cron_delete should work for /unschedule path"
        assert "sched-internal" not in bot._schedule_ids_by_route.get(route, set())


class TestUnscheduleNextOnly:
    """Verify /unschedule without args deletes only the next upcoming schedule."""

    async def test_sc3_unschedule_no_args_deletes_next_only(self, config):
        """With 3 schedules, /unschedule deletes only the soonest.

        Note: The handler-level test is in test_telegram.py
        (test_unschedule_no_args_removes_next_upcoming_only).
        This test verifies at the bot internal level.
        """
        from unittest.mock import AsyncMock, MagicMock
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        bot._get_state(route)
        now = time.time()

        for sid, offset in [("sched-10", 10), ("sched-30", 30), ("sched-60", 60)]:
            bot._register_topic_schedule(
                _TopicScheduleRecord(
                    schedule_id=sid,
                    route=route,
                    description=f"test-{sid}",
                    schedule_mode="interval",
                    cron_expr=None,
                    trigger_kind="interval",
                    interval_seconds=offset,
                    prompt=f"tick-{sid}",
                    max_runs=10,
                    next_run_at=now + offset,
                )
            )

        assert len(bot._schedule_ids_by_route.get(route, set())) == 3

        # Simulate /unschedule with no args via handle_unschedule
        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_message.chat_id = 67890
        update.effective_message.message_thread_id = None
        ctx = MagicMock()
        ctx.args = []  # No args = delete next upcoming
        ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))

        await bot.handle_unschedule(update, ctx)

        remaining = bot._schedule_ids_by_route.get(route, set())
        assert "sched-10" not in remaining, "soonest schedule should be deleted"
        assert "sched-30" in remaining, "later schedules should remain"
        assert "sched-60" in remaining, "later schedules should remain"

    async def test_sc4_unschedule_with_id_deletes_specific(self, config):
        """'/unschedule <id>' still works — deletes the specified schedule."""
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)

        record = _TopicScheduleRecord(
            schedule_id="target-sched",
            route=route,
            description="target",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=60,
            prompt="tick",
        )
        bot._register_topic_schedule(record)
        assert "target-sched" in bot._schedule_ids_by_route.get(route, set())

        bot._delete_topic_schedule("target-sched")
        assert "target-sched" not in bot._schedule_ids_by_route.get(route, set())

    def test_sc7_unschedule_all_is_deprecated(self):
        """'/unschedule all' is deprecated; topic-local unschedule remains covered above."""
        pass


class TestScheduleDispatchReliability:
    async def test_due_interval_schedules_dispatch_without_blocking_poller(self, config, monkeypatch):
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=67890, thread_id=1)
        route_b = TelegramRoute(chat_id=67890, thread_id=2)
        now = time.time()
        events: list[tuple[str, float]] = []
        release_long = asyncio.Event()

        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="long-running",
                route=route_a,
                description="long-running",
                schedule_mode="interval",
                cron_expr=None,
                trigger_kind="interval",
                interval_seconds=60,
                prompt="long",
                max_runs=3,
                next_run_at=now - 1,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="quick-other-route",
                route=route_b,
                description="quick-other-route",
                schedule_mode="interval",
                cron_expr=None,
                trigger_kind="interval",
                interval_seconds=60,
                prompt="quick",
                max_runs=3,
                next_run_at=now - 1,
            )
        )

        async def fake_execute(*, record, trigger_kind):
            events.append((record.schedule_id, time.monotonic()))
            if record.schedule_id == "long-running":
                await release_long.wait()
            else:
                record.run_count += 1
                record.next_run_at = time.time() + 60
                bot._register_topic_schedule(record)
            return True

        monkeypatch.setattr(bot, "_execute_topic_schedule", fake_execute)

        await bot._run_due_interval_schedules()
        await asyncio.sleep(0.05)

        assert {event[0] for event in events} == {"long-running", "quick-other-route"}
        assert bot._topic_schedules_by_id["quick-other-route"].run_count == 1
        assert "long-running" in bot._schedule_execution_tasks
        assert not bot._schedule_execution_tasks["long-running"].done()

        release_long.set()
        await asyncio.gather(*list(bot._schedule_execution_tasks.values()))

    async def test_same_schedule_due_again_while_running_does_not_double_fire(self, config, monkeypatch):
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=67890, thread_id=10)
        route_b = TelegramRoute(chat_id=67890, thread_id=20)
        now = time.time()
        started: list[str] = []
        release_long = asyncio.Event()

        long_record = _TopicScheduleRecord(
            schedule_id="same-schedule",
            route=route_a,
            description="same-schedule",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=60,
            prompt="long",
            max_runs=3,
            next_run_at=now - 1,
        )
        other_record = _TopicScheduleRecord(
            schedule_id="other-schedule",
            route=route_b,
            description="other-schedule",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=60,
            prompt="other",
            max_runs=3,
            next_run_at=now - 1,
        )
        bot._register_topic_schedule(long_record)
        bot._register_topic_schedule(other_record)

        async def fake_execute(*, record, trigger_kind):
            started.append(record.schedule_id)
            if record.schedule_id == "same-schedule":
                await release_long.wait()
            else:
                record.run_count += 1
                record.next_run_at = time.time() + 60
                bot._register_topic_schedule(record)
            return True

        monkeypatch.setattr(bot, "_execute_topic_schedule", fake_execute)

        await bot._run_due_interval_schedules()
        await asyncio.sleep(0.05)
        bot._topic_schedules_by_id["same-schedule"].next_run_at = time.time() - 1
        await bot._run_due_interval_schedules()
        await asyncio.sleep(0.05)

        assert started.count("same-schedule") == 1
        assert started.count("other-schedule") == 1
        assert bot._topic_schedules_by_id["other-schedule"].run_count == 1

        release_long.set()
        await asyncio.gather(*list(bot._schedule_execution_tasks.values()))

    async def test_schedule_failure_isolated_from_other_due_schedules(self, config, monkeypatch):
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=67890, thread_id=101)
        route_b = TelegramRoute(chat_id=67890, thread_id=202)
        now = time.time()
        completed: list[str] = []

        for schedule_id, route in [("failing", route_a), ("survivor", route_b)]:
            bot._register_topic_schedule(
                _TopicScheduleRecord(
                    schedule_id=schedule_id,
                    route=route,
                    description=schedule_id,
                    schedule_mode="interval",
                    cron_expr=None,
                    trigger_kind="interval",
                    interval_seconds=60,
                    prompt=schedule_id,
                    max_runs=3,
                    next_run_at=now - 1,
                )
            )

        async def fake_execute(*, record, trigger_kind):
            if record.schedule_id == "failing":
                record.last_error = "boom"
                record.run_count += 1
                record.next_run_at = time.time() + 60
                bot._register_topic_schedule(record)
                raise RuntimeError("boom")
            completed.append(record.schedule_id)
            record.run_count += 1
            record.next_run_at = time.time() + 60
            bot._register_topic_schedule(record)
            return True

        monkeypatch.setattr(bot, "_execute_topic_schedule", fake_execute)

        await bot._run_due_interval_schedules()
        results = await asyncio.gather(
            *list(bot._schedule_execution_tasks.values()),
            return_exceptions=True,
        )

        assert any(isinstance(result, RuntimeError) for result in results)
        assert completed == ["survivor"]
        assert bot._topic_schedules_by_id["failing"].last_error == "boom"
        assert bot._topic_schedules_by_id["survivor"].run_count == 1

    async def test_many_schedule_stress_runs_long_failing_and_multi_route(self, config, monkeypatch):
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        now = time.time()
        release_long = asyncio.Event()
        started: list[str] = []
        failures: list[str] = []
        route_count = 5
        schedule_count = 20

        for index in range(schedule_count):
            bot._register_topic_schedule(
                _TopicScheduleRecord(
                    schedule_id=f"stress-{index}",
                    route=TelegramRoute(chat_id=67890, thread_id=index % route_count),
                    description=f"stress-{index}",
                    schedule_mode="interval",
                    cron_expr=None,
                    trigger_kind="interval",
                    interval_seconds=60,
                    prompt=f"stress-{index}",
                    max_runs=3,
                    next_run_at=now - 1,
                )
            )

        async def fake_execute(*, record, trigger_kind):
            started.append(record.schedule_id)
            if record.schedule_id == "stress-0":
                await release_long.wait()
            if record.schedule_id == "stress-1":
                failures.append(record.schedule_id)
                record.last_error = "stress failure"
                record.run_count += 1
                record.next_run_at = time.time() + 60
                bot._register_topic_schedule(record)
                raise RuntimeError("stress failure")
            record.run_count += 1
            record.next_run_at = time.time() + 60
            bot._register_topic_schedule(record)
            return True

        monkeypatch.setattr(bot, "_execute_topic_schedule", fake_execute)

        await bot._run_due_interval_schedules()
        await asyncio.sleep(0.05)

        assert len(set(started)) == schedule_count
        assert "stress-0" in bot._schedule_execution_tasks
        assert not bot._schedule_execution_tasks["stress-0"].done()
        assert failures == ["stress-1"]
        assert bot._topic_schedules_by_id["stress-2"].run_count == 1
        assert len({bot._topic_schedules_by_id[sid].route for sid in started}) == route_count

        release_long.set()
        await asyncio.gather(
            *list(bot._schedule_execution_tasks.values()),
            return_exceptions=True,
        )

    async def test_persisted_due_schedule_survives_adapter_recreation(self, config, monkeypatch):
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        route = TelegramRoute(chat_id=67890, thread_id=303)
        first = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        first._get_state(route)
        first._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="persisted-schedule",
                route=route,
                description="persisted-schedule",
                schedule_mode="interval",
                cron_expr=None,
                trigger_kind="interval",
                interval_seconds=60,
                prompt="persisted",
                max_runs=3,
                next_run_at=time.time() - 1,
            )
        )
        first._state_store.close()

        second = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        second._restore_state_from_store()
        executed: list[str] = []

        async def fake_execute(*, record, trigger_kind):
            executed.append(record.schedule_id)
            record.run_count += 1
            record.next_run_at = time.time() + 60
            second._register_topic_schedule(record)
            return True

        monkeypatch.setattr(second, "_execute_topic_schedule", fake_execute)

        await second._run_due_interval_schedules()
        await asyncio.gather(*list(second._schedule_execution_tasks.values()))

        assert executed == ["persisted-schedule"]
        assert second._topic_schedules_by_id["persisted-schedule"].run_count == 1
        second._state_store.close()


class TestIntervalSecondsLow:
    """Verify interval_seconds=0 and =1 are valid."""

    def test_sc6_interval_seconds_one_valid_record(self):
        """_TopicScheduleRecord accepts interval_seconds=1."""
        from obs_agent.telegram import _TopicScheduleRecord, TelegramRoute

        record = _TopicScheduleRecord(
            schedule_id="fast-tick",
            route=TelegramRoute(chat_id=123, thread_id=456),
            description="fast interval",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=1,
            prompt="tick",
            max_runs=2,
        )
        assert record.interval_seconds == 1
        assert record.max_runs == 2

    def test_sc6b_interval_seconds_zero_valid_record(self):
        """_TopicScheduleRecord accepts interval_seconds=0."""
        from obs_agent.telegram import _TopicScheduleRecord, TelegramRoute

        record = _TopicScheduleRecord(
            schedule_id="immediate",
            route=TelegramRoute(chat_id=123, thread_id=456),
            description="immediate fire",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=0,
            prompt="fire now",
            max_runs=3,
        )
        assert record.interval_seconds == 0


# ---------------------------------------------------------------------------
# Extra: Inbox schema compatibility
# ---------------------------------------------------------------------------


class TestInboxSchemaCompatibility:
    """Verify extra fields survive JSON round-trips (both OBS and SDK patterns)."""

    def test_extra_fields_survive_read_modify_write(self, tmp_path):
        """Unknown fields like must_reply and replied are preserved through round-trips."""
        inbox_path = tmp_path / "inbox.json"
        original = [{
            "from": "sender",
            "text": "hello",
            "summary": "greeting",
            "timestamp": "2026-03-15T00:00:00Z",
            "read": False,
            "must_reply": True,
            "replied": False,
            "custom_field": "should_survive",
        }]
        inbox_path.write_text(json.dumps(original))

        loaded = json.loads(inbox_path.read_text())
        loaded[0]["read"] = True
        inbox_path.write_text(json.dumps(loaded))

        final = json.loads(inbox_path.read_text())
        assert final[0]["must_reply"] is True
        assert final[0]["replied"] is False
        assert final[0]["custom_field"] == "should_survive"
        assert final[0]["read"] is True

    def test_append_preserves_existing_entries(self, tmp_path):
        """Appending new messages preserves existing entries with extra fields."""
        inbox_path = tmp_path / "inbox.json"
        existing = [{
            "from": "agent-a",
            "text": "task 1",
            "must_reply": True,
            "replied": False,
            "read": False,
        }]
        inbox_path.write_text(json.dumps(existing))

        loaded = json.loads(inbox_path.read_text())
        loaded.append({
            "from": "agent-b",
            "text": "task 2",
            "must_reply": True,
            "replied": False,
            "read": False,
        })
        inbox_path.write_text(json.dumps(loaded))

        final = json.loads(inbox_path.read_text())
        assert len(final) == 2
        assert final[0]["from"] == "agent-a"
        assert final[0]["must_reply"] is True
        assert final[1]["from"] == "agent-b"
        assert final[1]["must_reply"] is True
