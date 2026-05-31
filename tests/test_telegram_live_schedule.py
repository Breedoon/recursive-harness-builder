"""Live Telegram schedule smoke tests for topic interval/on-stop behavior."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.test_telegram_live_forum_topics import (
    _LiveForumHarness,
    _message_containing,
    _wait_for_message_after_containing,
    live_tg_forum,  # fixture import
)


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestTelegramLiveSchedule:
    async def test_live_interval_schedule_runs_and_emits_completion_next_schedule(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Schedule Interval {tag}")
        schedule_name = f"INT-{tag}"
        schedule_token = f"SCHED-INT-{tag}"

        create_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=12, reset_session=false, max_runs=6, "
                f"description='{schedule_name}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {schedule_token}.' "
                f"After the tool call, reply with only CRON-CREATED-{tag}."
            ),
            thread_id=thread_id,
            timeout=180.0,
        )
        assert f"CRON-CREATED-{tag}" in create_trace.output, live_tg_forum.failure_context()
        assert "schedule created:" in create_trace.output.lower(), live_tg_forum.failure_context()

        created_marker = _message_containing(create_trace, f"CRON-CREATED-{tag}")
        triggered_marker = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=created_marker.message_id,
            token="schedule_triggered:",
            timeout=120.0,
        )
        assert schedule_name in triggered_marker.text, live_tg_forum.failure_context()
        assert "every 12s" in triggered_marker.text.lower(), live_tg_forum.failure_context()

        working_marker = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=triggered_marker.message_id,
            token="working",
            timeout=120.0,
        )
        scheduled_reply = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=working_marker.message_id,
            token=schedule_token,
            timeout=120.0,
        )
        assert schedule_token in scheduled_reply.text, live_tg_forum.failure_context()

        completion = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=scheduled_reply.message_id,
            token="next_schedule:",
            timeout=120.0,
        )
        lowered = completion.text.lower()
        assert "context:" in lowered, live_tg_forum.failure_context()
        assert "next_schedule:" in lowered, live_tg_forum.failure_context()
        assert schedule_name in completion.text, live_tg_forum.failure_context()
        assert " at " in lowered, live_tg_forum.failure_context()
        assert "remaining=5" in lowered, live_tg_forum.failure_context()
        assert "schedule_triggered:" not in lowered, live_tg_forum.failure_context()

    async def test_live_on_stop_schedule_fires_once_per_stop_and_no_self_loop(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Schedule Stop {tag}")
        stop_token = f"SCHED-STOP-{tag}"

        create_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=0, reset_session=false, max_runs=2, "
                f"description='STOP-{tag}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {stop_token}.' "
                f"After the tool call, reply with only STOP-CREATED-{tag}."
            ),
            thread_id=thread_id,
            timeout=180.0,
        )
        assert f"STOP-CREATED-{tag}" in create_trace.output, live_tg_forum.failure_context()

        # Allow any create-turn follow-up callbacks to settle before arming the stop trigger.
        for _ in range(3):
            try:
                await live_tg_forum.platform.wait_for_silence(thread_id=thread_id, seconds=3.0)
                break
            except AssertionError:
                await asyncio.sleep(1.0)

        arm_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        arm_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                f"Reply with only ARM-{tag}."
            ),
            thread_id=thread_id,
            timeout=120.0,
        )
        assert f"ARM-{tag}" in arm_trace.output, live_tg_forum.failure_context()

        first_fire = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=arm_baseline,
            token=stop_token,
            timeout=120.0,
        )
        assert stop_token in first_fire.text, live_tg_forum.failure_context()

        # If anti-loop gating is broken, on-stop schedules can rapidly self-retrigger.
        await asyncio.sleep(12.0)
        recent = await live_tg_forum.platform.get_recent_messages(thread_id=thread_id, limit=120)
        fires_after_arm = [
            message
            for message in recent
            if message.message_id > arm_baseline and stop_token in message.text
        ]
        assert len(fires_after_arm) == 1, live_tg_forum.failure_context()

    async def test_live_multi_topic_interval_schedules_stay_isolated_under_concurrency(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        topic_a = await live_tg_forum.platform.create_topic(f"Schedule A {tag}")
        topic_b = await live_tg_forum.platform.create_topic(f"Schedule B {tag}")

        token_a = f"SCHED-A-{tag}"
        token_b = f"SCHED-B-{tag}"

        create_a = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=12, reset_session=false, max_runs=20, "
                f"description='A-{tag}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {token_a}.' "
                f"After the tool call, reply with only CREATED-A-{tag}."
            ),
            thread_id=topic_a,
            timeout=150.0,
        )
        create_b = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=16, reset_session=false, max_runs=20, "
                f"description='B-{tag}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {token_b}.' "
                f"After the tool call, reply with only CREATED-B-{tag}."
            ),
            thread_id=topic_b,
            timeout=150.0,
        )
        assert f"CREATED-A-{tag}" in create_a.output, live_tg_forum.failure_context()
        assert f"CREATED-B-{tag}" in create_b.output, live_tg_forum.failure_context()

        # Exercise concurrent topic activity while schedules are pending.
        traffic_a = live_tg_forum.platform.send(
            f"This is a deterministic live scheduling test. Reply with only USER-A-{tag}.",
            thread_id=topic_a,
            timeout=90.0,
        )
        traffic_b = live_tg_forum.platform.send(
            f"This is a deterministic live scheduling test. Reply with only USER-B-{tag}.",
            thread_id=topic_b,
            timeout=90.0,
        )
        traffic_a_trace, traffic_b_trace = await asyncio.gather(traffic_a, traffic_b)
        user_a_marker = _message_containing(traffic_a_trace, f"USER-A-{tag}")
        user_b_marker = _message_containing(traffic_b_trace, f"USER-B-{tag}")

        fired_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=topic_a,
            after_message_id=user_a_marker.message_id,
            token=token_a,
            timeout=90.0,
        )
        fired_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=topic_b,
            after_message_id=user_b_marker.message_id,
            token=token_b,
            timeout=90.0,
        )
        assert token_a in fired_a.text, live_tg_forum.failure_context()
        assert token_b in fired_b.text, live_tg_forum.failure_context()

        recent_a = await live_tg_forum.platform.get_recent_messages(thread_id=topic_a, limit=120)
        recent_b = await live_tg_forum.platform.get_recent_messages(thread_id=topic_b, limit=120)
        assert not any(token_b in message.text for message in recent_a), live_tg_forum.failure_context()
        assert not any(token_a in message.text for message in recent_b), live_tg_forum.failure_context()

    async def test_live_cron_schedule_fires_on_wall_clock_boundary(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Schedule Cron {tag}")
        cron_token = f"SCHED-CRON-{tag}"

        create_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='cron', cron='* * * * *', reset_session=false, max_runs=2, "
                f"description='CRON-{tag}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {cron_token}.' "
                f"After the tool call, reply with only CRON-MODE-CREATED-{tag}."
            ),
            thread_id=thread_id,
            timeout=180.0,
        )
        assert f"CRON-MODE-CREATED-{tag}" in create_trace.output, live_tg_forum.failure_context()

        created_marker = _message_containing(create_trace, f"CRON-MODE-CREATED-{tag}")
        fired = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=created_marker.message_id,
            token=cron_token,
            timeout=150.0,
        )
        assert cron_token in fired.text, live_tg_forum.failure_context()

        completion = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=fired.message_id,
            token="next_schedule:",
            timeout=120.0,
        )
        assert "cron" in completion.text.lower(), live_tg_forum.failure_context()

    async def test_live_clear_keeps_schedule_and_unschedule_stops_future_runs(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Schedule Clear {tag}")
        token = f"SCHED-CLEAR-{tag}"

        create_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic live scheduling test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=20, reset_session=false, max_runs=8, "
                f"description='CLEAR-{tag}', "
                f"prompt='This is a deterministic live scheduling test. Reply with only {token}.' "
                f"After the tool call, reply with only CLEAR-CREATED-{tag}."
            ),
            thread_id=thread_id,
            timeout=180.0,
        )
        assert f"CLEAR-CREATED-{tag}" in create_trace.output, live_tg_forum.failure_context()

        create_marker = _message_containing(create_trace, f"CLEAR-CREATED-{tag}")
        first_fire = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=create_marker.message_id,
            token=token,
            timeout=120.0,
        )
        assert token in first_fire.text, live_tg_forum.failure_context()

        clear_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send_control(
            f"/clear@{live_tg_forum.bot_username}",
            thread_id=thread_id,
            timeout=60.0,
        )
        clear_notice = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=clear_baseline,
            token="session cleared; schedule was kept",
            timeout=120.0,
        )
        assert "/unschedule" in clear_notice.text.lower(), live_tg_forum.failure_context()

        second_fire = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=clear_notice.message_id,
            token=token,
            timeout=120.0,
        )
        assert token in second_fire.text, live_tg_forum.failure_context()

        unschedule_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send_control(
            f"/unschedule@{live_tg_forum.bot_username}",
            thread_id=thread_id,
            timeout=60.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=unschedule_baseline,
            token="unscheduled",
            timeout=120.0,
        )

        post_unschedule_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await asyncio.sleep(35.0)
        recent = await live_tg_forum.platform.get_recent_messages(thread_id=thread_id, limit=180)
        post_unschedule_fires = [
            message
            for message in recent
            if message.message_id > post_unschedule_baseline and token in message.text
        ]
        assert post_unschedule_fires == [], live_tg_forum.failure_context()
