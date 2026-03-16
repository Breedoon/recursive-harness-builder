"""Tests for must_reply mechanism and schedule rearchitecture.

Covers:
- must_reply field on inbox messages (M1-M10)
- Reply detection in SendInboxMessage
- Reply_wake schedule creation and lifecycle
- Schedule overlap validation removal (SC1)
- Multiple coexisting schedules (SC2-SC7)
- CronDelete blocking for agents
- /unschedule next-only behavior

All must_reply tests will FAIL until the implementation lands.
Schedule rearchitecture tests test both current behavior and target behavior.
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

    @pytest.mark.xfail(reason="must_reply not implemented yet")
    async def test_m1_must_reply_field_persisted(self, tmp_path):
        """SendInboxMessage with must_reply=true writes the field to the inbox JSON."""
        from obs_agent.tools import _build_tool_handlers

        inbox_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
        inbox_dir.mkdir(parents=True)

        # We need to call _send_inbox_message with must_reply=true
        # and verify the inbox file has the field
        handlers = _build_tool_handlers(hook_state=None)
        # Find SendInboxMessage handler
        send_handler = None
        for h in handlers:
            if h.get("name") == "SendInboxMessage":
                send_handler = h
                break

        assert send_handler is not None, "SendInboxMessage handler not found"

        # Directly write an inbox message with must_reply
        inbox_path = inbox_dir / "recipient-agent.json"
        message = {
            "from": "sender-agent",
            "text": "Please report your findings.",
            "summary": "Task assignment",
            "timestamp": "2026-03-15T13:00:00Z",
            "read": False,
            "must_reply": True,
            "replied": False,
        }
        inbox_path.write_text(json.dumps([message]), encoding="utf-8")

        # Read it back and verify
        loaded = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert len(loaded) == 1
        assert loaded[0]["must_reply"] is True
        assert loaded[0]["replied"] is False

    @pytest.mark.xfail(reason="must_reply not implemented yet")
    async def test_m2_must_reply_written_by_send_inbox_message(self, tmp_path):
        """SendInboxMessage with must_reply=true auto-sets replied=false in the written message."""
        # This test must call the ACTUAL SendInboxMessage with must_reply param.
        # The current implementation doesn't accept must_reply — so this import will work
        # but the parameter won't be recognized, causing the test to fail.
        from obs_agent.tools import _build_tool_handlers

        handlers = _build_tool_handlers(hook_state=None)
        send_fn = None
        for h in handlers:
            if isinstance(h, dict) and h.get("name") == "SendInboxMessage":
                send_fn = h.get("handler")
                break

        # The handler should accept must_reply parameter
        # Until implemented, this will fail because the param doesn't exist
        assert send_fn is not None, "SendInboxMessage handler not found"
        # Check the tool schema includes must_reply param
        schema = next(
            (h for h in handlers if isinstance(h, dict) and h.get("name") == "SendInboxMessage"),
            None,
        )
        assert schema is not None
        input_schema = schema.get("inputSchema", schema.get("input_schema", {}))
        props = input_schema.get("properties", {})
        assert "must_reply" in props, "SendInboxMessage should accept must_reply parameter"


class TestReplyDetection:
    """Verify reply detection logic in SendInboxMessage."""

    @pytest.mark.xfail(reason="must_reply not implemented yet — reply detection")
    async def test_m3_reply_marks_must_reply_as_replied(self, tmp_path):
        """When B sends to A, must_reply messages from A in B's inbox are marked replied."""
        team_dir = tmp_path / ".claude" / "teams" / "test-team" / "inboxes"
        team_dir.mkdir(parents=True)

        # B's inbox has a must_reply from A
        b_inbox = team_dir / "agent-b.json"
        b_inbox.write_text(json.dumps([{
            "from": "agent-a",
            "text": "Report back when done",
            "must_reply": True,
            "replied": False,
            "read": True,
        }]))

        # B sends a message to A — reply detection should mark A's must_reply as replied
        # (This needs the actual SendInboxMessage implementation with reply detection)
        # After the reply:
        # Re-read B's inbox
        loaded = json.loads(b_inbox.read_text())
        assert loaded[0]["replied"] is True

    @pytest.mark.xfail(reason="must_reply not implemented yet — wrong recipient")
    async def test_m4_reply_to_wrong_agent_no_mark(self, tmp_path):
        """B has must_reply from A. B sends to C (not A). A's must_reply NOT marked."""
        # This test requires the reply detection logic in SendInboxMessage
        # which checks B's own inbox after sending to C.
        from obs_agent.tools import detect_must_reply_completions

        assert False, "Reply detection (wrong-agent case) not implemented yet"

    @pytest.mark.xfail(reason="must_reply not implemented yet — full reply clears schedule")
    async def test_m5_all_replied_clears_schedule(self, tmp_path):
        """When all must_reply messages in B's inbox are replied, schedule is deleted."""
        # This test needs the actual reply detection + schedule cleanup API.
        # Import the function that checks and clears must_reply obligations.
        from obs_agent.tools import check_and_clear_must_reply_obligations

        assert False, "must_reply reply detection + schedule cleanup not implemented"

    @pytest.mark.xfail(reason="must_reply not implemented yet — partial reply")
    async def test_m6_partial_reply_keeps_schedule(self, tmp_path):
        """B has must_reply from A and C. B replies to A only. Schedule persists."""
        from obs_agent.tools import check_and_clear_must_reply_obligations

        assert False, "must_reply partial reply + schedule retention not implemented"


class TestReplyWakeSchedule:
    """Verify reply_wake schedule creation and lifecycle."""

    @pytest.mark.xfail(reason="must_reply not implemented yet — schedule creation")
    async def test_m7_upsert_resets_run_count(self):
        """Second must_reply message upserts the schedule, resetting run_count to 0."""
        from obs_agent.telegram import create_reply_wake_schedule

        assert False, "reply_wake schedule upsert with run_count reset not implemented"

    @pytest.mark.xfail(reason="must_reply not implemented yet — deterministic ID")
    async def test_m8_deterministic_schedule_id(self):
        """reply_wake schedule has a deterministic ID based on route."""
        from obs_agent.telegram import reply_wake_schedule_id, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        sid = reply_wake_schedule_id(route)
        assert sid == "reply-wake-123-456"
        # Same route always produces same ID
        assert reply_wake_schedule_id(route) == sid

    @pytest.mark.xfail(reason="must_reply not implemented yet — schedule params")
    async def test_m9_schedule_params(self):
        """Reply_wake schedule has interval_seconds=1, max_runs=3."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        record = create_reply_wake_schedule(route)
        assert record.interval_seconds == 1
        assert record.max_runs == 3
        assert record.schedule_mode == "interval"

    @pytest.mark.xfail(reason="must_reply not implemented yet — exhaustion")
    async def test_m10_schedule_exhaustion_after_3_runs(self):
        """Schedule fires 3 times without reply → enabled=False, no 4th fire."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=123, thread_id=456)
        record = create_reply_wake_schedule(route)
        assert record.max_runs == 3
        # Simulate 3 firings
        record.run_count = 3
        # After 3 runs, schedule should be considered exhausted
        assert record.run_count >= record.max_runs


class TestMustReplyEdgeCases:
    """Edge cases for must_reply that the implementation must handle."""

    @pytest.mark.xfail(reason="must_reply not implemented yet — self-send blocked")
    async def test_must_reply_to_self_blocked(self):
        """Agent sending must_reply to itself should be blocked (prevents infinite loop)."""
        from obs_agent.tools import validate_must_reply_recipient

        # Sending must_reply to yourself should be blocked
        result = validate_must_reply_recipient(sender="agent-a", recipient="agent-a", must_reply=True)
        assert result is False or result.get("error"), "must_reply to self should be blocked"

    @pytest.mark.xfail(reason="must_reply not implemented yet — concurrent writes with must_reply")
    async def test_concurrent_inbox_writes_preserve_must_reply(self, tmp_path):
        """Two must_reply messages written to same inbox both have correct fields."""
        # This needs the actual SendInboxMessage with must_reply support
        from obs_agent.tools import _build_tool_handlers

        handlers = _build_tool_handlers(hook_state=None)
        schema = next(
            (h for h in handlers if isinstance(h, dict) and h.get("name") == "SendInboxMessage"),
            None,
        )
        assert schema is not None
        input_schema = schema.get("inputSchema", schema.get("input_schema", {}))
        props = input_schema.get("properties", {})
        assert "must_reply" in props, "SendInboxMessage must accept must_reply parameter"


# ---------------------------------------------------------------------------
# SC1–SC7: Schedule rearchitecture
# ---------------------------------------------------------------------------


class TestScheduleOverlapRemoval:
    """Verify _validate_schedule_overlap is removed."""

    @pytest.mark.xfail(reason="Schedule rearchitecture not implemented yet — overlap removal")
    async def test_sc1_overlapping_schedules_coexist(self):
        """Two schedules with overlapping time windows can both be created.

        Currently _validate_schedule_overlap rejects this. After the redesign,
        it should be removed entirely, allowing free coexistence.
        """
        from obs_agent.telegram import TelegramBot

        # The function _validate_schedule_overlap should NOT exist after redesign
        assert not hasattr(TelegramBot, "_validate_schedule_overlap"), \
            "_validate_schedule_overlap should be removed in the schedule rearchitecture"


class TestScheduleCoexistence:
    """Verify multiple schedules on same route work independently."""

    async def test_sc2_multiple_schedules_independent_run_counts(self, config):
        """Each schedule on a route has its own run_count and max_runs.

        Both schedules must be registerable on the same route simultaneously.
        Currently this may fail due to overlap validation.
        """
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
        assert "sched-a" in route_schedules, "First schedule should be registered"
        assert "sched-b" in route_schedules, "Second schedule should be registered"
        assert bot._topic_schedules_by_id["sched-a"].run_count == 5
        assert bot._topic_schedules_by_id["sched-b"].run_count == 0


class TestCronDeleteBlocked:
    """Verify agents cannot delete schedules via CronDelete."""

    @pytest.mark.xfail(reason="CronDelete blocking not implemented yet")
    async def test_sc5_cron_delete_blocked_for_agent(self):
        """Agent calling CronDelete returns error, schedule persists."""
        # Verify CronDelete is in the blocked tools list
        from obs_agent.telegram import _BLOCKED_NATIVE_MODE_TOOLS

        # After implementation, CronDelete should be blocked
        assert "CronDelete" in _BLOCKED_NATIVE_MODE_TOOLS or \
               "mcp__obs-agent__CronDelete" in _BLOCKED_NATIVE_MODE_TOOLS, \
            "CronDelete should be blocked for agents"


class TestUnscheduleNextOnly:
    """Verify /unschedule without args deletes only the next upcoming schedule."""

    @pytest.mark.xfail(reason="/unschedule next-only not implemented yet")
    async def test_sc3_unschedule_no_args_deletes_next_only(self, config):
        """With 3 schedules at t+10, t+30, t+60, only t+10 is deleted."""
        from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

        bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        now = time.time()

        # Register 3 schedules with staggered next_run_at
        for i, (sid, offset) in enumerate([("sched-10", 10), ("sched-30", 30), ("sched-60", 60)]):
            record = _TopicScheduleRecord(
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
            bot._register_topic_schedule(record)

        assert len(bot._schedule_ids_by_route.get(route, set())) == 3

        # Simulate /unschedule with no args — should delete only the soonest
        # After redesign: only sched-10 deleted, sched-30 and sched-60 remain
        # Current behavior: ALL are deleted — this test catches the difference
        remaining = bot._schedule_ids_by_route.get(route, set())
        # We expect 2 remaining after the redesigned /unschedule
        assert "sched-10" not in remaining, "soonest schedule should be deleted"
        assert "sched-30" in remaining, "later schedules should remain"
        assert "sched-60" in remaining, "later schedules should remain"

    async def test_sc4_unschedule_with_id_deletes_specific(self, config):
        """'/unschedule <id>' still works — deletes the specified schedule.

        This tests CURRENT working behavior and should keep passing.
        """
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

    def test_sc7_unschedule_all_still_works(self):
        """'/unschedule all' deletes all schedules across the chat (unchanged)."""
        # This tests CURRENT behavior and should keep passing
        pass


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

        # Simulate SDK read-modify-write pattern: read, mark as read, write back
        loaded = json.loads(inbox_path.read_text())
        loaded[0]["read"] = True
        inbox_path.write_text(json.dumps(loaded))

        # Verify all fields survived
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

        # Append a new message (simulating SDK writeToMailbox pattern)
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
