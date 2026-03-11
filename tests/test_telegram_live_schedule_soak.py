"""Long-running Telegram schedule soak coverage."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.test_telegram_live_forum_topics import (
    _LiveForumHarness,
    _message_containing,
    _reset_general,
    _start_bot,
    _stop_bot,
    _wait_for_message_after_containing,
    live_tg_forum,  # fixture import
)


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_soak
@pytest.mark.timeout(5400)
class TestTelegramLiveScheduleSoak:
    async def test_live_one_hour_mixed_schedule_soak_with_restart(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        interval_topic = await live_tg_forum.platform.create_topic(f"Soak Interval {tag}")
        cron_topic = await live_tg_forum.platform.create_topic(f"Soak Cron {tag}")

        interval_token = f"SOAK-INT-{tag}"
        cron_token = f"SOAK-CRON-{tag}"

        interval_create = await live_tg_forum.platform.send(
            (
                "This is a deterministic soak test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=120, reset_session=false, max_runs=120, "
                f"description='SOAK-INT-{tag}', "
                f"prompt='This is a deterministic soak test. Reply with only {interval_token}.' "
                f"After the tool call, reply with only SOAK-INT-CREATED-{tag}."
            ),
            thread_id=interval_topic,
            timeout=180.0,
        )
        assert f"SOAK-INT-CREATED-{tag}" in interval_create.output, live_tg_forum.failure_context()

        cron_create = await live_tg_forum.platform.send(
            (
                "This is a deterministic soak test. "
                "Call CronCreate exactly once with "
                "schedule_mode='cron', cron='*/4 * * * *', reset_session=false, max_runs=120, "
                f"description='SOAK-CRON-{tag}', "
                f"prompt='This is a deterministic soak test. Reply with only {cron_token}.' "
                f"After the tool call, reply with only SOAK-CRON-CREATED-{tag}."
            ),
            thread_id=cron_topic,
            timeout=180.0,
        )
        assert f"SOAK-CRON-CREATED-{tag}" in cron_create.output, live_tg_forum.failure_context()

        interval_create_marker = _message_containing(interval_create, f"SOAK-INT-CREATED-{tag}")
        cron_create_marker = _message_containing(cron_create, f"SOAK-CRON-CREATED-{tag}")

        # Prove both schedules are alive before entering the long soak window.
        first_interval = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=interval_topic,
            after_message_id=interval_create_marker.message_id,
            token=interval_token,
            timeout=180.0,
        )
        first_cron = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=cron_topic,
            after_message_id=cron_create_marker.message_id,
            token=cron_token,
            timeout=320.0,
        )
        assert interval_token in first_interval.text, live_tg_forum.failure_context()
        assert cron_token in first_cron.text, live_tg_forum.failure_context()

        interval_seen_ids: set[int] = {first_interval.message_id}
        cron_seen_ids: set[int] = {first_cron.message_id}

        start = asyncio.get_running_loop().time()
        end = start + 3600.0
        restart_done = False
        fork_launches = 0
        chatter_index = 0
        next_chatter_at = start + 180.0
        next_fork_at = start + 900.0

        while True:
            now = asyncio.get_running_loop().time()
            if now >= end:
                break

            recent_interval = await live_tg_forum.platform.get_recent_messages(
                thread_id=interval_topic,
                limit=400,
            )
            recent_cron = await live_tg_forum.platform.get_recent_messages(
                thread_id=cron_topic,
                limit=400,
            )

            for message in recent_interval:
                if interval_token in message.text:
                    interval_seen_ids.add(message.message_id)
            for message in recent_cron:
                if cron_token in message.text:
                    cron_seen_ids.add(message.message_id)

            # Guard against cross-topic bleed.
            assert not any(cron_token in message.text for message in recent_interval), (
                live_tg_forum.failure_context()
            )
            assert not any(interval_token in message.text for message in recent_cron), (
                live_tg_forum.failure_context()
            )

            if not restart_done and (now - start) >= 1800.0:
                _stop_bot(live_tg_forum.proc)
                proc, log_file = _start_bot(
                    live_tg_forum.vault_path,
                    live_tg_forum.temp_root or (live_tg_forum.vault_path / ".tmp"),
                    state_db_path=live_tg_forum.state_db_path,
                )
                live_tg_forum.proc = proc
                live_tg_forum.log_file = log_file
                restart_done = True
                await asyncio.sleep(8.0)

                restart_probe = await live_tg_forum.platform.send(
                    f"This is a deterministic soak test. Reply with only SOAK-RESTART-OK-{tag}.",
                    thread_id=interval_topic,
                    timeout=180.0,
                )
                assert f"SOAK-RESTART-OK-{tag}" in restart_probe.output, live_tg_forum.failure_context()

            if now >= next_chatter_at:
                chatter_index += 1
                chatter_trace = await live_tg_forum.platform.send(
                    (
                        "This is a deterministic soak test. "
                        f"Reply with only SOAK-CHAT-{tag}-{chatter_index}."
                    ),
                    thread_id=interval_topic,
                    timeout=180.0,
                )
                assert f"SOAK-CHAT-{tag}-{chatter_index}" in chatter_trace.output, (
                    live_tg_forum.failure_context()
                )
                next_chatter_at += 180.0

            if now >= next_fork_at:
                fork_attempt = fork_launches + 1
                anchor_token = f"SOAK-FORK-ANCHOR-{tag}-{fork_attempt}"
                anchor_trace = await live_tg_forum.platform.send(
                    (
                        "This is a deterministic soak test. "
                        f"Reply with only {anchor_token}."
                    ),
                    thread_id=interval_topic,
                    timeout=180.0,
                )
                assert anchor_token in anchor_trace.output, live_tg_forum.failure_context()
                anchor_message = _message_containing(anchor_trace, anchor_token)
                fork_baseline = await live_tg_forum.platform.latest_bot_message_id(
                    thread_id=interval_topic
                )
                fork_trace = await live_tg_forum.platform.send_control(
                    f"/fork@{live_tg_forum.bot_username} SoakFork-{tag}-{fork_attempt}",
                    thread_id=interval_topic,
                    reply_to_message_id=anchor_message.message_id,
                    timeout=90.0,
                )
                try:
                    await _wait_for_message_after_containing(
                        live_tg_forum,
                        thread_id=interval_topic,
                        after_message_id=fork_baseline,
                        token="fork topic created",
                        timeout=180.0,
                    )
                    fork_launches += 1
                except AssertionError:
                    # Fork can fail if Telegram routing races on reply metadata;
                    # tolerate this and keep soak running.
                    assert "can't fork from this message" in fork_trace.output.lower(), (
                        live_tg_forum.failure_context()
                    )
                next_fork_at += 900.0

            await asyncio.sleep(20.0)

        final_interval = await live_tg_forum.platform.get_recent_messages(
            thread_id=interval_topic,
            limit=600,
        )
        final_cron = await live_tg_forum.platform.get_recent_messages(
            thread_id=cron_topic,
            limit=600,
        )
        for message in final_interval:
            if interval_token in message.text:
                interval_seen_ids.add(message.message_id)
        for message in final_cron:
            if cron_token in message.text:
                cron_seen_ids.add(message.message_id)

        assert restart_done, live_tg_forum.failure_context()
        assert fork_launches >= 1, live_tg_forum.failure_context()
        assert len(interval_seen_ids) >= 10, live_tg_forum.failure_context()
        assert len(cron_seen_ids) >= 8, live_tg_forum.failure_context()
        assert len(interval_seen_ids) <= 80, live_tg_forum.failure_context()
        assert len(cron_seen_ids) <= 40, live_tg_forum.failure_context()
