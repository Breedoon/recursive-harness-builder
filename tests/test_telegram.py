"""Tests for obs_agent.telegram - Telegram bot integration.

Covers the simplified Telegram runtime:
- per-turn chronological message flushing
- inline status + text rendering
- final context-summary completion behavior
- per-chat lock serialization
- background queue auto-delivery poller
"""

import asyncio
import json
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, TelegramError

from obs_agent.events import StatusEvent
from obs_agent.lineage import (
    ObsBootstrap,
    agent_name_for_lineage,
    format_root_display_name,
    root_team_key_for_lineage,
)
from obs_agent.queueing import QueuedMessage
from obs_agent.runner import DoneEvent, TextEvent, TurnEndEvent
from obs_agent.telegram import (
    FragmentBuffer,
    TelegramRoute,
    TelegramBot,
    _ForkTaskRecord,
    _PRIORITY_ASSISTANT,
    _TopicScheduleRecord,
    _RunOutcome,
    _TelegramMessageBinding,
    _clear_secondary_bot_commands,
    create_reply_wake_schedule,
    create_telegram_app,
    _set_bot_commands,
    run_telegram_bot,
)

# Near-zero gap for fast test execution (real default is 1.0s)
_TEST_GAP = 0.05


def _make_update(
    text: str,
    user_id: int = 12345,
    chat_id: int = 67890,
    message_id: int = 1,
    thread_id: int | None = None,
) -> MagicMock:
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_message.text = text
    update.effective_message.caption = None
    update.effective_message.chat_id = chat_id
    update.effective_message.message_id = message_id
    update.effective_message.message_thread_id = thread_id
    update.effective_message.reply_to_message = None
    update.effective_message.media_group_id = None
    update.effective_message.document = None
    update.effective_message.video = None
    update.effective_message.voice = None
    update.effective_message.audio = None
    update.effective_message.video_note = None
    update.effective_message.animation = None
    update.effective_message.sticker = None
    update.effective_message.photo = []
    update.effective_message.effective_attachment = None
    update.effective_message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_context() -> MagicMock:
    """Create a mock telegram context."""
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.args = []
    return ctx


def _expected_local_schedule_time(
    ts: float,
    *,
    now_ts: float,
    include_seconds: bool,
) -> str:
    now_dt = datetime.fromtimestamp(now_ts, timezone.utc).astimezone()
    value_dt = datetime.fromtimestamp(ts, timezone.utc).astimezone()
    tz_label = value_dt.tzname() or value_dt.strftime("%z") or "local"
    time_fmt = "%H:%M:%S" if include_seconds else "%H:%M"
    if value_dt.date() == now_dt.date():
        return f"today at {value_dt.strftime(time_fmt)} {tz_label}"
    if value_dt.date() == (now_dt + timedelta(days=1)).date():
        return f"tomorrow at {value_dt.strftime(time_fmt)} {tz_label}"
    full_fmt = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
    return f"{value_dt.strftime(full_fmt)} {tz_label}"


def _state(bot: TelegramBot, chat_id: int = 67890, thread_id: int | None = None):
    return bot._get_state(TelegramRoute(chat_id=chat_id, thread_id=thread_id))


class TestTelegramBotAuth:
    def test_state_db_inside_temp_root_is_rejected(self, config, tmp_path):
        config.telegram_temp_root = tmp_path / "tg-temp"
        config.telegram_state_db_path = config.telegram_temp_root / "telegram-state.sqlite3"
        with pytest.raises(ValueError, match="outside OBS_TELEGRAM_TEMP_ROOT"):
            TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

    async def test_allowed_user_passes(self, config):
        config.telegram_allowed_user_ids = [12345]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        events = [TextEvent(text="Hello"), TurnEndEvent(), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("hi", user_id=12345)
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        # receipt + working + content + completion summary
        assert ctx.bot.send_message.call_count == 4

    async def test_disallowed_user_rejected(self, config):
        config.telegram_allowed_user_ids = [99999]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        update = _make_update("hi", user_id=12345)
        ctx = _make_context()
        await bot.handle_message(update, ctx)

        ctx.bot.send_message.assert_not_called()


class TestTelegramMessageFlow:
    async def test_interrupt_marker_is_rendered_as_system_command(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        rendered = bot._render_turn_html([TextEvent(text="[Request interrupted by user]")])
        assert rendered == "<u><i>[Request interrupted by user]</i></u>"

    async def test_system_messages_reply_to_user_message(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [TextEvent(text="done"), TurnEndEvent(), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        calls = [c.kwargs for c in ctx.bot.send_message.call_args_list]
        assert calls[0]["reply_to_message_id"] == 42
        assert calls[1]["reply_to_message_id"] == 42
        assert calls[-1]["reply_to_message_id"] == 42

    async def test_assistant_messages_are_mapped_to_jsonl_uuid(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            TextEvent(text="mapped"),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=True),
            DoneEvent(),
        ]

        sent_ids = [101, 102, 103, 104]

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = sent_ids.pop(0)
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _state(bot).session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        binding = bot._message_map[(67890, 103)]
        assert binding.jsonl_uuid == "assistant-uuid"
        assert binding.session_id == "sid-1"
        assert bot._session_heads["sid-1"] == "assistant-uuid"

    async def test_status_only_assistant_message_is_mapped(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            StatusEvent(type="thinking", summary="thinking"),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=False),
            DoneEvent(),
        ]

        next_id = 101

        async def send_side_effect(**kwargs):
            nonlocal next_id
            message = MagicMock()
            message.message_id = next_id
            next_id += 1
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _state(bot).session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        assistant_bindings = [
            binding for binding in bot._message_map.values()
            if binding.jsonl_uuid == "assistant-uuid" and binding.role == "assistant"
        ]
        assert assistant_bindings

    async def test_trigger_user_message_is_mapped_when_sdk_emits_user_uuid(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            TurnEndEvent(jsonl_uuid="user-uuid", message_role="user", has_text=False),
            TextEvent(text="reply"),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=True),
            DoneEvent(),
        ]

        sent_ids = [101, 102, 103, 104]

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = sent_ids.pop(0)
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _state(bot).session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        binding = bot._message_map[(67890, 42)]
        assert binding.jsonl_uuid == "user-uuid"
        assert binding.session_id == "sid-1"
        assert binding.role == "user"

    async def test_status_markers_are_bound_to_underlying_turns(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            TurnEndEvent(jsonl_uuid="user-uuid", message_role="user", has_text=False),
            TextEvent(text="reply"),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=True),
            DoneEvent(),
        ]

        sent_ids = [101, 102, 103, 104]

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = sent_ids.pop(0)
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _state(bot).session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        assert bot._message_map[(67890, 101)].jsonl_uuid == "user-uuid"
        assert bot._message_map[(67890, 102)].jsonl_uuid == "user-uuid"
        assert bot._message_map[(67890, 104)].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[(67890, 104)].role == "assistant"

    async def test_status_markers_fall_back_to_assistant_uuid(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            TextEvent(text="reply"),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=True),
            DoneEvent(),
        ]

        sent_ids = [101, 102, 103, 104]

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = sent_ids.pop(0)
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _state(bot).session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        assert bot._message_map[(67890, 101)].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[(67890, 101)].role == "assistant"
        assert bot._message_map[(67890, 102)].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[(67890, 102)].role == "assistant"

    async def test_turn_contains_inline_status_and_text(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            StatusEvent(type="tool_use", summary="Read: CLAUDE.md"),
            TextEvent(text="Hello from tool run"),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        assert ctx.bot.send_message.call_count == 4

        calls = [c.kwargs for c in ctx.bot.send_message.call_args_list]
        assert calls[0]["text"] == "<u><i>received</i></u>"
        assert calls[1]["text"] == "<u><i>working</i></u>"
        assert calls[2]["parse_mode"] == "HTML"
        assert calls[2]["disable_notification"] is True
        assert "<i>Read: CLAUDE.md</i>" in calls[2]["text"]
        assert "Hello from tool run" in calls[2]["text"]
        assert calls[3]["text"] == "<u><i>context: 0 / 1m</i></u>"
        assert calls[3]["disable_notification"] is False

    async def test_thinking_content_is_rendered_verbatim(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            StatusEvent(type="thinking", summary="I should inspect CLAUDE.md first."),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        calls = [c.kwargs for c in ctx.bot.send_message.call_args_list]
        assert "<i>I should inspect CLAUDE.md first.</i>" in calls[2]["text"]

    async def test_queue_delivery_emits_working_when_queued_message_is_delivered(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=1,
                messages=["follow-up while busy"],
            ),
            TurnEndEvent(jsonl_uuid="assistant-uuid", message_role="assistant", has_text=False),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                _ = msg
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        texts = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
        working_count = sum(1 for text in texts if text == "<u><i>working</i></u>")
        assert working_count == 2

    async def test_notification_status_renders_system_heading_and_cursive_body(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            StatusEvent(
                type="notification",
                summary="notification: task_notification",
                messages=["task_id: task-123", "worker-a finished"],
            ),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        calls = [c.kwargs for c in ctx.bot.send_message.call_args_list]
        assert "<u><i>notification: task_notification</i></u>" in calls[2]["text"]
        assert "<i>task_id: task-123</i>" in calls[2]["text"]
        assert "<i>worker-a finished</i>" in calls[2]["text"]

    async def test_sends_one_content_message_per_turn(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [
            TextEvent(text="turn one"),
            TurnEndEvent(),
            TextEvent(text="turn two"),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        assert ctx.bot.send_message.call_count == 5
        calls = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
        assert calls[0] == "<u><i>received</i></u>"
        assert calls[1] == "<u><i>working</i></u>"
        assert "turn one" in calls[2]
        assert "turn two" in calls[3]
        assert calls[4] == "<u><i>context: 0 / 1m</i></u>"

    async def test_completion_summary_omits_username_when_configured(self, config):
        config.telegram_notify_username = "breedoon"
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [TextEvent(text="done"), TurnEndEvent(), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        calls = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
        assert calls[-1] == "<u><i>context: 0 / 1m</i></u>"

    async def test_attachment_receipt_is_sent_before_normalization(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("", message_id=42)
        update.effective_message.text = None
        update.effective_message.document = MagicMock()
        update.effective_message.effective_attachment = object()
        ctx = _make_context()
        receipt_message = MagicMock()
        receipt_message.message_id = 900
        ctx.bot.send_message = AsyncMock(return_value=receipt_message)

        normalize_started = asyncio.Event()
        release_normalize = asyncio.Event()

        async def fake_normalize(_update):
            normalize_started.set()
            await release_normalize.wait()
            return MagicMock(agent_text="normalized attachment", user_warnings=[])

        with patch.object(bot._normalizer, "normalize_update", side_effect=fake_normalize), patch.object(
            bot, "_process_message", new_callable=AsyncMock
        ) as mock_process:
            task = asyncio.create_task(bot.handle_message(update, ctx))
            await normalize_started.wait()

            assert ctx.bot.send_message.await_count == 1
            assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>received</i></u>"
            assert ctx.bot.send_message.call_args.kwargs["reply_to_message_id"] == 42

            release_normalize.set()
            await task

        assert mock_process.await_count == 1
        assert mock_process.call_args.kwargs["pre_sent_status_message_ids"] == [900]

    async def test_media_group_receipt_is_sent_once_on_first_item(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        ctx = _make_context()

        first_receipt = MagicMock()
        first_receipt.message_id = 901
        ctx.bot.send_message = AsyncMock(return_value=first_receipt)

        update1 = _make_update("", message_id=51)
        update1.effective_message.text = None
        update1.effective_message.media_group_id = "album-1"
        update2 = _make_update("", message_id=52)
        update2.effective_message.text = None
        update2.effective_message.media_group_id = "album-1"

        with patch.object(bot._media_group_buffer, "add", new_callable=AsyncMock) as mock_add:
            await bot.handle_message(update1, ctx)
            await bot.handle_message(update2, ctx)

        assert ctx.bot.send_message.await_count == 1
        assert mock_add.await_count == 2
        assert bot._media_group_receipt_ids[(67890, 12345, None, "album-1")] == [901]


class TestPerChatLock:
    async def test_busy_follow_up_is_enqueued_instead_of_starting_second_turn(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        started: list[str] = []
        release = asyncio.Event()

        async def fake_run_and_send(**kwargs):
            text = kwargs["user_text"]
            started.append(text)
            await release.wait()

        with patch.object(bot, "_run_and_send", side_effect=fake_run_and_send):
            ctx = _make_context()
            u1 = _make_update("first", message_id=1)
            u2 = _make_update("second", message_id=2)

            t1 = asyncio.create_task(bot._process_message("first", u1, ctx))
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(bot._process_message("second", u2, ctx))
            await asyncio.sleep(0.01)

            assert started == ["first"]
            assert _state(bot).hook_state.message_queue.get_nowait() == QueuedMessage(
                text="second",
                telegram_message_id=2,
                reply_to_message_id=None,
            )

            release.set()
            await asyncio.gather(t1, t2)

        texts = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
        assert "<u><i>queued</i></u>" not in texts


class TestBackgroundPoller:
    async def test_auto_delivers_queued_messages_when_idle(self, config):
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            background_poll_seconds=0.01,
            enable_background_poller=True,
        )
        fake_ptb_bot = MagicMock()
        fake_ptb_bot.send_message = AsyncMock()

        state = _state(bot)
        state.last_bot = fake_ptb_bot
        state.hook_state.message_queue.put_nowait("queued bg result")

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            await asyncio.sleep(0.06)
            await bot.shutdown()

            assert mock_run.called
            kwargs = mock_run.call_args.kwargs
            assert kwargs["state"] is state
            assert kwargs["extra_pending"] == [QueuedMessage(text="queued bg result")]
            assert "queued updates arrived while idle" in kwargs["user_text"]

    async def test_auto_delivery_waits_for_transport_backlog_to_drain(self, config):
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            background_poll_seconds=0.01,
            enable_background_poller=True,
        )
        fake_ptb_bot = MagicMock()
        fake_ptb_bot.send_message = AsyncMock()

        state = _state(bot)
        state.last_bot = fake_ptb_bot
        state.hook_state.message_queue.put_nowait("queued while chat still draining")
        bot._chat_pending_ops[state.route.chat_id] = 1

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            await asyncio.sleep(0.05)
            assert mock_run.await_count == 0

            bot._chat_pending_ops.pop(state.route.chat_id, None)
            await asyncio.sleep(0.06)
            await bot.shutdown()

            assert mock_run.await_count == 1
            kwargs = mock_run.call_args.kwargs
            assert kwargs["state"] is state
            assert kwargs["extra_pending"] == [QueuedMessage(text="queued while chat still draining")]
            assert "queued updates arrived while idle" in kwargs["user_text"]

    async def test_auto_delivery_stress_respects_transport_backlog(self, config):
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            background_poll_seconds=0.005,
            enable_background_poller=True,
        )
        fake_ptb_bot = MagicMock()
        fake_ptb_bot.send_message = AsyncMock()

        state = _state(bot)
        state.last_bot = fake_ptb_bot
        chat_id = state.route.chat_id

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            for attempt in range(8):
                state.hook_state.message_queue.put_nowait(f"stress-queued-{attempt}")
                bot._chat_pending_ops[chat_id] = 1

                blocked_count = mock_run.await_count
                await asyncio.sleep(0.01 + (attempt % 3) * 0.005)
                assert mock_run.await_count == blocked_count

                bot._chat_pending_ops.pop(chat_id, None)
                deadline = asyncio.get_running_loop().time() + 0.25
                while mock_run.await_count == blocked_count and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.005)

                assert mock_run.await_count == blocked_count + 1

            await bot.shutdown()

    async def test_inbox_poll_wakes_idle_team_worker_without_notifier(self, config, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            background_poll_seconds=0.01,
            enable_background_poller=True,
        )
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert child_state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=930))
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-child"] = "child-head-uuid"

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Old prompt",
            prompt_file="old.md",
            prompt_file_content="old file content",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(
            json.dumps(
                [
                    {
                        "from": "worker-b",
                        "text": "poll wake message",
                        "summary": "handoff",
                        "timestamp": "2026-03-13T00:00:00Z",
                        "read": False,
                    }
                ],
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            try:
                await bot._ensure_background_poller(fake_bot)
                deadline = asyncio.get_running_loop().time() + 0.35
                while schedule_mock.await_count == 0 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
            finally:
                await bot.shutdown()

        assert schedule_mock.await_count == 1
        schedule_mock.assert_awaited_once_with(task_id="task-team", parent_state=child_state)
        assert record.status == "launched"
        assert record.idle_ready is False
        assert record.prompt_file is None
        assert record.prompt_file_content is None
        assert "Latest sender: worker-b." in record.prompt
        assert "Latest content preview: poll wake message" in record.prompt

    async def test_run_and_send_preserves_pending_across_session_switch(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.pending_messages = [
            QueuedMessage(
                text="reply while busy",
                telegram_message_id=10,
                reply_to_message_id=5,
            )
        ]

        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                if False:
                    yield

            instance.run = mock_run
            instance.remaining_pending = []

            async def fake_resolve_session_for_trigger(**kwargs):
                state.pending_messages = []
                return True, 10

            with patch.object(
                bot,
                "_resolve_session_for_trigger",
                side_effect=fake_resolve_session_for_trigger,
            ):
                await bot._run_and_send(
                    state=state,
                    user_text="(System: queued updates arrived while idle. Process and summarize them.)",
                    bot=fake_bot,
                    trigger_message=QueuedMessage(
                        text="reply while busy",
                        telegram_message_id=10,
                        reply_to_message_id=5,
                    ),
                )

        assert mock_runner.call_args.kwargs["pending_messages"] == [
            QueuedMessage(
                text="reply while busy",
                telegram_message_id=10,
                reply_to_message_id=5,
            )
        ]

    async def test_run_and_send_injects_interrupt_notice_once(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        state.hook_state.interrupt_notice_pending = True
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
        captured_prompt: list[str] = []

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                captured_prompt.append(msg)
                yield DoneEvent()

            instance.run = mock_run
            instance.remaining_pending = []
            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        assert len(captured_prompt) == 1
        assert "user interrupted your previous response via /stop" in captured_prompt[0]
        assert captured_prompt[0].strip().endswith("hello")
        assert state.hook_state.interrupt_notice_pending is False

    async def test_run_and_send_primes_trunk_lineage_before_first_client_connect(
        self,
        config,
        monkeypatch,
        tmp_path,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)

        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
        observed: dict[str, object] = {}

        async def fake_get_client():
            observed["session_id"] = state.session_id
            observed["env"] = state.session_manager.sdk_env_overrides
            return MagicMock()

        with (
            patch.object(state.session_manager, "get_client", AsyncMock(side_effect=fake_get_client)),
            patch("obs_agent.telegram.ConversationRunner") as mock_runner,
        ):
            instance = mock_runner.return_value

            async def mock_run(msg):
                observed["prompt"] = msg
                yield DoneEvent()

            instance.run = mock_run
            instance.remaining_pending = []
            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        assert observed.get("session_id") is None
        assert state.agent_lineage == ("General",)
        env = observed["env"]
        assert isinstance(env, dict)
        # Team key has timestamp prefix + slug — verify format, not exact value
        assert env["CLAUDE_CODE_TEAM_NAME"].endswith("-general")
        assert env["CLAUDE_CODE_TASK_LIST_ID"] == env["CLAUDE_CODE_TEAM_NAME"]
        # Bug 4 fix: trunk agent_name = full team key
        assert env["CLAUDE_CODE_AGENT_NAME"] == env["CLAUDE_CODE_TEAM_NAME"]
        prompt = str(observed.get("prompt") or "")
        assert "<obs-bootstrap" in prompt  # bootstrap is prepended (after system-note)
        assert "<origin>trunk_start</origin>" in prompt
        assert "<session_id>" not in prompt
        assert prompt.strip().endswith("hello")

    async def test_prime_obs_bootstrap_preserves_agent_task_env_overrides(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=223)
        state = bot._get_state(route, topic_title="Alpha")
        assert state is not None
        state.session_manager.set_sdk_env_overrides({"AF_SERVICE_WRITE_FILE": "/tmp/report.md"})

        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Alpha"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id="sid-child",
        )

        env = state.session_manager.sdk_env_overrides
        assert env["AF_SERVICE_WRITE_FILE"] == "/tmp/report.md"
        assert env["CLAUDE_CODE_TEAM_NAME"].endswith("-root")
        assert state.hook_state.sdk_env_overrides["AF_SERVICE_WRITE_FILE"] == "/tmp/report.md"

    async def test_run_and_send_injects_pending_child_bootstrap_even_when_fork_session_has_parent_head(
        self,
        config,
        monkeypatch,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=222)
        state = bot._get_state(route, topic_title="Alpha")
        assert state is not None
        state.session_manager.set_session_id("sid-child")
        bot._bind_state_session(state)
        bot._set_session_head(session_id="sid-child", jsonl_uuid="uuid-parent-head")
        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Alpha"),
            origin="agent_task_fork",
            is_fork=True,
            session_id="sid-child",
            parent_session_id="sid-root",
        )

        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=778))
        captured_prompt: list[str] = []
        stale_parent_bootstrap = ObsBootstrap(
            raw_xml=(
                "<obs-bootstrap version='2'>"
                "<obs-lineage><obs-node display_name='Root' agent_name='2026-03-31-10-00-root' /></obs-lineage>"
                "<fork_context><origin>trunk_start</origin><is_fork>false</is_fork><session_id>sid-child</session_id></fork_context>"
                "<team_context><root_team_key>2026-03-31-10-00-root</root_team_key><agent_name>2026-03-31-10-00-root</agent_name></team_context>"
                "</obs-bootstrap>"
            ),
            lineage=("Root",),
            origin="trunk_start",
            is_fork=False,
            session_id="sid-child",
            agent_id=None,
            parent_session_id=None,
            root_team_key="2026-03-31-10-00-root",
            agent_name="2026-03-31-10-00-root",
            parent_agent_name=None,
            parent_display_name=None,
        )

        with (
            patch.object(state.session_manager, "get_client", AsyncMock(return_value=MagicMock())),
            patch(
                "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
                return_value=stale_parent_bootstrap,
            ),
            patch("obs_agent.telegram.ConversationRunner") as mock_runner,
        ):
            instance = mock_runner.return_value

            async def mock_run(msg):
                captured_prompt.append(msg)
                yield DoneEvent()

            instance.run = mock_run
            instance.remaining_pending = []
            await bot._run_and_send(
                state=state,
                user_text="hello from child",
                bot=fake_bot,
            )

        assert len(captured_prompt) == 1
        assert state.pending_obs_bootstrap is not None
        assert state.pending_obs_bootstrap in captured_prompt[0]
        assert "<obs-node display_name=\"Alpha\"" in captured_prompt[0]
        await bot.shutdown()

    async def test_run_and_send_summary_uses_assistant_transport_priority(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=501))

        with (
            patch("obs_agent.telegram.ConversationRunner") as mock_runner,
            patch.object(
                bot,
                "_send_system_message",
                new_callable=AsyncMock,
            ) as mock_send_system,
        ):
            mock_send_system.return_value = MagicMock(message_id=902)
            instance = mock_runner.return_value

            async def mock_run(msg):
                _ = msg
                yield DoneEvent()

            instance.run = mock_run
            instance.remaining_pending = []

            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        summary_calls = [
            call for call in mock_send_system.await_args_list
            if "text" in call.kwargs and str(call.kwargs["text"]).startswith("context:")
        ]
        assert summary_calls, "Expected completion summary call"
        assert summary_calls[-1].kwargs.get("priority") == _PRIORITY_ASSISTANT

    async def test_run_and_send_drops_pending_messages_after_interrupt(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=601))

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value
            instance.remaining_pending = [QueuedMessage(text="queued-after-stop")]

            async def mock_run(msg):
                _ = msg
                state.hook_state.interrupt_requested = True
                yield DoneEvent()

            instance.run = mock_run
            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        assert instance.remaining_pending == [QueuedMessage(text="queued-after-stop")]
        assert state.pending_messages == []

    async def test_run_and_send_completion_summary_reports_interrupt_discard_count(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=601))

        with (
            patch("obs_agent.telegram.ConversationRunner") as mock_runner,
            patch.object(bot, "_send_system_message", new_callable=AsyncMock) as mock_send_system,
        ):
            mock_send_system.return_value = MagicMock(message_id=902)
            instance = mock_runner.return_value
            instance.remaining_pending = [
                QueuedMessage(text="queued-after-stop-a"),
                QueuedMessage(text="queued-after-stop-b"),
            ]

            async def mock_run(msg):
                _ = msg
                state.hook_state.interrupt_requested = True
                yield DoneEvent()

            instance.run = mock_run
            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        summary_texts = [str(call.kwargs.get("text", "")) for call in mock_send_system.await_args_list]
        assert any(
            "interrupted; 2 queued messages discarded" in text and "context:" in text
            for text in summary_texts
        )

    async def test_run_and_send_completion_summary_omits_zero_discard_suffix(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=601))

        with (
            patch("obs_agent.telegram.ConversationRunner") as mock_runner,
            patch.object(bot, "_send_system_message", new_callable=AsyncMock) as mock_send_system,
        ):
            mock_send_system.return_value = MagicMock(message_id=903)
            instance = mock_runner.return_value
            instance.remaining_pending = []

            async def mock_run(msg):
                _ = msg
                state.hook_state.interrupt_requested = True
                yield DoneEvent()

            instance.run = mock_run
            await bot._run_and_send(
                state=state,
                user_text="hello",
                bot=fake_bot,
            )

        summary_texts = [str(call.kwargs.get("text", "")) for call in mock_send_system.await_args_list]
        assert any("interrupted" in text and "context:" in text for text in summary_texts)
        assert all("queued messages discarded" not in text for text in summary_texts)

    async def test_route_warning_is_deprecated(self, config):
        config.telegram_notify_username = "breedoon"
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.session_manager.set_session_id("sid-main")
        state.session_manager.last_activity = 0.0
        state.last_bot = MagicMock()

        with patch("obs_agent.telegram.time.time", return_value=(50 * 60) + 1), patch.object(
            bot, "_send_system_message", new_callable=AsyncMock
        ) as mock_send:
            await bot._maybe_send_route_warning(state)
            await bot._maybe_send_route_warning(state)

        mock_send.assert_not_called()

    async def test_route_warning_waits_until_idle_threshold(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.session_manager.set_session_id("sid-fork")
        state.session_manager.last_activity = (50 * 60) - 5
        state.last_bot = MagicMock()

        with patch("obs_agent.telegram.time.time", return_value=(50 * 60) + 1), patch.object(
            bot, "_send_system_message", new_callable=AsyncMock
        ) as mock_send:
            await bot._maybe_send_route_warning(state)

        mock_send.assert_not_called()


class TestTopicScheduling:
    async def test_hook_context_snapshot_provider_exposes_route_topic_team_and_model(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=70)
        state = bot._get_state(route, topic_title="Root - Worker")
        assert state is not None
        state.session_manager.set_session_id("sid-worker")
        state.session_manager.model_override = "gpt-5.4-mini[200k]"
        state.agent_lineage = ("Root", "Worker")
        state.session_manager.set_sdk_env_overrides(
            {
                "CLAUDE_CODE_TEAM_NAME": "root-team",
                "CLAUDE_CODE_AGENT_NAME": "worker-agent",
            }
        )
        bot._session_heads["sid-worker"] = "uuid-head"

        snapshot = state.hook_state.context_snapshot_provider()

        assert snapshot["route"] == {"chat_id": 67890, "thread_id": 70}
        assert snapshot["topic"]["title"] == "Root - Worker"
        assert snapshot["session"] == {"session_id": "sid-worker", "head_uuid": "uuid-head"}
        assert snapshot["team"] == {
            "team_name": "root-team",
            "agent_name": "worker-agent",
            "lineage": ["Root", "Worker"],
        }
        assert snapshot["effective_model"] == "gpt-5.4-mini[200k]"

    async def test_execute_schedule_sets_runtime_schedule_context(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=71)
        state = bot._get_state(route, topic_title="Scheduled")
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=100))
        state.last_bot = fake_bot
        record = _TopicScheduleRecord(
            schedule_id="sched-runtime",
            route=route,
            description="runtime context",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=10,
            prompt="run",
            max_runs=1,
            next_run_at=0,
        )
        bot._register_topic_schedule(record)
        seen: dict[str, object] = {}

        async def fake_run_and_send(**kwargs):
            seen["schedule_run_active"] = state.hook_state.schedule_run_active
            seen["triggered_schedule_id"] = state.hook_state.triggered_schedule_id
            seen["active_schedule"] = dict(state.hook_state.active_schedule or {})
            seen["triggered_kwarg"] = kwargs["triggered_schedule_id"]
            return _RunOutcome(assistant_text="ok")

        with patch.object(bot, "_run_and_send", side_effect=fake_run_and_send), patch.object(
            bot,
            "_send_system_message",
            new_callable=AsyncMock,
        ):
            ran = await bot._execute_topic_schedule(record=record, trigger_kind="interval")

        assert ran is True
        assert seen["schedule_run_active"] is True
        assert seen["triggered_schedule_id"] == "sched-runtime"
        assert seen["triggered_kwarg"] == "sched-runtime"
        assert seen["active_schedule"]["id"] == "sched-runtime"
        assert seen["active_schedule"]["description"] == "runtime context"
        assert state.hook_state.schedule_run_active is False
        assert state.hook_state.triggered_schedule_id is None
        assert state.hook_state.active_schedule is None

    async def test_default_schedule_is_seeded_from_settings_on_new_route(self, config):
        settings_path = config.vault_path / ".claude" / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "obs_agent": {
                        "schedule_defaults": {
                            "default_interval": {
                                "enabled": True,
                                "interval_seconds": 120,
                                "prompt": "default run",
                                "description": "DefaultSchedule",
                                "max_runs": 3,
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=70)
        state = bot._get_state(route)
        assert state is not None

        schedule_ids = bot._schedule_ids_by_route.get(route, set())
        assert len(schedule_ids) == 1
        record = bot._topic_schedules_by_id[next(iter(schedule_ids))]
        assert record.description == "DefaultSchedule"
        assert record.prompt == "default run"
        assert record.interval_seconds == 120
        assert record.max_retry_attempts == 0
        assert record.max_runs == 3
        assert record.enabled is True

    async def test_default_schedule_respects_disabled_flag(self, config):
        settings_path = config.vault_path / ".claude" / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "obs_agent": {
                        "schedule_defaults": {
                            "default_interval": {
                                "enabled": False,
                                "interval_seconds": 120,
                                "prompt": "default run",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=71)
        state = bot._get_state(route)
        assert state is not None
        assert bot._schedule_ids_by_route.get(route, set()) == set()

    async def test_default_schedule_can_load_from_obs_namespace(self, config):
        settings_path = config.vault_path / ".claude" / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "obs": {
                        "scheduling": {
                            "retry": {
                                "max_attempts": 2,
                                "delay_seconds": 45,
                            },
                            "defaults": {
                                "schedule": {
                                    "enabled": True,
                                    "schedule_mode": "interval",
                                    "interval_seconds": 180,
                                    "prompt": "obs default run",
                                    "description": "ObsDefault",
                                    "reset_session": True,
                                    "max_runs": 4,
                                    "inherit": "fork",
                                }
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=72)
        state = bot._get_state(route)
        assert state is not None

        schedule_ids = bot._schedule_ids_by_route.get(route, set())
        assert len(schedule_ids) == 1
        record = bot._topic_schedules_by_id[next(iter(schedule_ids))]
        assert record.description == "ObsDefault"
        assert record.prompt == "obs default run"
        assert record.interval_seconds == 180
        assert record.reset_session is True
        assert record.max_runs == 4
        assert record.inherit_mode == "fork"
        assert record.max_retry_attempts == 2
        assert record.retry_delay_seconds == 45

    async def test_cron_create_fails_on_unsupported_expression(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=77)
        state = bot._get_state(route)
        assert state is not None

        result = await bot._cron_create(
            route=route,
            args={
                "cron": "not-a-cron",
                "prompt": "run",
            },
        )

        assert result["is_error"] is True
        assert "invalid cron expression" in result["content"][0]["text"]

    async def test_cron_create_supports_on_stop_via_interval_zero(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=78)
        state = bot._get_state(route)
        assert state is not None

        result = await bot._cron_create(
            route=route,
            args={
                "cron": "*/5 * * * *",
                "prompt": "run stop",
                "interval_seconds": 0,
                "description": "StopRunner",
            },
        )

        assert "schedule" in result["tool_use_result"]
        schedules = bot._schedule_ids_by_route[route]
        assert len(schedules) == 1
        schedule_id = next(iter(schedules))
        record = bot._topic_schedules_by_id[schedule_id]
        assert record.trigger_kind == "on_topic_stop"
        assert record.next_run_at is None

    async def test_cron_create_defaults_to_one_run_without_retries(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=88)
        state = bot._get_state(route)
        assert state is not None

        result = await bot._cron_create(
            route=route,
            args={
                "cron": "*/5 * * * *",
                "prompt": "run once",
                "interval_seconds": 30,
            },
        )

        assert "is_error" not in result
        schedules = bot._schedule_ids_by_route[route]
        assert len(schedules) == 1
        record = bot._topic_schedules_by_id[next(iter(schedules))]
        assert record.max_runs == 1
        assert record.max_retry_attempts == 0
        assert record.retry_delay_seconds == 30
        assert record.retry_attempt_count == 0

    async def test_cron_create_supports_wall_clock_cron_mode(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=889)
        state = bot._get_state(route)
        assert state is not None

        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            result = await bot._cron_create(
                route=route,
                args={
                    "schedule_mode": "cron",
                    "cron": "*/5 * * * *",
                    "prompt": "run cron",
                },
            )

        assert "is_error" not in result
        schedule_id = next(iter(bot._schedule_ids_by_route[route]))
        record = bot._topic_schedules_by_id[schedule_id]
        assert record.schedule_mode == "cron"
        assert record.trigger_kind == "cron"
        assert record.next_run_at == 1200.0

    async def test_cron_create_allows_overlapping_windows(self, config):
        """Overlapping schedule windows are now allowed (overlap validation removed)."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=891)
        state = bot._get_state(route)
        assert state is not None

        first = await bot._cron_create(
            route=route,
            args={
                "schedule_mode": "interval",
                "interval_seconds": 60,
                "cron": "*/1 * * * *",
                "prompt": "first",
                "from": "2030-03-10T10:00:00Z",
                "until": "2030-03-10T11:00:00Z",
            },
        )
        assert "is_error" not in first

        # Previously this would be rejected — now it should succeed
        overlap = await bot._cron_create(
            route=route,
            args={
                "schedule_mode": "interval",
                "interval_seconds": 120,
                "cron": "*/2 * * * *",
                "prompt": "second",
                "from": "2030-03-10T10:30:00Z",
                "until": "2030-03-10T11:30:00Z",
            },
        )
        assert "is_error" not in overlap, "Overlapping windows should now be allowed"

        # Both schedules should coexist
        route_schedules = bot._schedule_ids_by_route.get(route, set())
        assert len(route_schedules) == 2

    async def test_due_interval_schedule_executes_once_and_advances(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=79)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-int",
            route=route,
            description="Interval",
            cron_expr="*/2 * * * *",
            trigger_kind="interval",
            interval_seconds=120,
            prompt="run interval",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=0.0,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock, patch("obs_agent.telegram.time.time", return_value=1000.0):
            await bot._run_due_interval_schedules()

        run_mock.assert_awaited_once()
        updated = bot._topic_schedules_by_id["sched-int"]
        assert updated.run_count == 1
        assert updated.last_success_at == 1000.0
        assert updated.next_run_at == 1120.0

    async def test_schedule_run_summary_uses_updated_interval_eta_and_remaining(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=791)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-summary",
            route=route,
            description="SummaryJob",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run summary",
            schedule_mode="interval",
            reset_session=False,
            max_runs=3,
            next_run_at=0.0,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock, patch.object(bot, "_send_system_message", new=AsyncMock()) as send_mock, patch(
            "obs_agent.telegram.time.time", return_value=1000.0
        ):
            ran = await bot._execute_topic_schedule(record=record, trigger_kind="interval")

        assert ran is True
        run_mock.assert_awaited_once()
        updated = bot._topic_schedules_by_id["sched-summary"]
        assert updated.run_count == 1
        assert updated.next_run_at == 1060.0
        summary_texts = [str(call.kwargs.get("text", "")) for call in send_mock.await_args_list]
        assert any("schedule_triggered: SummaryJob (every 1m ; remaining=2)" in text for text in summary_texts)
        next_local = _expected_local_schedule_time(1060.0, now_ts=1000.0, include_seconds=False)
        assert any(f"next_schedule: SummaryJob at {next_local}" in text for text in summary_texts)
        assert any("remaining=2" in text for text in summary_texts)

    async def test_schedule_run_emits_trigger_marker_before_completion_summary(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=7911)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-trigger-order",
            route=route,
            description="OrderJob",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run order",
            schedule_mode="interval",
            reset_session=False,
            max_runs=2,
            next_run_at=0.0,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock, patch.object(bot, "_send_system_message", new=AsyncMock()) as send_mock, patch(
            "obs_agent.telegram.time.time", return_value=1000.0
        ):
            ran = await bot._execute_topic_schedule(record=record, trigger_kind="interval")

        assert ran is True
        run_mock.assert_awaited_once()
        sent_texts = [str(call.kwargs.get("text", "")) for call in send_mock.await_args_list]
        assert sent_texts[0].startswith("schedule_triggered: OrderJob")
        assert "remaining=1" in sent_texts[0]
        next_local = _expected_local_schedule_time(1060.0, now_ts=1000.0, include_seconds=False)
        assert any(f"next_schedule: OrderJob at {next_local}" in text for text in sent_texts[1:])

    async def test_completion_summary_omits_schedule_triggered_line(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=7912)
        state = bot._get_state(route)
        assert state is not None
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-trigger-hidden",
                route=route,
                description="HiddenTrigger",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=60,
                prompt="run",
                schedule_mode="interval",
                next_run_at=1060.0,
            )
        )
        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            summary = bot._build_completion_summary(
                state,
                triggered_schedule_id="sched-trigger-hidden",
            )
        assert "schedule_triggered:" not in summary

    async def test_next_schedule_line_uses_second_precision_for_second_intervals(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=7913)
        state = bot._get_state(route)
        assert state is not None
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-seconds",
                route=route,
                description="SecondsJob",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=15,
                prompt="run",
                schedule_mode="interval",
                max_runs=3,
                run_count=1,
                from_ts=1005.0,
                until_ts=2000.0,
                next_run_at=1013.0,
            )
        )
        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            summary = bot._build_completion_summary(state)
        next_local = _expected_local_schedule_time(1013.0, now_ts=1000.0, include_seconds=True)
        from_local = _expected_local_schedule_time(1005.0, now_ts=1000.0, include_seconds=True)
        until_local = _expected_local_schedule_time(2000.0, now_ts=1000.0, include_seconds=True)
        assert f"next_schedule: SecondsJob at {next_local}" in summary
        assert f"from={from_local}" in summary
        assert f"until={until_local}" in summary

    async def test_reply_wake_schedule_does_not_consume_attempts_while_route_is_busy(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=79131)
        state = bot._get_state(route)
        assert state is not None
        state.busy = True
        state.last_bot = MagicMock()
        state.last_bot.send_message = AsyncMock()

        record = create_reply_wake_schedule(route)
        record.next_run_at = 0.0
        bot._register_topic_schedule(record)

        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            assert await bot._execute_topic_schedule(record=record, trigger_kind="interval") is False
            first = bot._topic_schedules_by_id[record.schedule_id]
            assert first.run_count == 0
            assert first.enabled is True
            assert first.next_run_at == 1001.0

        with patch("obs_agent.telegram.time.time", return_value=1001.0):
            assert await bot._execute_topic_schedule(record=first, trigger_kind="interval") is False
            second = bot._topic_schedules_by_id[record.schedule_id]
            assert second.run_count == 0
            assert second.enabled is True
            assert second.next_run_at == 1002.0

        with patch("obs_agent.telegram.time.time", return_value=1002.0):
            assert await bot._execute_topic_schedule(record=second, trigger_kind="interval") is False

        final = bot._topic_schedules_by_id[record.schedule_id]
        assert final.run_count == 0
        assert final.enabled is True
        assert final.next_run_at == 1003.0
        state.last_bot.send_message.assert_not_awaited()

    async def test_reply_wake_schedule_deleted_during_run_stays_deleted(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=79132)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = create_reply_wake_schedule(route)
        record.next_run_at = 0.0
        bot._register_topic_schedule(record)

        async def _run_and_delete(*args, **kwargs):
            bot._delete_topic_schedule(record.schedule_id)
            return _RunOutcome(assistant_text="ok")

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(side_effect=_run_and_delete),
        ), patch.object(bot, "_send_system_message", new=AsyncMock()), patch(
            "obs_agent.telegram.time.time", return_value=1000.0
        ):
            assert await bot._execute_topic_schedule(record=record, trigger_kind="interval") is True

        assert record.schedule_id not in bot._topic_schedules_by_id
        assert bot._state_store.load_snapshot().topic_schedules == []

    async def test_overlap_validation_removed_allows_free_coexistence(self, config):
        """Overlap validation was removed — schedules freely coexist.

        Previously tested that exhausted/inflight schedules were ignored during
        overlap validation. Now there's no validation at all, so any number
        of schedules on the same route just work.
        """
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=7914)
        state = bot._get_state(route)
        assert state is not None

        # _validate_schedule_overlap should no longer exist
        assert not hasattr(bot, "_validate_schedule_overlap"), \
            "_validate_schedule_overlap should be removed"

        # Register multiple schedules on same route — all should succeed
        for i in range(3):
            bot._register_topic_schedule(
                _TopicScheduleRecord(
                    schedule_id=f"sched-{i}",
                    route=route,
                    description=f"Schedule {i}",
                    cron_expr="*/1 * * * *",
                    trigger_kind="interval",
                    interval_seconds=60,
                    prompt=f"run {i}",
                    schedule_mode="interval",
                    from_ts=900.0,
                    until_ts=None,
                    next_run_at=1000.0,
                )
            )

        route_schedules = bot._schedule_ids_by_route.get(route, set())
        assert len(route_schedules) == 3

    async def test_interval_schedule_failure_consumes_run_count_and_notifies_user(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=89)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-fail",
            route=route,
            description="FailingJob",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run fail",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=0.0,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="x", failed=True, error="boom")),
        ), patch.object(bot, "_send_system_message", new=AsyncMock()) as notify_mock, patch(
            "obs_agent.telegram.time.time", return_value=1000.0
        ):
            await bot._execute_topic_schedule(record=record, trigger_kind="interval")
            updated = bot._topic_schedules_by_id["sched-fail"]
            assert updated.run_count == 1
            assert updated.retry_attempt_count == 0
            assert updated.next_run_at == 1060.0
            assert updated.last_error == "boom"

        assert notify_mock.await_count >= 1
        sent_texts = [str(call.kwargs.get("text", "")) for call in notify_mock.await_args_list]
        assert any("schedule failed: FailingJob: boom" in text for text in sent_texts)

    async def test_schedule_exhausts_when_either_max_runs_or_until_is_reached(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=890)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        max_runs_record = _TopicScheduleRecord(
            schedule_id="sched-max-runs",
            route=route,
            description="MaxRuns",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=0.0,
            run_count=5,
            max_runs=5,
            until_ts=2000.0,
        )
        until_record = _TopicScheduleRecord(
            schedule_id="sched-until",
            route=route,
            description="Until",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=0.0,
            run_count=0,
            max_runs=5,
            until_ts=900.0,
        )
        bot._register_topic_schedule(max_runs_record)
        bot._register_topic_schedule(until_record)

        with patch.object(bot, "_run_and_send", new=AsyncMock()) as run_mock, patch(
            "obs_agent.telegram.time.time", return_value=1000.0
        ):
            ran_max = await bot._execute_topic_schedule(record=max_runs_record, trigger_kind="interval")
            ran_until = await bot._execute_topic_schedule(record=until_record, trigger_kind="interval")

        assert ran_max is False
        assert ran_until is False
        run_mock.assert_not_awaited()
        assert bot._topic_schedules_by_id["sched-max-runs"].enabled is False
        assert bot._topic_schedules_by_id["sched-until"].enabled is False

    async def test_stop_event_triggers_on_stop_schedule_once(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=80)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-stop",
            route=route,
            description="OnStop",
            cron_expr="*/1 * * * *",
            trigger_kind="on_topic_stop",
            interval_seconds=None,
            prompt="run on stop",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=None,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock:
            bot._schedule_stop_events.put_nowait(
                (
                    route,
                    {
                        "session_id": None,
                        "schedule_run_active": False,
                    },
                )
            )
            await bot._process_stop_schedule_events()

        run_mock.assert_awaited_once()
        assert bot._topic_schedules_by_id["sched-stop"].run_count == 1

    async def test_stop_event_skips_schedule_origin_to_prevent_loop(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=81)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-stop-2",
            route=route,
            description="OnStop",
            cron_expr="*/1 * * * *",
            trigger_kind="on_topic_stop",
            interval_seconds=None,
            prompt="run on stop",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=None,
        )
        bot._register_topic_schedule(record)

        with patch.object(bot, "_run_and_send", new=AsyncMock()) as run_mock:
            bot._schedule_stop_events.put_nowait(
                (
                    route,
                    {
                        "session_id": None,
                        "schedule_run_active": True,
                    },
                )
            )
            await bot._process_stop_schedule_events()

        run_mock.assert_not_awaited()

    async def test_stop_event_defers_while_execution_active_then_runs(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=8100)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-stop-defer",
            route=route,
            description="OnStop",
            cron_expr="*/1 * * * *",
            trigger_kind="on_topic_stop",
            interval_seconds=None,
            prompt="run on stop",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=None,
        )
        bot._register_topic_schedule(record)

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock:
            bot._schedule_stop_events.put_nowait(
                (
                    route,
                    {
                        "session_id": None,
                        "schedule_run_active": False,
                        "execution_active": True,
                    },
                )
            )
            await bot._process_stop_schedule_events()
            run_mock.assert_not_awaited()
            assert not bot._schedule_stop_events.empty()

            await bot._process_stop_schedule_events()

        run_mock.assert_awaited_once()
        assert bot._topic_schedules_by_id["sched-stop-defer"].run_count == 1
        assert bot._schedule_stop_events.empty()

    async def test_stop_event_suppressed_by_recent_schedule_window(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=810)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-stop-window",
            route=route,
            description="OnStop",
            cron_expr="*/1 * * * *",
            trigger_kind="on_topic_stop",
            interval_seconds=None,
            prompt="run on stop",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=None,
        )
        bot._register_topic_schedule(record)
        bot._schedule_stop_suppress_until[route] = 2000.0

        with patch.object(bot, "_run_and_send", new=AsyncMock()) as run_mock, patch(
            "obs_agent.telegram.time.time", return_value=1999.0
        ):
            bot._schedule_stop_events.put_nowait(
                (
                    route,
                    {
                        "session_id": None,
                        "schedule_run_active": False,
                    },
                )
            )
            await bot._process_stop_schedule_events()

        run_mock.assert_not_awaited()

    async def test_stop_event_reanchors_interval_schedule_next_run(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=811)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-reanchor",
            route=route,
            description="Reanchor",
            schedule_mode="interval",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run interval",
            reset_session=False,
            next_run_at=9999.0,
        )
        bot._register_topic_schedule(record)

        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            bot._schedule_stop_events.put_nowait(
                (
                    route,
                    {
                        "session_id": None,
                        "schedule_run_active": False,
                        "execution_active": False,
                    },
                )
            )
            await bot._process_stop_schedule_events()

        assert bot._topic_schedules_by_id["sched-reanchor"].next_run_at == 1060.0

    async def test_reanchor_interval_schedule_updates_completion_eta(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=8111)
        state = bot._get_state(route)
        assert state is not None

        record = _TopicScheduleRecord(
            schedule_id="sched-summary-reanchor",
            route=route,
            description="Summary",
            schedule_mode="interval",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run interval",
            reset_session=False,
            next_run_at=1010.0,
        )
        bot._register_topic_schedule(record)

        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            bot._reanchor_interval_schedules_for_route(route=route, base_ts=1000.0)
            summary = bot._build_completion_summary(state)

        assert bot._topic_schedules_by_id["sched-summary-reanchor"].next_run_at == 1060.0
        next_local = _expected_local_schedule_time(1060.0, now_ts=1000.0, include_seconds=False)
        assert f"next_schedule: Summary at {next_local}" in summary

    async def test_inherit_mode_fork_only_copies_to_fork_children(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=67890, thread_id=812)
        non_fork_child = TelegramRoute(chat_id=67890, thread_id=813)
        fork_child = TelegramRoute(chat_id=67890, thread_id=814)
        assert bot._get_state(parent_route) is not None
        assert bot._get_state(non_fork_child) is not None
        assert bot._get_state(fork_child) is not None

        parent_record = _TopicScheduleRecord(
            schedule_id="sched-parent",
            route=parent_route,
            description="Parent",
            schedule_mode="interval",
            cron_expr="*/2 * * * *",
            trigger_kind="interval",
            interval_seconds=120,
            prompt="run parent",
            reset_session=False,
            inherit_mode="fork",
            next_run_at=1000.0,
        )
        bot._register_topic_schedule(parent_record)

        bot._inherit_topic_schedules(
            parent_route=parent_route,
            child_route=non_fork_child,
            is_fork=False,
        )
        assert bot._schedule_ids_by_route.get(non_fork_child, set()) == set()

        bot._inherit_topic_schedules(
            parent_route=parent_route,
            child_route=fork_child,
            is_fork=True,
        )
        inherited_ids = bot._schedule_ids_by_route.get(fork_child, set())
        assert len(inherited_ids) == 1
        inherited = bot._topic_schedules_by_id[next(iter(inherited_ids))]
        assert inherited.inherit_mode == "fork"
        assert inherited.interval_seconds == 120
        assert inherited.run_count == 0

    async def test_completion_summary_reports_only_one_next_schedule(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=815)
        state = bot._get_state(route)
        assert state is not None
        state.session_manager.set_session_id("sid-summary")

        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-first",
                route=route,
                description="First",
                schedule_mode="interval",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=60,
                prompt="first",
                reset_session=False,
                next_run_at=1060.0,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-second",
                route=route,
                description="Second",
                schedule_mode="interval",
                cron_expr="*/2 * * * *",
                trigger_kind="interval",
                interval_seconds=120,
                prompt="second",
                reset_session=False,
                next_run_at=1120.0,
                from_ts=1200.0,
                until_ts=1300.0,
            )
        )

        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            summary = bot._build_completion_summary(state)
        assert summary.count("next_schedule:") == 1
        next_local = _expected_local_schedule_time(1060.0, now_ts=1000.0, include_seconds=False)
        assert f"next_schedule: First at {next_local}" in summary

    async def test_completion_summary_includes_next_schedule_line(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=82)
        state = bot._get_state(route)
        assert state is not None
        state.session_manager.set_session_id("sid-1")

        record = _TopicScheduleRecord(
            schedule_id="sched-next",
            route=route,
            description="Maintenance",
            cron_expr="*/5 * * * *",
            trigger_kind="interval",
            interval_seconds=300,
            prompt="run",
            schedule_mode="interval",
            reset_session=False,
            run_count=2,
            max_runs=5,
            next_run_at=1300.0,
        )
        bot._register_topic_schedule(record)
        with patch("obs_agent.telegram.time.time", return_value=1000.0):
            summary = bot._build_completion_summary(state)

        assert summary.startswith("context: ")
        next_local = _expected_local_schedule_time(1300.0, now_ts=1000.0, include_seconds=False)
        assert f"next_schedule: Maintenance at {next_local}" in summary
        assert "remaining=3" in summary

    async def test_due_schedule_defers_while_busy_and_runs_once_after_unlock(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=83)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        record = _TopicScheduleRecord(
            schedule_id="sched-busy",
            route=route,
            description="Busy",
            cron_expr="*/1 * * * *",
            trigger_kind="interval",
            interval_seconds=60,
            prompt="run",
            schedule_mode="interval",
            reset_session=False,
            next_run_at=0.0,
        )
        bot._register_topic_schedule(record)
        state.busy = True
        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock, patch("obs_agent.telegram.time.time", return_value=1000.0):
            await bot._run_due_interval_schedules()
            run_mock.assert_not_awaited()
            state.busy = False
            await bot._run_due_interval_schedules()
            run_mock.assert_awaited_once()

    async def test_multi_topic_due_schedules_remain_isolated(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=67890, thread_id=84)
        route_b = TelegramRoute(chat_id=67890, thread_id=85)
        state_a = bot._get_state(route_a)
        state_b = bot._get_state(route_b)
        assert state_a is not None
        assert state_b is not None
        fake_bot_a = MagicMock()
        fake_bot_b = MagicMock()
        fake_bot_a.send_message = AsyncMock()
        fake_bot_b.send_message = AsyncMock()
        state_a.last_bot = fake_bot_a
        state_b.last_bot = fake_bot_b

        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-a",
                route=route_a,
                description="A",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=60,
                prompt="run A",
                schedule_mode="interval",
                reset_session=False,
                next_run_at=0.0,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-b",
                route=route_b,
                description="B",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=60,
                prompt="run B",
                schedule_mode="interval",
                reset_session=False,
                next_run_at=0.0,
            )
        )

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock, patch("obs_agent.telegram.time.time", return_value=2000.0):
            await bot._run_due_interval_schedules()

        assert run_mock.await_count == 2
        called_routes = {call.kwargs["state"].route for call in run_mock.await_args_list}
        assert called_routes == {route_a, route_b}

    async def test_on_stop_and_interval_in_same_route_do_not_duplicate(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=86)
        state = bot._get_state(route)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state.last_bot = fake_bot

        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-i",
                route=route,
                description="Interval",
                cron_expr="*/1 * * * *",
                trigger_kind="interval",
                interval_seconds=60,
                prompt="interval",
                schedule_mode="interval",
                reset_session=False,
                next_run_at=0.0,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-s",
                route=route,
                description="Stop",
                cron_expr="*/1 * * * *",
                trigger_kind="on_topic_stop",
                interval_seconds=None,
                prompt="stop",
                schedule_mode="interval",
                reset_session=False,
                next_run_at=None,
            )
        )

        with patch.object(
            bot,
            "_run_and_send",
            new=AsyncMock(return_value=_RunOutcome(assistant_text="ok")),
        ) as run_mock:
            with patch("obs_agent.telegram.time.time", return_value=3000.0):
                await bot._run_due_interval_schedules()
            with patch("obs_agent.telegram.time.time", return_value=3005.0):
                bot._schedule_stop_events.put_nowait(
                    (route, {"session_id": None, "schedule_run_active": False})
                )
                await bot._process_stop_schedule_events()

        assert run_mock.await_count == 2
        prompts = [call.kwargs["user_text"] for call in run_mock.await_args_list]
        assert any("interval" in prompt for prompt in prompts)
        assert any("stop" in prompt for prompt in prompts)


class TestForkViaReply:
    async def test_reply_to_old_assistant_message_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        bot._message_map[(67890, 5)] = _TelegramMessageBinding(
            jsonl_uuid="assistant-1",
            session_id="sid-root",
            role="assistant",
            route=state.route,
        )
        bot._session_heads["sid-root"] = "assistant-latest"

        trigger = QueuedMessage(
            text="follow up",
            telegram_message_id=11,
            reply_to_message_id=5,
        )
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork") as mock_fork:
            proceed, reply_to_user_message_id = await bot._resolve_session_for_trigger(
                state=state,
                trigger_message=trigger,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert state.session_id == "sid-fork"
        mock_fork.assert_called_once()

    async def test_reply_to_old_user_message_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        bot._message_map[(67890, 5)] = _TelegramMessageBinding(
            jsonl_uuid="user-1",
            session_id="sid-root",
            role="user",
            route=state.route,
        )
        bot._session_heads["sid-root"] = "assistant-latest"

        trigger = QueuedMessage(
            text="follow up",
            telegram_message_id=11,
            reply_to_message_id=5,
        )
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork") as mock_fork:
            proceed, reply_to_user_message_id = await bot._resolve_session_for_trigger(
                state=state,
                trigger_message=trigger,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert state.session_id == "sid-fork"
        mock_fork.assert_called_once()

    async def test_reply_to_mapped_system_marker_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        bot._message_map[(67890, 7)] = _TelegramMessageBinding(
            jsonl_uuid="assistant-1",
            session_id="sid-root",
            role="assistant",
            route=state.route,
        )
        bot._session_heads["sid-root"] = "assistant-latest"

        trigger = QueuedMessage(
            text="follow up",
            telegram_message_id=11,
            reply_to_message_id=7,
        )
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork") as mock_fork:
            proceed, reply_to_user_message_id = await bot._resolve_session_for_trigger(
                state=state,
                trigger_message=trigger,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert state.session_id == "sid-fork"
        mock_fork.assert_called_once()

    async def test_reply_to_unmapped_message_returns_error(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        trigger = QueuedMessage(
            text="follow up",
            telegram_message_id=11,
            reply_to_message_id=999,
        )
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        state = _state(bot)

        proceed, reply_to_user_message_id = await bot._resolve_session_for_trigger(
            state=state,
            trigger_message=trigger,
            bot=fake_bot,
        )

        assert proceed is False
        assert reply_to_user_message_id == 11
        assert fake_bot.send_message.call_args.kwargs["text"] == "<u><i>can&#x27;t fork from this message</i></u>"

    async def test_pre_sent_receipts_are_not_duplicated_when_processing_media(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("", message_id=61)
        update.effective_message.text = None
        ctx = _make_context()

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._process_message(
                "normalized attachment",
                update,
                ctx,
                pre_sent_status_message_ids=[777],
            )

        ctx.bot.send_message.assert_not_called()
        assert mock_run.await_count == 1
        assert mock_run.call_args.kwargs["trigger_status_message_ids"] == [777]

    async def test_poller_skips_while_busy(self, config):
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            background_poll_seconds=0.01,
            enable_background_poller=True,
        )
        fake_ptb_bot = MagicMock()
        fake_ptb_bot.send_message = AsyncMock()

        state = _state(bot)
        state.last_bot = fake_ptb_bot
        state.busy = True
        state.hook_state.message_queue.put_nowait("queued bg result")

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            await asyncio.sleep(0.05)
            await bot.shutdown()

            mock_run.assert_not_called()
            assert not state.hook_state.message_queue.empty()


class TestCommands:
    async def test_clear_resets_route_state_and_keeps_identity(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=321)
        state = bot._get_state(route, topic_title="Root Worker")
        assert state is not None
        state.pending_messages = [QueuedMessage(text="x")]
        state.hook_state.interrupt_flag = True
        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
        )
        team_name = root_team_key_for_lineage(("Root", "Worker"))
        agent_name = agent_name_for_lineage(("Root", "Worker"))

        update = _make_update("/clear", thread_id=321)
        ctx = _make_context()

        with patch.object(state.session_manager, "async_reset", new_callable=AsyncMock) as mock_reset:
            await bot.handle_clear(update, ctx)

        mock_reset.assert_called_once()
        assert state.pending_messages == []
        assert state.hook_state.interrupt_flag is False
        assert state.agent_lineage == ("Root", "Worker")
        assert bot._resolve_route_inbox_target(team_name=team_name, agent_name=agent_name) is state
        ctx.bot.send_message.assert_called_once()
        assert (
            ctx.bot.send_message.call_args.kwargs["text"]
            == "<u><i>session cleared; agent identity was kept</i></u>"
        )

    async def test_clear_mentions_unschedule_when_topic_has_schedule(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=1)
        state = bot._get_state(route)
        assert state is not None
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-clear-note",
                route=route,
                description="Kept",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=300,
                prompt="run",
                reset_session=False,
                next_run_at=5000.0,
            )
        )

        update = _make_update("/clear", thread_id=1)
        ctx = _make_context()
        await bot.handle_clear(update, ctx)
        assert (
            ctx.bot.send_message.call_args.kwargs["text"]
            == "<u><i>session cleared; schedule was kept; agent identity was kept. Use /unschedule to remove this topic schedule.</i></u>"
        )

    async def test_new_reseeds_route_as_new_trunk_identity(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=321)
        state = bot._get_state(route, topic_title="Old Topic")
        assert state is not None
        state.topic_icon_custom_emoji_id = "emoji-old"
        bot._set_topic_metadata(route=route, title="Old Topic", icon_custom_emoji_id="emoji-old")
        bot._prime_obs_bootstrap(
            state,
            lineage=("Old Topic",),
            origin="trunk_start",
            is_fork=False,
        )
        old_team_name = root_team_key_for_lineage(("Old Topic",))
        old_agent_name = agent_name_for_lineage(("Old Topic",))

        update = _make_update("/new ⚡ Fresh Start", thread_id=321)
        ctx = _make_context()
        ctx.args = ["⚡", "Fresh", "Start"]
        ctx.bot.edit_forum_topic = AsyncMock(return_value=True)

        with (
            patch.object(state.session_manager, "async_reset", new_callable=AsyncMock) as mock_reset,
            patch.object(
                bot,
                "_resolve_new_topic_visibility",
                AsyncMock(return_value=("Fresh Start", "⚡", "emoji-new")),
            ),
        ):
            await bot.handle_new(update, ctx)

        mock_reset.assert_called_once()
        ctx.bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            message_thread_id=321,
            name="Fresh Start",
            icon_custom_emoji_id="emoji-new",
        )
        assert state.agent_lineage == ("Fresh Start",)
        assert state.pending_obs_bootstrap is not None
        assert "Fresh Start" in state.pending_obs_bootstrap
        assert bot._resolve_route_inbox_target(
            team_name=old_team_name,
            agent_name=old_agent_name,
        ) is None
        new_team_name = root_team_key_for_lineage(("Fresh Start",))
        assert bot._resolve_route_inbox_target(
            team_name=new_team_name,
            agent_name=new_team_name,
        ) is state
        assert (
            ctx.bot.send_message.call_args.kwargs["text"]
            == "<u><i>new trunk session created: ⚡ Fresh Start</i></u>"
        )

    async def test_new_visibility_uses_requested_valid_emoji_and_name(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=321)
        state = bot._get_state(route, topic_title="Old Topic")
        assert state is not None

        fake_bot = MagicMock()
        fake_bot.get_forum_topic_icon_stickers = AsyncMock(
            return_value=[
                SimpleNamespace(emoji="⚡", custom_emoji_id="emoji-requested"),
                SimpleNamespace(emoji="🔥", custom_emoji_id="emoji-other"),
            ]
        )

        title, emoji, icon = await bot._resolve_new_topic_visibility(
            state=state,
            bot=fake_bot,
            raw_args="⚡ Fresh Start",
        )

        assert title == "Fresh Start"
        assert emoji == "⚡"
        assert icon == "emoji-requested"

    async def test_tree_renders_nested_display_names_and_topic_links(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=321)
        state = bot._get_state(route, topic_title="Branch")
        assert state is not None
        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Branch"),
            origin="agent_task_fresh",
            is_fork=False,
        )
        tree_context = bot._current_tree_context(state)
        assert tree_context is not None
        team_name, current_agent_name, current_lineage = tree_context
        root_agent_name = agent_name_for_lineage(("Root",), team_key=team_name)
        child_agent_name = agent_name_for_lineage(("Root", "Branch", "Child"), team_key=team_name)
        grandchild_agent_name = agent_name_for_lineage(("Root", "Branch", "Child", "Grandchild"), team_key=team_name)
        sibling_agent_name = agent_name_for_lineage(("Root", "Sibling"), team_key=team_name)
        team_dir = tmp_path / ".claude" / "teams" / team_name
        (team_dir / "inboxes").mkdir(parents=True, exist_ok=True)
        for agent_name in (
            root_agent_name,
            current_agent_name,
            child_agent_name,
            grandchild_agent_name,
            sibling_agent_name,
        ):
            (team_dir / "inboxes" / f"{agent_name}.json").write_text("[]", encoding="utf-8")
        (team_dir / "config.json").write_text(
            json.dumps(
                {
                    "members": [
                        {
                            "name": root_agent_name,
                            "obs": {
                                "display_name": "Root",
                                "lineage": ["Root"],
                                "lineage_length": 1,
                                "topic_chat_id": -10067890,
                                "topic_thread_id": 111,
                            },
                        },
                        {
                            "name": current_agent_name,
                            "obs": {
                                "display_name": "Branch",
                                "lineage": ["Root", "Branch"],
                                "lineage_length": 2,
                                "parent_agent_name": root_agent_name,
                                "parent_display_name": "Root",
                                "topic_chat_id": -10067890,
                                "topic_thread_id": 321,
                            },
                        },
                        {
                            "name": child_agent_name,
                            "obs": {
                                "display_name": "Child",
                                "lineage": ["Root", "Branch", "Child"],
                                "lineage_length": 3,
                                "parent_agent_name": current_agent_name,
                                "parent_display_name": "Branch",
                                "topic_chat_id": -10067890,
                                "topic_thread_id": 333,
                            },
                        },
                        {
                            "name": grandchild_agent_name,
                            "obs": {
                                "display_name": "Grandchild",
                                "lineage": ["Root", "Branch", "Child", "Grandchild"],
                                "lineage_length": 4,
                                "parent_agent_name": child_agent_name,
                                "parent_display_name": "Child",
                                "topic_chat_id": -10067890,
                                "topic_thread_id": 444,
                            },
                        },
                        {
                            "name": sibling_agent_name,
                            "obs": {
                                "display_name": "Sibling",
                                "lineage": ["Root", "Sibling"],
                                "lineage_length": 2,
                                "parent_agent_name": root_agent_name,
                                "parent_display_name": "Root",
                                "topic_chat_id": -10067890,
                                "topic_thread_id": 555,
                            },
                        },
                    ]
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        members = bot._load_tree_members(
            team_name=team_name,
            current_agent_name=current_agent_name,
            current_lineage=current_lineage,
            current_route=route,
        )
        html_text = bot._render_tree_html(
            team_name=team_name,
            current_agent_name=current_agent_name,
            current_lineage=current_lineage,
            members=members,
            mode="tree",
        )
        root_display = format_root_display_name(team_name, "Root")
        assert html_text.startswith(
            f'<b><a href="https://t.me/c/67890/111">{root_display}</a></b>\n\n'
        )
        assert "- Branch (current)" in html_text
        assert "\u00A0\u00A0\u00A0\u00A0- <a href=\"https://t.me/c/67890/333\">Child</a>" in html_text
        assert "https://t.me/c/67890/444" in html_text
        assert "- <a href=\"https://t.me/c/67890/555\">Sibling</a>" in html_text
        await bot.shutdown()

    async def test_tree_ancestors_and_descendants_filters(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=321)
        state = bot._get_state(route, topic_title="Branch")
        assert state is not None
        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Branch"),
            origin="agent_task_fresh",
            is_fork=False,
        )
        team_name, current_agent_name, current_lineage = bot._current_tree_context(state)
        root_agent_name = agent_name_for_lineage(("Root",), team_key=team_name)
        child_agent_name = agent_name_for_lineage(("Root", "Branch", "Child"), team_key=team_name)
        grandchild_agent_name = agent_name_for_lineage(("Root", "Branch", "Child", "Grandchild"), team_key=team_name)
        sibling_agent_name = agent_name_for_lineage(("Root", "Sibling"), team_key=team_name)
        team_dir = tmp_path / ".claude" / "teams" / team_name
        (team_dir / "inboxes").mkdir(parents=True, exist_ok=True)
        for agent_name in (
            root_agent_name,
            current_agent_name,
            child_agent_name,
            grandchild_agent_name,
            sibling_agent_name,
        ):
            (team_dir / "inboxes" / f"{agent_name}.json").write_text("[]", encoding="utf-8")
        (team_dir / "config.json").write_text(
            json.dumps(
                {
                    "members": [
                        {"name": root_agent_name, "obs": {"display_name": "Root", "lineage": ["Root"], "lineage_length": 1}},
                        {"name": current_agent_name, "obs": {"display_name": "Branch", "lineage": ["Root", "Branch"], "lineage_length": 2, "parent_agent_name": root_agent_name, "parent_display_name": "Root"}},
                        {"name": child_agent_name, "obs": {"display_name": "Child", "lineage": ["Root", "Branch", "Child"], "lineage_length": 3, "parent_agent_name": current_agent_name, "parent_display_name": "Branch"}},
                        {"name": grandchild_agent_name, "obs": {"display_name": "Grandchild", "lineage": ["Root", "Branch", "Child", "Grandchild"], "lineage_length": 4, "parent_agent_name": child_agent_name, "parent_display_name": "Child"}},
                        {"name": sibling_agent_name, "obs": {"display_name": "Sibling", "lineage": ["Root", "Sibling"], "lineage_length": 2, "parent_agent_name": root_agent_name, "parent_display_name": "Root"}},
                    ]
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        members = bot._load_tree_members(
            team_name=team_name,
            current_agent_name=current_agent_name,
            current_lineage=current_lineage,
            current_route=route,
        )
        ancestors_html = bot._render_tree_html(
            team_name=team_name,
            current_agent_name=current_agent_name,
            current_lineage=current_lineage,
            members=members,
            mode="ancestors",
        )
        assert format_root_display_name(team_name, "Root") in ancestors_html
        assert "<b>Branch</b> (current)" in ancestors_html
        assert "Child" not in ancestors_html

        descendants_html = bot._render_tree_html(
            team_name=team_name,
            current_agent_name=current_agent_name,
            current_lineage=current_lineage,
            members=members,
            mode="descendants",
        )
        assert descendants_html.startswith("<b>Branch</b> (current)\n\n")
        assert "Child" in descendants_html
        assert "Grandchild" in descendants_html
        assert "Sibling" not in descendants_html
        await bot.shutdown()

    async def test_tree_alias_command_maps_hyphen_variants(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/tree-ancestors", chat_id=67890, thread_id=321)
        ctx = _make_context()
        with patch.object(bot, "_handle_tree_view", new_callable=AsyncMock) as mock_tree:
            await bot.handle_tree_alias_command(update, ctx)
        assert mock_tree.await_args.kwargs["mode"] == "ancestors"

        update.effective_message.text = "/tree-"
        with patch.object(bot, "_handle_tree_view", new_callable=AsyncMock) as mock_tree:
            await bot.handle_tree_alias_command(update, ctx)
        assert mock_tree.await_args.kwargs["mode"] == "descendants"
        await bot.shutdown()

    async def test_new_visibility_generates_random_title_and_non_current_emoji(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=321)
        state = bot._get_state(route, topic_title="Old Topic")
        assert state is not None
        state.topic_icon_custom_emoji_id = "emoji-old"

        fake_bot = MagicMock()
        fake_bot.get_forum_topic_icon_stickers = AsyncMock(
            return_value=[
                SimpleNamespace(emoji="⚡", custom_emoji_id="emoji-old"),
                SimpleNamespace(emoji="🔥", custom_emoji_id="emoji-new"),
            ]
        )

        with patch.object(bot, "_random_topic_title", return_value="Fresh Orbit"):
            title, emoji, icon = await bot._resolve_new_topic_visibility(
                state=state,
                bot=fake_bot,
                raw_args=None,
            )

        assert title == "Fresh Orbit"
        assert emoji == "🔥"
        assert icon == "emoji-new"

    async def test_deleted_route_becomes_undeliverable_until_same_lineage_is_respawned(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=654)
        child_state = bot._get_state(child_route, topic_title="Root - Worker")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-worker")
        bot._bind_state_session(child_state)
        bot._prime_obs_bootstrap(
            child_state,
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id="sid-worker",
        )
        team_name = root_team_key_for_lineage(("Root", "Worker"))
        agent_name = agent_name_for_lineage(("Root", "Worker"))

        assert bot._recipient_delivery_status(team_name=team_name, agent_name=agent_name)["deliverable"] is True

        await bot._drop_route_state(child_route, terminal_status="failed")

        after_delete = bot._recipient_delivery_status(team_name=team_name, agent_name=agent_name)
        assert after_delete["deliverable"] is False

        reborn_state = bot._get_state(child_route, topic_title="Root - Worker")
        assert reborn_state is not None
        reborn_state.session_manager.set_session_id("sid-worker-new")
        bot._bind_state_session(reborn_state)
        bot._prime_obs_bootstrap(
            reborn_state,
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id="sid-worker-new",
        )

        assert bot._recipient_delivery_status(team_name=team_name, agent_name=agent_name)["deliverable"] is True
        await bot.shutdown()

    async def test_launch_fresh_child_reuses_unbound_inbox_projection_after_delete(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=55)
        parent_state = bot._get_state(parent_route, topic_title="Root")
        assert parent_state is not None
        parent_state.last_bot = MagicMock()
        parent_state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=333))
        parent_state.last_bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=920), MagicMock(message_id=921), MagicMock(message_id=922)]
        )
        parent_state.session_manager.set_session_id("sid-root")
        bot._bind_state_session(parent_state)
        bot._prime_obs_bootstrap(
            parent_state,
            lineage=("Root",),
            origin="user_thread",
            is_fork=False,
            session_id="sid-root",
        )

        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Root - Worker")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-worker")
        bot._bind_state_session(child_state)
        bot._prime_obs_bootstrap(
            child_state,
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id="sid-worker",
        )
        team_name = root_team_key_for_lineage(("Root", "Worker"))
        agent_name = agent_name_for_lineage(("Root", "Worker"))
        inbox_path = bot._team_inbox_path(team_name, agent_name)
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text("[]", encoding="utf-8")

        await bot._drop_route_state(child_route, terminal_status="failed")
        assert bot._recipient_delivery_status(team_name=team_name, agent_name=agent_name)["deliverable"] is False

        fake_task_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        with patch("obs_agent.telegram.uuid.uuid4", side_effect=[fake_task_id]), patch.object(
            bot,
            "_execute_fork_task",
            new_callable=AsyncMock,
        ):
            launched = await bot._launch_fork_task(
                route=parent_route,
                args={
                    "prompt": "Return READY",
                    "display_name": "Worker",
                    "fork": False,
                    "team_name": team_name,
                },
            )

        assert "AgentTask launched successfully." in launched["content"][0]["text"]
        record = bot._fork_tasks_by_id[str(fake_task_id)]
        assert record.team_name == team_name
        assert record.agent_name == agent_name
        await bot.shutdown()

    async def test_drop_route_state_sweeps_stale_team_worker_binding_when_cancel_path_misses_it(
        self,
        config,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=654)
        state = bot._get_state(route, topic_title="Root - Worker")
        assert state is not None
        state.session_manager.set_session_id("sid-worker")
        bot._bind_state_session(state)
        bot._prime_obs_bootstrap(
            state,
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id="sid-worker",
        )

        team_name = root_team_key_for_lineage(("Root", "Worker"))
        agent_name = agent_name_for_lineage(("Root", "Worker"))
        record = _ForkTaskRecord(
            task_id="task-worker-1",
            parent_route=TelegramRoute(chat_id=67890, thread_id=321),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="uuid-parent",
            child_route=route,
            child_session_id="sid-worker",
            prompt="",
            description="Worker",
            status="launched",
            is_fork=False,
            launch_tool_name="AgentTask",
            team_name=team_name,
            agent_name=agent_name,
        )
        bot._fork_tasks_by_id[record.task_id] = record
        bot._fork_task_by_child_route[route] = record.task_id
        bot._register_team_worker_record(record)

        assert bot._resolve_team_worker_record(team_name=team_name, agent_name=agent_name) is not None
        assert bot._state_store.load_snapshot().team_worker_states

        with patch.object(bot, "_cancel_route_fork_tasks", new=AsyncMock()) as cancel_mock:
            await bot._drop_route_state(route, terminal_status="failed")

        cancel_mock.assert_awaited_once_with(route, status="failed")
        assert bot._resolve_team_worker_record(team_name=team_name, agent_name=agent_name) is None
        assert bot._state_store.load_snapshot().team_worker_states == []
        assert bot._fork_task_by_child_route.get(route) is None
        assert bot._fork_tasks_by_id[record.task_id].terminal_request == "failed"
        await bot.shutdown()

    async def test_unschedule_no_args_removes_next_upcoming_only(self, config):
        """'/unschedule' without args removes only the next-upcoming schedule."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=1)
        assert bot._get_state(route) is not None

        # Register two schedules with different next_run_at
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-soon",
                route=route,
                description="Soon",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=10,
                prompt="run soon",
                reset_session=False,
                next_run_at=5000.0,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-later",
                route=route,
                description="Later",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=300,
                prompt="run later",
                reset_session=False,
                next_run_at=9000.0,
            )
        )
        assert len(bot._schedule_ids_by_route.get(route, set())) == 2

        update = _make_update("/unschedule", thread_id=1)
        ctx = _make_context()
        await bot.handle_unschedule(update, ctx)

        remaining = bot._schedule_ids_by_route.get(route, set())
        assert "sched-soon" not in remaining, "Soonest schedule should be removed"
        assert "sched-later" in remaining, "Later schedule should remain"
        msg = ctx.bot.send_message.call_args.kwargs["text"]
        assert "unscheduled" in msg.lower() or "sched-soon" in msg

    async def test_unschedule_single_schedule_removes_it(self, config):
        """'/unschedule' with only one schedule removes it (backward compatible)."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=1)
        assert bot._get_state(route) is not None
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-only",
                route=route,
                description="Only one",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=300,
                prompt="run",
                reset_session=False,
                next_run_at=5000.0,
            )
        )
        update = _make_update("/unschedule", thread_id=1)
        ctx = _make_context()
        await bot.handle_unschedule(update, ctx)
        assert bot._schedule_ids_by_route.get(route, set()) == set()
        msg = ctx.bot.send_message.call_args.kwargs["text"]
        assert "unscheduled" in msg.lower() or "sched-only" in msg

    async def test_unschedule_all_removes_chat_schedules(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=67890, thread_id=1)
        route_b = TelegramRoute(chat_id=67890, thread_id=2)
        assert bot._get_state(route_a) is not None
        assert bot._get_state(route_b) is not None
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-a",
                route=route_a,
                description="A",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=300,
                prompt="run",
                reset_session=False,
                next_run_at=5000.0,
            )
        )
        bot._register_topic_schedule(
            _TopicScheduleRecord(
                schedule_id="sched-b",
                route=route_b,
                description="B",
                schedule_mode="interval",
                cron_expr="*/5 * * * *",
                trigger_kind="interval",
                interval_seconds=300,
                prompt="run",
                reset_session=False,
                next_run_at=5000.0,
            )
        )

        update = _make_update("/unschedule all", thread_id=1)
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_unschedule(update, ctx)
        assert bot._schedule_ids_by_route.get(route_a, set()) == set()
        assert bot._schedule_ids_by_route.get(route_b, set()) == set()
        assert "unscheduled 2 schedule(s) across this chat" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_stop_sets_interrupt_flag_and_calls_sdk_interrupt(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        fake_client = MagicMock()
        fake_client.interrupt = AsyncMock()
        state.session_manager._client = fake_client
        state.session_manager._connected = True

        update = _make_update("/stop")
        ctx = _make_context()
        await bot.handle_stop(update, ctx)

        assert state.hook_state.interrupt_flag is True
        assert state.hook_state.interrupt_requested is False
        assert state.hook_state.interrupt_notice_pending is True
        assert state.hook_state.pause_queue_delivery is False
        fake_client.interrupt.assert_awaited_once()
        ctx.bot.send_message.assert_called_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent</i></u>"

    async def test_stop_all_sets_interrupt_flag_for_all_routes(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        general = _state(bot)
        topic = _state(bot, thread_id=321)
        general_client = MagicMock()
        general_client.interrupt = AsyncMock()
        topic_client = MagicMock()
        topic_client.interrupt = AsyncMock()
        general.session_manager._client = general_client
        general.session_manager._connected = True
        topic.session_manager._client = topic_client
        topic.session_manager._connected = True

        update = _make_update("/stop all")
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_stop(update, ctx)

        assert general.hook_state.interrupt_flag is True
        assert topic.hook_state.interrupt_flag is True
        assert general.hook_state.interrupt_notice_pending is True
        assert topic.hook_state.interrupt_notice_pending is True
        assert general.hook_state.pause_queue_delivery is False
        assert topic.hook_state.pause_queue_delivery is False
        general_client.interrupt.assert_awaited_once()
        topic_client.interrupt.assert_awaited_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent to all topics</i></u>"

    async def test_stop_all_with_mention_suffix_targets_all_routes(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        general = _state(bot)
        topic = _state(bot, thread_id=321)
        general_client = MagicMock()
        general_client.interrupt = AsyncMock()
        topic_client = MagicMock()
        topic_client.interrupt = AsyncMock()
        general.session_manager._client = general_client
        general.session_manager._connected = True
        topic.session_manager._client = topic_client
        topic.session_manager._connected = True

        update = _make_update("/stop all@obs_bot")
        ctx = _make_context()
        ctx.args = ["all@obs_bot"]
        await bot.handle_stop(update, ctx)

        assert general.hook_state.interrupt_flag is True
        assert topic.hook_state.interrupt_flag is True
        general_client.interrupt.assert_awaited_once()
        topic_client.interrupt.assert_awaited_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent to all topics</i></u>"

    async def test_stop_with_command_mention_targets_all_routes(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        general = _state(bot)
        topic = _state(bot, thread_id=321)
        general_client = MagicMock()
        general_client.interrupt = AsyncMock()
        topic_client = MagicMock()
        topic_client.interrupt = AsyncMock()
        general.session_manager._client = general_client
        general.session_manager._connected = True
        topic.session_manager._client = topic_client
        topic.session_manager._connected = True

        update = _make_update("/stop@obs_bot all")
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_stop(update, ctx)

        assert general.hook_state.interrupt_flag is True
        assert topic.hook_state.interrupt_flag is True
        general_client.interrupt.assert_awaited_once()
        topic_client.interrupt.assert_awaited_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent to all topics</i></u>"

    async def test_stop_does_not_mark_parent_agent_task_terminal_request(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=67890, thread_id=None)
        child_route = TelegramRoute(chat_id=67890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route)
        assert parent_state is not None
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-child")

        record = _ForkTaskRecord(
            task_id="task-agent",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            is_fork=False,
            launch_tool_name="AgentTask",
        )
        bot._fork_tasks_by_id["task-agent"] = record

        update = _make_update("/stop", thread_id=None)
        ctx = _make_context()
        await bot.handle_stop(update, ctx)

        assert record.terminal_request is None

    async def test_stop_all_cancels_agent_task_children(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=67890, thread_id=None)
        child_route = TelegramRoute(chat_id=67890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route)
        assert parent_state is not None
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-child")
        child_client = MagicMock()
        child_client.interrupt = AsyncMock()
        with patch.object(
            child_state.session_manager,
            "get_client",
            AsyncMock(return_value=child_client),
        ):
            running = asyncio.create_task(asyncio.sleep(30))
            record = _ForkTaskRecord(
                task_id="task-agent-all",
                parent_route=parent_route,
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_route,
                child_session_id="sid-child",
                prompt="Return READY",
                is_fork=False,
                launch_tool_name="AgentTask",
            )
            bot._fork_tasks_by_id["task-agent-all"] = record
            bot._fork_task_tasks["task-agent-all"] = running

            update = _make_update("/stop all", thread_id=None)
            ctx = _make_context()
            ctx.args = ["all"]
            await bot.handle_stop(update, ctx)
            await asyncio.sleep(0)

        assert record.terminal_request == "stopped"
        assert record.status == "stopped"
        assert running.done()
        child_client.interrupt.assert_awaited()

    async def test_stop_all_cancels_multiple_child_tasks(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=67890, thread_id=None)
        child_a_route = TelegramRoute(chat_id=67890, thread_id=321)
        child_b_route = TelegramRoute(chat_id=67890, thread_id=322)
        assert bot._get_state(parent_route) is not None
        child_a_state = bot._get_state(child_a_route)
        child_b_state = bot._get_state(child_b_route)
        assert child_a_state is not None
        assert child_b_state is not None
        child_a_state.session_manager.set_session_id("sid-child-a")
        child_b_state.session_manager.set_session_id("sid-child-b")
        child_a_client = MagicMock()
        child_a_client.interrupt = AsyncMock()
        child_b_client = MagicMock()
        child_b_client.interrupt = AsyncMock()

        with (
            patch.object(
                child_a_state.session_manager,
                "get_client",
                AsyncMock(return_value=child_a_client),
            ),
            patch.object(
                child_b_state.session_manager,
                "get_client",
                AsyncMock(return_value=child_b_client),
            ),
        ):
            task_a = asyncio.create_task(asyncio.sleep(30))
            task_b = asyncio.create_task(asyncio.sleep(30))
            record_a = _ForkTaskRecord(
                task_id="task-agent-a",
                parent_route=parent_route,
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_a_route,
                child_session_id="sid-child-a",
                prompt="Return READY A",
                is_fork=True,
                launch_tool_name="ForkTask",
            )
            record_b = _ForkTaskRecord(
                task_id="task-agent-b",
                parent_route=parent_route,
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_b_route,
                child_session_id="sid-child-b",
                prompt="Return READY B",
                is_fork=True,
                launch_tool_name="ForkTask",
            )
            bot._fork_tasks_by_id["task-agent-a"] = record_a
            bot._fork_tasks_by_id["task-agent-b"] = record_b
            bot._fork_task_tasks["task-agent-a"] = task_a
            bot._fork_task_tasks["task-agent-b"] = task_b

            update = _make_update("/stop all", thread_id=None)
            ctx = _make_context()
            ctx.args = ["all"]
            await bot.handle_stop(update, ctx)
            await asyncio.sleep(0)

        assert record_a.terminal_request == "stopped"
        assert record_b.terminal_request == "stopped"
        assert record_a.status == "stopped"
        assert record_b.status == "stopped"
        assert task_a.done()
        assert task_b.done()
        child_a_client.interrupt.assert_awaited()
        child_b_client.interrupt.assert_awaited()

    async def test_report_writes_case_file_with_metadata(self, config):
        chat_id = -100555666777
        thread_id = 321
        trigger_message_id = 42
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot, chat_id=chat_id, thread_id=thread_id)
        state.session_manager.set_session_id("sid-root")
        bot._session_heads["sid-root"] = "head-uuid"
        bot._record_message_binding(
            route=state.route,
            message_id=trigger_message_id,
            jsonl_uuid="bound-uuid",
            session_id="sid-root",
            role="assistant",
        )

        update = _make_update(
            "/report investigate fork callback mismatch",
            chat_id=chat_id,
            message_id=trigger_message_id,
            thread_id=thread_id,
        )
        ctx = _make_context()
        ctx.args = ["investigate", "fork", "callback", "mismatch"]

        with patch.dict("os.environ", {"OBS_RUNTIME_LOG_FILE": "/tmp/runtime.log"}, clear=False):
            await bot.handle_report(update, ctx)

        ctx.bot.send_message.assert_called_once()
        assert "report saved:" in ctx.bot.send_message.call_args.kwargs["text"]

        report_dir = config.claude_path / "reports" / "cases"
        reports = sorted(report_dir.glob("case-*.md"))
        assert len(reports) == 1
        text = reports[0].read_text()
        assert "- Trigger comment: investigate fork callback mismatch" in text
        assert f"- Chat ID: {chat_id}" in text
        assert f"- Topic thread ID: {thread_id}" in text
        assert f"- Trigger message ID: {trigger_message_id}" in text
        assert "https://t.me/c/555666777/321/42" in text
        assert "- Runtime log file: /tmp/runtime.log" in text
        assert "- Active route session ID: sid-root" in text
        assert "- Active route head UUID: head-uuid" in text
        assert "- Trigger binding UUID: bound-uuid" in text

    async def test_report_without_comment_uses_placeholder(self, config):
        chat_id = -100777888999
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/report", chat_id=chat_id, message_id=50)
        ctx = _make_context()
        ctx.args = []

        await bot.handle_report(update, ctx)

        report_dir = config.claude_path / "reports" / "cases"
        reports = sorted(report_dir.glob("case-*.md"))
        assert len(reports) == 1
        text = reports[0].read_text()
        assert "- Trigger comment: (no comment provided)" in text

    async def test_report_unauthorized_user_does_not_write_case_file(self, config):
        config.telegram_allowed_user_ids = [99999]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/report unauthorized", user_id=12345)
        ctx = _make_context()
        ctx.args = ["unauthorized"]

        await bot.handle_report(update, ctx)

        ctx.bot.send_message.assert_not_called()
        report_dir = config.claude_path / "reports" / "cases"
        assert not report_dir.exists()


class TestTopicCommands:
    async def test_forum_topic_created_populates_title_and_icon_metadata(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("ignored", message_id=42, thread_id=321)
        update.effective_message.forum_topic_created = SimpleNamespace(
            name="Created Title",
            icon_custom_emoji_id="emoji-created",
        )
        update.effective_message.forum_topic_edited = None
        ctx = _make_context()

        await bot.handle_forum_topic_created(update, ctx)

        state = _state(bot, thread_id=321)
        assert state.topic_title == "Created Title"
        assert state.topic_icon_custom_emoji_id == "emoji-created"

    async def test_topic_route_messages_include_thread_id(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        events = [TextEvent(text="done"), TurnEndEvent(), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42, thread_id=321)
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        for call in ctx.bot.send_message.call_args_list:
            assert call.kwargs["message_thread_id"] == 321

    async def test_topic_root_reply_metadata_is_not_treated_as_fork_reply(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("topic hello", message_id=42, thread_id=321)
        update.effective_message.reply_to_message = MagicMock(message_id=321)
        ctx = _make_context()

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._process_message("topic hello", update, ctx)

        trigger = mock_run.call_args.kwargs["trigger_message"]
        assert trigger.reply_to_message_id is None

    async def test_fork_command_creates_topic_from_current_head(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.session_manager.set_session_id("sid-root")
        bot._session_heads["sid-root"] = "head-uuid"
        bot._record_message_binding(
            route=state.route,
            message_id=55,
            jsonl_uuid="head-uuid",
            session_id="sid-root",
            role="assistant",
        )

        update = _make_update("/fork", message_id=77)
        ctx = _make_context()
        ctx.bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=321))
        service_message = MagicMock()
        service_message.message_id = 900
        session_marker = MagicMock()
        session_marker.message_id = 901
        confirm_message = MagicMock()
        confirm_message.message_id = 902
        ctx.bot.send_message = AsyncMock(side_effect=[service_message, session_marker, confirm_message])

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child") as mock_fork:
            await bot.handle_fork(update, ctx)

        mock_fork.assert_called_once()
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            name="General - F1",
            icon_custom_emoji_id=None,
        )
        child_state = _state(bot, thread_id=321)
        assert child_state.session_id == "sid-child"
        assert bot._message_map[(67890, 900)].session_id == "sid-child"
        assert bot._message_map[(67890, 901)].session_id == "sid-child"
        assert ctx.bot.send_message.call_args_list[0].kwargs["message_thread_id"] == 321
        assert ctx.bot.send_message.call_args_list[1].kwargs["message_thread_id"] == 321
        assert ctx.bot.send_message.call_args_list[2].kwargs["message_thread_id"] is None

    async def test_fork_command_resets_unnamed_counter_when_topic_title_changes(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.session_manager.set_session_id("sid-root")
        bot._session_heads["sid-root"] = "head-uuid"
        bot._record_message_binding(
            route=state.route,
            message_id=55,
            jsonl_uuid="head-uuid",
            session_id="sid-root",
            role="assistant",
        )

        first_update = _make_update("/fork", message_id=77)
        first_ctx = _make_context()
        first_ctx.bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=321))
        first_ctx.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=900), MagicMock(message_id=901), MagicMock(message_id=902)]
        )
        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child-1"):
            await bot.handle_fork(first_update, first_ctx)
        first_ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            name="General - F1",
            icon_custom_emoji_id=None,
        )

        state.topic_title = "Renamed General"
        second_update = _make_update("/fork", message_id=78)
        second_ctx = _make_context()
        second_ctx.bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=322))
        second_ctx.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=910), MagicMock(message_id=911), MagicMock(message_id=912)]
        )
        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child-2"):
            await bot.handle_fork(second_update, second_ctx)
        second_ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            name="Renamed General - F1",
            icon_custom_emoji_id=None,
        )

    async def test_forum_topic_edited_updates_route_title(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=321)
        state = bot._get_state(route, topic_title="Old Title")
        assert state is not None
        update = _make_update("ignored", message_id=42, thread_id=321)
        update.effective_message.forum_topic_edited = SimpleNamespace(name="Renamed Title")
        ctx = _make_context()

        await bot.handle_forum_topic_edited(update, ctx)

        assert state.topic_title == "Renamed Title"

    async def test_fork_command_uses_edited_topic_title_and_icon(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot, thread_id=321)
        state.session_manager.set_session_id("sid-root")
        bot._session_heads["sid-root"] = "head-uuid"
        bot._record_message_binding(
            route=state.route,
            message_id=55,
            jsonl_uuid="head-uuid",
            session_id="sid-root",
            role="assistant",
        )

        edited_update = _make_update("ignored", message_id=70, thread_id=321)
        edited_update.effective_message.forum_topic_created = None
        edited_update.effective_message.forum_topic_edited = SimpleNamespace(
            name="Renamed Topic",
            icon_custom_emoji_id="emoji-edited",
        )
        await bot.handle_forum_topic_edited(edited_update, _make_context())

        update = _make_update("/fork", message_id=77, thread_id=321)
        ctx = _make_context()
        ctx.bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=444))
        ctx.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=910), MagicMock(message_id=911), MagicMock(message_id=912)]
        )

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child"):
            await bot.handle_fork(update, ctx)

        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            name="Renamed Topic - F1",
            icon_custom_emoji_id="emoji-edited",
        )

    async def test_fork_command_reply_uses_replied_binding_and_explicit_label(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        bot._message_map[(67890, 12)] = _TelegramMessageBinding(
            jsonl_uuid="older-uuid",
            session_id="sid-root",
            role="assistant",
            route=state.route,
        )
        bot._session_heads["sid-root"] = "latest-uuid"

        update = _make_update("/fork Focused topic", message_id=77)
        update.effective_message.reply_to_message = MagicMock(message_id=12)
        ctx = _make_context()
        ctx.args = ["Focused", "topic"]
        ctx.bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=444))
        service_message = MagicMock()
        service_message.message_id = 910
        session_marker = MagicMock()
        session_marker.message_id = 911
        confirm_message = MagicMock()
        confirm_message.message_id = 912
        ctx.bot.send_message = AsyncMock(side_effect=[service_message, session_marker, confirm_message])

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child") as mock_fork:
            await bot.handle_fork(update, ctx)

        assert mock_fork.call_args.kwargs["target_uuid"] == "older-uuid"
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=67890,
            name="General - Focused topic",
            icon_custom_emoji_id=None,
        )

    async def test_deferred_bindings_can_backfill_topic_head_after_session_id_appears(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        deferred_bindings = [(77, "late-uuid", "assistant")]

        state.session_manager.set_session_id("sid-late")
        bot._flush_deferred_bindings(
            route=state.route,
            deferred_bindings=deferred_bindings,
            session_id=state.session_id,
        )
        bot._refresh_session_head(
            state=state,
            latest_turn_uuid="late-uuid",
            source="test",
        )

        assert bot._message_map[(67890, 77)].jsonl_uuid == "late-uuid"
        assert bot._session_heads["sid-late"] == "late-uuid"

    async def test_delete_current_topic_drops_route_state(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        topic_state = _state(bot, thread_id=321)
        topic_state.session_manager.set_session_id("sid-topic")

        update = _make_update("/delete", thread_id=321)
        ctx = _make_context()
        ctx.bot.delete_forum_topic = AsyncMock(return_value=True)

        await bot.handle_delete(update, ctx)

        ctx.bot.delete_forum_topic.assert_awaited_once_with(chat_id=67890, message_thread_id=321)
        assert TelegramRoute(chat_id=67890, thread_id=321) not in bot._states_by_route

    async def test_delete_all_replies_in_general_route(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        _state(bot)
        _state(bot, thread_id=321)

        update = _make_update("/delete all", thread_id=321)
        ctx = _make_context()
        ctx.args = ["all"]
        ctx.bot.delete_forum_topic = AsyncMock(return_value=True)

        await bot.handle_delete(update, ctx)

        assert ctx.bot.send_message.call_args.kwargs["message_thread_id"] is None
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>all non-General topics deleted</i></u>"
        assert ctx.bot.send_message.call_args.kwargs["disable_notification"] is False


class TestForkTaskRuntime:
    async def test_launch_agent_task_fails_fast_when_topic_creation_badrequest(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(
            side_effect=BadRequest("not enough rights to manage topics")
        )
        state.last_bot.send_message = AsyncMock()
        state.session_manager.set_session_id("sid-root")

        with pytest.raises(RuntimeError, match="Failed to create forum topic"):
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Launch worker",
                    "description": "Topic launch",
                    "fork": False,
                    "task_tool_name": "AgentTask",
                },
            )

        state.last_bot.create_forum_topic.assert_awaited_once()
        state.last_bot.send_message.assert_not_awaited()
        assert bot._fork_tasks_by_id == {}
        assert state.active_fork_task_ids == set()
        await bot.shutdown()

    async def test_launch_agent_task_bounds_topic_creation_retries(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(side_effect=TelegramError("network failure"))
        state.last_bot.send_message = AsyncMock()
        state.session_manager.set_session_id("sid-root")

        async def _no_sleep(_delay: float) -> None:
            return None

        with patch("obs_agent.telegram.asyncio.sleep", new=AsyncMock(side_effect=_no_sleep)):
            with pytest.raises(RuntimeError, match="Failed to create forum topic"):
                await bot._launch_fork_task(
                    route=route,
                    args={
                        "prompt": "Launch worker",
                        "description": "Topic launch",
                        "fork": False,
                        "task_tool_name": "AgentTask",
                    },
                )

        assert state.last_bot.create_forum_topic.await_count == 3
        state.last_bot.send_message.assert_not_awaited()
        assert bot._fork_tasks_by_id == {}
        assert state.active_fork_task_ids == set()
        await bot.shutdown()

    async def test_launch_fork_task_creates_record_and_child_topic(self, config, tmp_path):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=321))
        service_message = MagicMock()
        service_message.message_id = 900
        session_marker = MagicMock()
        session_marker.message_id = 901
        confirmation_message = MagicMock()
        confirmation_message.message_id = 902
        state.last_bot.send_message = AsyncMock(
            side_effect=[service_message, session_marker, confirmation_message]
        )
        state.session_manager.set_session_id("sid-root")
        state.session_manager.model_override = "gpt-5.5"
        bot._session_heads["sid-root"] = "head-uuid"
        bot._record_message_binding(
            route=route,
            message_id=55,
            jsonl_uuid="head-uuid",
            session_id="sid-root",
            role="assistant",
        )

        child_jsonl = tmp_path / "sid-child.jsonl"
        child_jsonl.write_text("", encoding="utf-8")
        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-child") as mock_fork, patch(
            "obs_agent.telegram.find_session_jsonl", return_value=child_jsonl
        ), patch.object(bot, "_execute_fork_task", new_callable=AsyncMock):
            launched = await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Inspect the docs and return READY",
                    "description": "Audit",
                    "timeout_ms": 5000,
                    "max_turns": 9,
                },
            )

        mock_fork.assert_called_once()
        state.last_bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-10067890,
            name="General - Audit",
            icon_custom_emoji_id=None,
        )
        launch_text = launched["content"][0]["text"]
        assert "ForkTask launched successfully." in launch_text
        task_id = launch_text.split("agentId: ", 1)[1].splitlines()[0]
        assert "output_file:" in launch_text
        assert "telegram_topic: https://t.me/c/67890/321/900" in launch_text
        assert task_id in bot._fork_tasks_by_id
        record = bot._fork_tasks_by_id[task_id]
        assert record.child_route == TelegramRoute(chat_id=-10067890, thread_id=321)
        assert record.child_session_id == "sid-child"
        assert record.launch_child_message_id == 900
        assert record.launch_parent_message_id == 902
        assert record.max_turns is None
        assert task_id in state.active_fork_task_ids
        send_calls = state.last_bot.send_message.await_args_list
        assert "fork task launched by agent" in send_calls[0].kwargs["text"]
        assert "source message" in send_calls[0].kwargs["text"]
        assert "https://t.me/c/67890/55" in send_calls[0].kwargs["text"]
        assert "session forked, your new session id is sid-child" in send_calls[1].kwargs["text"]
        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=321))
        assert child_state is not None
        assert child_state.notify_on_completion is True
        assert child_state.session_manager.model_override == "gpt-5.5"
        assert child_state.session_manager.create_options().model == "gpt-5.5[1m]"
        assert (
            child_state.session_manager.create_options().env["ANTHROPIC_API_KEY"]
            == config.cli_proxy_api_key
        )
        await bot.shutdown()

    async def test_launch_super_task_without_fork_creates_fresh_child_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=333))
        state.last_bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=920), MagicMock(message_id=921), MagicMock(message_id=922)]
        )
        state.session_manager.set_session_id("sid-root")
        state.session_manager.model_override = "gpt-5.5"

        # Use a unique team name to avoid collision with stale inbox files
        unique_team = f"team-fresh-{uuid.uuid4().hex[:8]}"
        fake_task_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        with patch("obs_agent.telegram.uuid.uuid4", side_effect=[fake_task_id]), patch(
            "obs_agent.telegram.fork_session_jsonl"
        ) as mock_fork, patch.object(bot, "_execute_fork_task", new_callable=AsyncMock):
            launched = await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Start fresh and return READY-FRESH",
                    "description": "Fresh child",
                    "fork": False,
                    "team_name": unique_team,
                },
            )

        mock_fork.assert_not_called()
        state.last_bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-10067890,
            name="General - Fresh child",
            icon_custom_emoji_id=None,
        )
        launch_text = launched["content"][0]["text"]
        assert "AgentTask launched successfully." in launch_text
        assert "agentId: 11111111-1111-1111-1111-111111111111" in launch_text
        record = bot._fork_tasks_by_id["11111111-1111-1111-1111-111111111111"]
        assert record.is_fork is False
        assert record.child_session_id == ""
        assert record.launch_tool_name == "AgentTask"
        assert record.team_name == unique_team
        # With two-tier naming, agent_name is computed from lineage, not raw name param
        assert record.agent_name is not None
        assert "fresh-child" in record.agent_name.lower(), \
            f"Agent name should contain 'fresh-child' slug, got: {record.agent_name}"
        send_calls = state.last_bot.send_message.await_args_list
        assert "agent task launched by agent" in send_calls[0].kwargs["text"]
        assert f"team_name: {unique_team}" in send_calls[0].kwargs["text"]
        assert (
            send_calls[1].kwargs["text"]
            == "<u><i>session launched; a fresh session id will be assigned on first turn</i></u>"
        )
        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=333))
        assert child_state is not None
        assert child_state.session_id is None
        assert child_state.session_manager.model_override == "gpt-5.5"
        child_options = child_state.session_manager.create_options()
        assert child_options.model == "gpt-5.5[1m]"
        child_env = child_options.env
        assert child_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
        assert child_env["CLAUDE_CODE_ENABLE_TASKS"] == "1"
        assert child_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
        assert child_env["CLAUDE_CODE_TASK_LIST_ID"] == unique_team
        assert child_env["CLAUDE_CODE_TEAM_NAME"] == unique_team
        assert child_env["ANTHROPIC_API_KEY"] == config.cli_proxy_api_key
        # Agent name should be computed (hash-prefix + slug), not raw alias
        assert "fresh-child" in child_env["CLAUDE_CODE_AGENT_NAME"].lower(), \
            f"Agent name env var should contain 'fresh-child', got: {child_env['CLAUDE_CODE_AGENT_NAME']}"
        await bot.shutdown()

    async def test_launch_agent_task_explicit_shorthand_model_gets_1m_at_sdk_boundary(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=334))
        state.last_bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=930), MagicMock(message_id=931), MagicMock(message_id=932)]
        )
        state.session_manager.set_session_id("sid-root")

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Start fresh and report model context",
                    "description": "GPT child",
                    "fork": False,
                    "model": "gpt",
                    "task_tool_name": "AgentTask",
                },
            )

        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=334))
        assert child_state is not None
        assert child_state.session_manager.model_override == "gpt-5.4-mini"
        child_options = child_state.session_manager.create_options()
        assert child_options.model == "gpt-5.4-mini[1m]"
        assert child_options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
        assert child_options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
        assert child_options.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "92"
        assert child_options.env["ANTHROPIC_API_KEY"] == config.cli_proxy_api_key
        await bot.shutdown()

    async def test_launch_agent_task_explicit_context_model_preserves_context_at_sdk_boundary(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=335))
        state.last_bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=940), MagicMock(message_id=941), MagicMock(message_id=942)]
        )
        state.session_manager.set_session_id("sid-root")

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Start fresh and report model context",
                    "description": "Small GPT child",
                    "fork": False,
                    "model": "gpt[200k]",
                    "task_tool_name": "AgentTask",
                },
            )

        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=335))
        assert child_state is not None
        assert child_state.session_manager.model_override == "gpt-5.4-mini[200k]"
        child_options = child_state.session_manager.create_options()
        assert child_options.model == "gpt-5.4-mini[200k]"
        assert child_options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "200000"
        assert child_options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
        assert child_options.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "84"
        await bot.shutdown()

    async def test_launch_agent_task_inherit_model_keeps_parent_identity_and_adds_1m(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        config.model = "gpt-5.4-mini"
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=336))
        state.last_bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=950), MagicMock(message_id=951), MagicMock(message_id=952)]
        )
        state.session_manager.set_session_id("sid-root")

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Inherit model",
                    "description": "Inherited child",
                    "fork": False,
                    "model": "inherit",
                    "task_tool_name": "AgentTask",
                },
            )

        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=336))
        assert child_state is not None
        assert child_state.session_manager.model_override is None
        child_options = child_state.session_manager.create_options()
        assert child_options.model == "gpt-5.4-mini[1m]"
        assert child_options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
        await bot.shutdown()

    async def test_scheduled_run_uses_route_model_context_semantics(
        self,
        config,
    ):
        config.model = "gpt-5.4-mini"
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=337)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=960), MagicMock(message_id=961)])
        state.session_manager.set_session_id("sid-root")
        record = _TopicScheduleRecord(
            schedule_id="schedule-model-context",
            route=route,
            description="Model context",
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=60,
            prompt="Report context",
            max_runs=1,
            next_run_at=0,
        )
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="DONE"))

        with patch.object(bot, "_run_and_send", run_mock):
            await bot._execute_topic_schedule(record=record, trigger_kind="interval")

        options = state.session_manager.create_options()
        assert options.model == "gpt-5.4-mini[1m]"
        assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
        run_text = run_mock.await_args.kwargs["user_text"]
        assert run_text.startswith("(System: scheduled execution.)")
        await bot.shutdown()

    async def test_launch_agent_task_registers_named_team_workers_for_peer_discovery(
        self,
        config,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.last_bot.create_forum_topic = AsyncMock(
            side_effect=[
                MagicMock(message_thread_id=333),
                MagicMock(message_thread_id=334),
            ]
        )
        state.last_bot.send_message = AsyncMock(
            side_effect=[
                MagicMock(message_id=920),
                MagicMock(message_id=921),
                MagicMock(message_id=922),
                MagicMock(message_id=923),
                MagicMock(message_id=924),
                MagicMock(message_id=925),
            ]
        )
        state.session_manager.set_session_id("sid-root")

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Worker A boot",
                    "description": "Peer A",
                    "fork": False,
                    "team_name": "team-alpha",
                    "name": "worker-a",
                    "task_tool_name": "AgentTask",
                },
            )
            await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Worker B boot",
                    "description": "Peer B",
                    "fork": False,
                    "team_name": "team-alpha",
                    "name": "worker-b",
                    "task_tool_name": "AgentTask",
                },
            )

        team_config = tmp_path / ".claude" / "teams" / "team-alpha" / "config.json"
        assert team_config.exists()
        payload = json.loads(team_config.read_text(encoding="utf-8"))
        members = payload.get("members") or []
        member_names = {str(member.get("name")) for member in members if isinstance(member, dict)}
        # With two-tier naming, member names use {parent_hash}-{slug} format.
        # The description ("Peer A", "Peer B") becomes the lineage name, slugified.
        assert any("peer-a" in n for n in member_names), \
            f"Expected a member with 'peer-a' in name, got: {member_names}"
        assert any("peer-b" in n for n in member_names), \
            f"Expected a member with 'peer-b' in name, got: {member_names}"
        peer_a = next(member for member in members if isinstance(member, dict) and "peer-a" in str(member.get("name")))
        peer_a_obs = peer_a.get("obs")
        assert isinstance(peer_a_obs, dict)
        assert peer_a_obs["display_name"] == "Peer A"
        assert peer_a_obs["lineage"] == ["General", "Peer A"]
        assert peer_a_obs["root_team_key"] == "team-alpha"
        assert peer_a_obs["parent_agent_name"] == "team-alpha"
        assert peer_a_obs["topic_chat_id"] == -10067890
        assert peer_a_obs["topic_thread_id"] == 333
        await bot.shutdown()

    def test_fork_task_prompt_file_context_keeps_inline_prompt_separate(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        child_prompt = bot._compose_prompt_file_context(
            prompt="Inline instruction",
            prompt_file="procedures/task.md",
            prompt_file_content="File context <with markup>",
        )

        assert '<prompt_file_context path="procedures/task.md">' in child_prompt
        assert "File context &lt;with markup&gt;" in child_prompt
        assert child_prompt.endswith("\n\nInline instruction")

    def test_fork_task_child_service_html_shows_prompt_and_prompt_file(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        service_html = bot._build_fork_task_child_service_html(
            agent_id="task-123",
            description="Audit",
            prompt="Inline instruction",
            source_link=None,
            prompt_file="procedures/task.md",
            prompt_file_content="File context",
        )

        assert "prompt_file: procedures/task.md" in service_html
        assert "File context" not in service_html
        assert "prompt:" in service_html
        assert "Inline instruction" in service_html

    def test_build_super_task_lifecycle_html_uses_system_heading_and_cursive_body(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=TelegramRoute(chat_id=-10067890, thread_id=321),
            child_session_id="sid-child",
            prompt="Return READY",
            description="Worker A",
            is_fork=False,
        )

        rendered = bot._build_super_task_lifecycle_html(
            record=record,
            phase="idle",
            elapsed_seconds=42.8,
            idle_seconds=18.3,
        )

        assert "<u><i>notification: agent task idle</i></u>" in rendered
        assert "<i>agentId: task-123</i>" in rendered
        assert "<i>description: Worker A</i>" in rendered
        assert "<i>elapsed_s: 42</i>" in rendered
        assert "<i>idle_for_s: 18</i>" in rendered

    async def test_monitor_super_task_lifecycle_emits_running_and_idle(self, config):
        bot = TelegramBot(
            config,
            fragment_gap=_TEST_GAP,
            enable_background_poller=False,
            super_task_heartbeat_seconds=0.06,
            super_task_idle_seconds=0.12,
            super_task_monitor_tick_seconds=0.02,
        )
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Worker")
        assert parent_state is not None
        assert child_state is not None

        fake_bot = MagicMock()
        fake_bot.send_chat_action = AsyncMock()
        sent_messages = []

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = 950 + len(sent_messages)
            sent_messages.append(kwargs)
            return message

        fake_bot.send_message = AsyncMock(side_effect=send_side_effect)
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-child")

        record = _ForkTaskRecord(
            task_id="task-super-1",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Run long job",
            description="Team Worker",
            launch_parent_message_id=902,
            is_fork=False,
        )
        background = asyncio.create_task(asyncio.sleep(0.30))
        bot._fork_task_tasks[record.task_id] = background

        monitor = asyncio.create_task(
            bot._monitor_super_task_lifecycle(
                record=record,
                child_state=child_state,
                parent_state=parent_state,
            )
        )
        await asyncio.sleep(0.22)
        await background
        await monitor
        bot._fork_task_tasks.pop(record.task_id, None)

        assert sent_messages == []
        await bot.shutdown()

    async def test_execute_fork_task_enqueues_parent_callback(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Audit")
        assert parent_state is not None
        assert child_state is not None

        fake_bot = MagicMock()
        child_terminal = MagicMock()
        child_terminal.message_id = 910
        parent_marker = MagicMock()
        parent_marker.message_id = 911
        fake_bot.send_message = AsyncMock(side_effect=[child_terminal, parent_marker])
        fake_bot.edit_message_text = AsyncMock()
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-parent"] = "parent-current-uuid"
        bot._session_heads["sid-child"] = "child-final-uuid"
        parent_state.active_fork_task_ids.add("task-123")

        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return SECRET-42",
            prompt_file="context.md",
            prompt_file_content="Use this context",
            description="Audit",
            timeout_ms=5000,
            launch_parent_message_id=901,
            launch_child_message_id=900,
            team_name="team-alpha",
            agent_name="worker-a",
        )
        bot._fork_tasks_by_id["task-123"] = record
        bot._fork_task_by_child_route[child_route] = "task-123"

        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="SECRET-42"))
        with patch.object(
            bot,
            "_run_and_send",
            run_mock,
        ):
            await bot._execute_fork_task("task-123")

        assert record.status == "completed"
        assert record.result_text == "SECRET-42"
        assert record.child_completion_message_id == 910
        assert record.parent_callback_message_id == 911
        assert "task-123" not in parent_state.active_fork_task_ids
        queued = parent_state.hook_state.message_queue.get_nowait()
        assert "<task-notification>" in queued
        assert "<status>completed</status>" in queued
        assert "SECRET-42" in queued
        assert "https://t.me/c/67890/321/910" in queued
        send_calls = fake_bot.send_message.await_args_list
        assert "subtask: fork completed" in send_calls[0].kwargs["text"]
        assert "open child completion" in send_calls[1].kwargs["text"]
        assert "https://t.me/c/67890/321/910" in send_calls[1].kwargs["text"]
        run_text = run_mock.await_args.kwargs["user_text"]
        assert run_text == (
            '<prompt_file_context path="context.md">\n'
            'Use this context\n'
            '</prompt_file_context>\n\n'
            'Return SECRET-42'
        )
        fake_bot.edit_message_text.assert_awaited_once()
        assert "subtask: fork completed" in fake_bot.edit_message_text.await_args.kwargs["text"]
        assert "return_to_parent: https://t.me/c/67890/911" in fake_bot.edit_message_text.await_args.kwargs["text"]
        assert record.idle_ready is True
        assert bot._fork_task_by_child_route[child_route] == "task-123"
        assert bot._team_worker_records[("team-alpha", "worker-a")] == "task-123"

    async def test_execute_super_task_team_worker_stays_idle_ready(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert parent_state is not None
        assert child_state is not None

        fake_bot = MagicMock()
        child_terminal = MagicMock()
        child_terminal.message_id = 912
        parent_marker = MagicMock()
        parent_marker.message_id = 913
        fake_bot.send_message = AsyncMock(side_effect=[child_terminal, parent_marker])
        fake_bot.edit_message_text = AsyncMock()
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-parent"] = "parent-current-uuid"
        bot._session_heads["sid-child"] = "child-final-uuid"
        parent_state.active_fork_task_ids.add("task-team")

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Handle team task",
            description="Team Worker",
            launch_parent_message_id=901,
            launch_child_message_id=900,
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"

        with patch.object(
            bot,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="TEAM-OK")),
        ):
            await bot._execute_fork_task("task-team")

        assert record.status == "completed"
        assert record.idle_ready is True
        assert bot._fork_task_by_child_route[child_route] == "task-team"
        assert bot._team_worker_records[("team-alpha", "worker-a")] == "task-team"
        assert "task-team" not in parent_state.active_fork_task_ids
        await bot.shutdown()

    async def test_inbox_message_wakes_idle_team_worker(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert child_state is not None
        fake_bot = MagicMock()
        wake_message = MagicMock()
        wake_message.message_id = 930
        fake_bot.send_message = AsyncMock(return_value=wake_message)
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-child"] = "child-head-uuid"

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Old prompt",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "please process item 7",
                },
            )

        schedule_mock.assert_awaited_once_with(task_id="task-team", parent_state=child_state)
        assert record.status == "launched"
        assert record.idle_ready is False
        assert record.emit_parent_callback is False
        assert "ReadInbox" in record.prompt
        assert "team_name=team-alpha" in record.prompt
        assert "agent=worker-a" in record.prompt
        assert "Latest sender: worker-b." in record.prompt
        assert "Latest summary: handoff." in record.prompt
        assert "Latest content preview: please process item 7" in record.prompt
        assert fake_bot.send_message.await_count == 1
        await bot.shutdown()

    async def test_inbox_message_wakes_completed_team_worker_even_if_idle_flag_false(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert child_state is not None
        fake_bot = MagicMock()
        wake_message = MagicMock()
        wake_message.message_id = 935
        fake_bot.send_message = AsyncMock(return_value=wake_message)
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-child"] = "child-head-uuid"

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Old prompt",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="completed",
            idle_ready=False,
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "please process item 7",
                },
            )

        schedule_mock.assert_awaited_once_with(task_id="task-team", parent_state=child_state)
        assert record.status == "launched"
        assert record.idle_ready is False
        await bot.shutdown()

    async def test_inbox_message_wakes_idle_forked_team_worker(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="General - Fork Worker")
        assert child_state is not None
        fake_bot = MagicMock()
        wake_message = MagicMock()
        wake_message.message_id = 937
        fake_bot.send_message = AsyncMock(return_value=wake_message)
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-fork-child")
        bot._session_heads["sid-fork-child"] = "fork-child-head-uuid"

        record = _ForkTaskRecord(
            task_id="task-fork-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-fork-child",
            prompt="Old fork prompt",
            description="Fork Worker",
            team_name="team-alpha",
            agent_name="worker-fork",
            is_fork=True,
            status="completed",
            idle_ready=True,
        )
        bot._fork_tasks_by_id["task-fork-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-fork-team"
        bot._team_worker_records[("team-alpha", "worker-fork")] = "task-fork-team"

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-fork",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "please process fork item 7",
                },
            )

        schedule_mock.assert_awaited_once_with(task_id="task-fork-team", parent_state=child_state)
        assert record.status == "launched"
        assert record.idle_ready is False
        assert "ReadInbox" in record.prompt
        assert "agent=worker-fork" in record.prompt
        assert fake_bot.send_message.await_count == 1
        await bot.shutdown()

    async def test_inbox_message_queues_notice_when_worker_is_running(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Worker Topic")
        assert child_state is not None
        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Old prompt",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="launched",
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"
        running = asyncio.create_task(asyncio.sleep(30))
        bot._fork_task_tasks["task-team"] = running
        try:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process item 8",
                },
            )
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running

        queued = child_state.hook_state.message_queue.get_nowait()
        assert isinstance(queued, QueuedMessage)
        assert "System notification: New teammate messages arrived while you were still running." in queued.text
        assert "ReadInbox with team_name=team-alpha, agent=worker-a" in queued.text
        assert "Latest sender: worker-b." in queued.text
        assert "Latest summary: handoff." in queued.text
        assert "Latest content preview: process item 8" in queued.text
        assert record.wake_requested is False
        await bot.shutdown()

    async def test_inbox_message_queues_notice_when_route_is_running(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=222)
        state = bot._get_state(route, topic_title="Busy Root")
        assert state is not None
        state.busy = True
        bot._route_inbox_targets[("team-alpha", "root-agent")] = route

        await bot._handle_inbox_message_notification(
            sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
            payload={
                "team_name": "team-alpha",
                "recipient": "root-agent",
                "sender": "worker-b",
                "summary": "handoff",
                "content": "process root item 3",
            },
        )

        queued = state.hook_state.message_queue.get_nowait()
        assert isinstance(queued, QueuedMessage)
        assert "System notification: New teammate messages arrived while you were still running." in queued.text
        assert "ReadInbox with team_name=team-alpha, agent=root-agent" in queued.text
        assert "Latest sender: worker-b." in queued.text
        assert "Latest summary: handoff." in queued.text
        assert "Latest content preview: process root item 3" in queued.text
        await bot.shutdown()

    async def test_poll_team_worker_inbox_queues_notice_when_worker_is_running(
        self,
        config,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Worker Topic")
        assert child_state is not None
        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Old prompt",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="launched",
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"
        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(
            json.dumps(
                [
                    {
                        "from": "worker-b",
                        "text": "process item 8",
                        "summary": "handoff",
                        "timestamp": "2026-03-13T00:00:00Z",
                        "read": False,
                    }
                ],
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        running = asyncio.create_task(asyncio.sleep(30))
        bot._fork_task_tasks["task-team"] = running
        try:
            await bot._poll_team_worker_inbox_wakes()
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running

        queued = child_state.hook_state.message_queue.get_nowait()
        assert isinstance(queued, QueuedMessage)
        assert "System notification: New teammate messages arrived while you were still running." in queued.text
        assert "ReadInbox with team_name=team-alpha, agent=worker-a" in queued.text
        assert "Latest sender: worker-b." in queued.text
        assert "Latest summary: handoff." in queued.text
        assert "Latest content preview: process item 8" in queued.text
        assert record.wake_requested is False
        await bot.shutdown()

    async def test_poll_team_worker_inbox_keeps_team_scopes_separate(
        self,
        config,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route_a = TelegramRoute(chat_id=-10067890, thread_id=321)
        route_b = TelegramRoute(chat_id=-10067890, thread_id=322)
        state_a = bot._get_state(route_a, topic_title="General - Team A")
        state_b = bot._get_state(route_b, topic_title="General - Team B")
        assert state_a is not None
        assert state_b is not None
        state_a.session_manager.set_session_id("sid-a")
        state_b.session_manager.set_session_id("sid-b")
        bot._session_heads["sid-a"] = "uuid-a"
        bot._session_heads["sid-b"] = "uuid-b"
        state_a.last_bot = MagicMock(send_message=AsyncMock(return_value=MagicMock(message_id=941)))
        state_b.last_bot = MagicMock(send_message=AsyncMock(return_value=MagicMock(message_id=942)))

        record_a = _ForkTaskRecord(
            task_id="task-team-a",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=route_a,
            child_session_id="sid-a",
            prompt="Old prompt A",
            description="Team Worker A",
            team_name="team-alpha",
            agent_name="worker-shared",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )
        record_b = _ForkTaskRecord(
            task_id="task-team-b",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=route_b,
            child_session_id="sid-b",
            prompt="Old prompt B",
            description="Team Worker B",
            team_name="team-beta",
            agent_name="worker-shared",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )
        bot._fork_tasks_by_id[record_a.task_id] = record_a
        bot._fork_tasks_by_id[record_b.task_id] = record_b
        bot._fork_task_by_child_route[route_a] = record_a.task_id
        bot._fork_task_by_child_route[route_b] = record_b.task_id
        bot._team_worker_records[("team-alpha", "worker-shared")] = record_a.task_id
        bot._team_worker_records[("team-beta", "worker-shared")] = record_b.task_id

        inbox_path = tmp_path / ".claude" / "teams" / "team-beta" / "inboxes" / "worker-shared.json"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(
            json.dumps(
                [
                    {
                        "from": "worker-peer",
                        "text": "team-beta message",
                        "summary": "beta-summary",
                        "timestamp": "2026-03-13T00:00:00Z",
                        "read": False,
                    }
                ],
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await bot._poll_team_worker_inbox_wakes()

        schedule_mock.assert_awaited_once_with(task_id="task-team-b", parent_state=state_b)
        assert record_a.status == "completed"
        assert record_b.status == "launched"
        assert record_b.idle_ready is False
        await bot.shutdown()

    async def test_inbox_message_wakes_idle_lineage_route_and_merges_queued_updates(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=654)
        state = bot._get_state(route, topic_title="General - Fork Child")
        assert state is not None
        state.session_manager.set_session_id("sid-route-child")
        bot._bind_state_session(state)
        bot._set_session_head(session_id="sid-route-child", jsonl_uuid="uuid-route-child")
        fake_bot = MagicMock()
        wake_message = MagicMock()
        wake_message.message_id = 943
        fake_bot.send_message = AsyncMock(return_value=wake_message)
        state.last_bot = fake_bot
        lineage = ("General", "Fork Child")
        team_name = root_team_key_for_lineage(lineage)
        agent_name = agent_name_for_lineage(lineage)
        bot._prime_obs_bootstrap(
            state,
            lineage=lineage,
            origin="user_fork",
            is_fork=True,
            session_id="sid-route-child",
        )
        state.hook_state.message_queue.put_nowait(
            QueuedMessage(text="queued user followup", telegram_message_id=944)
        )

        with patch.object(
            bot,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="ROUTE-WAKE-OK")),
        ) as run_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=777),
                payload={
                    "team_name": team_name,
                    "recipient": agent_name,
                    "sender": "worker-peer",
                    "summary": "handoff",
                    "content": "route wake item 17",
                },
            )

        run_mock.assert_awaited_once()
        kwargs = run_mock.await_args.kwargs
        assert kwargs["state"] is state
        assert kwargs["bot"] is fake_bot
        assert "ReadInbox" in kwargs["user_text"]
        assert f"team_name={team_name}" in kwargs["user_text"]
        assert f"agent={agent_name}" in kwargs["user_text"]
        assert kwargs["trigger_message"].telegram_message_id == 943
        assert kwargs["extra_pending"] is not None
        assert [message.text for message in kwargs["extra_pending"]] == ["queued user followup"]
        assert fake_bot.send_message.await_count == 1
        await bot.shutdown()

    async def test_poll_team_worker_inbox_wakes_idle_lineage_route_without_task_handle(
        self,
        config,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=654)
        state = bot._get_state(route, topic_title="General - Fork Child")
        assert state is not None
        state.session_manager.set_session_id("sid-route-child")
        bot._bind_state_session(state)
        bot._set_session_head(session_id="sid-route-child", jsonl_uuid="uuid-route-child")
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=944))
        state.last_bot = fake_bot
        lineage = ("General", "Fork Child")
        team_name = root_team_key_for_lineage(lineage)
        agent_name = agent_name_for_lineage(lineage)
        bot._prime_obs_bootstrap(
            state,
            lineage=lineage,
            origin="user_fork",
            is_fork=True,
            session_id="sid-route-child",
        )

        inbox_path = tmp_path / ".claude" / "teams" / team_name / "inboxes" / f"{agent_name}.json"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(
            json.dumps(
                [
                    {
                        "from": "worker-peer",
                        "text": "poll wake payload",
                        "summary": "poll-handoff",
                        "timestamp": "2026-03-14T00:00:00Z",
                        "read": False,
                    }
                ],
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        with patch.object(
            bot,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="POLL-WAKE-OK")),
        ) as run_mock:
            await bot._poll_team_worker_inbox_wakes()

        run_mock.assert_awaited_once()
        kwargs = run_mock.await_args.kwargs
        assert kwargs["state"] is state
        assert kwargs["bot"] is fake_bot
        assert "poll wake payload" in kwargs["user_text"]
        assert fake_bot.send_message.await_count == 1
        await bot.shutdown()

    async def test_execute_super_task_triggers_pending_idle_wake_after_completion(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert parent_state is not None
        assert child_state is not None

        fake_bot = MagicMock()
        child_terminal = MagicMock()
        child_terminal.message_id = 940
        parent_marker = MagicMock()
        parent_marker.message_id = 941
        fake_bot.send_message = AsyncMock(side_effect=[child_terminal, parent_marker])
        fake_bot.edit_message_text = AsyncMock()
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-parent"] = "parent-current-uuid"
        bot._session_heads["sid-child"] = "child-final-uuid"
        parent_state.active_fork_task_ids.add("task-team")

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Handle team task",
            description="Team Worker",
            launch_parent_message_id=901,
            launch_child_message_id=900,
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            wake_requested=True,
            wake_source_sender="worker-b",
            wake_source_summary="handoff",
            wake_source_content="process item 9",
        )
        bot._fork_tasks_by_id["task-team"] = record
        bot._fork_task_by_child_route[child_route] = "task-team"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-team"

        with patch.object(
            bot,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="TEAM-OK")),
        ), patch.object(
            bot,
            "_start_idle_team_worker_wake",
            new_callable=AsyncMock,
        ) as wake_mock:
            await bot._execute_fork_task("task-team")

        wake_mock.assert_awaited_once_with(
            record=record,
            sender="worker-b",
            summary="handoff",
            content="process item 9",
        )
        await bot.shutdown()

    async def test_wake_uses_current_topic_head_session_for_team_worker(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="General - Team Worker")
        assert child_state is not None
        fake_bot = MagicMock()
        wake_message = MagicMock()
        wake_message.message_id = 936
        fake_bot.send_message = AsyncMock(return_value=wake_message)
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-current")
        bot._session_heads["sid-legacy"] = "uuid-legacy"
        bot._session_heads["sid-current"] = "uuid-current"

        record = _ForkTaskRecord(
            task_id="task-team",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="uuid-parent",
            child_route=child_route,
            child_session_id="sid-legacy",
            prompt="Old prompt",
            description="Team Worker",
            team_name="team-alpha",
            agent_name="worker-a",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._start_idle_team_worker_wake(
                record=record,
                sender="worker-b",
                summary="handoff",
                content="process item 10",
            )

        assert record.child_session_id == "sid-current"
        assert record.parent_session_id_at_launch == "sid-current"
        assert record.parent_source_uuid == "uuid-current"
        assert "agent=worker-a" in record.prompt
        await bot.shutdown()

    async def test_execute_fork_task_uses_terminal_request_status(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Audit")
        assert parent_state is not None
        assert child_state is not None

        fake_bot = MagicMock()
        child_terminal = MagicMock()
        child_terminal.message_id = 910
        parent_marker = MagicMock()
        parent_marker.message_id = 911
        fake_bot.send_message = AsyncMock(side_effect=[child_terminal, parent_marker])
        fake_bot.edit_message_text = AsyncMock()
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        bot._session_heads["sid-parent"] = "parent-current-uuid"
        bot._session_heads["sid-child"] = "child-final-uuid"
        parent_state.active_fork_task_ids.add("task-123")

        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return SECRET-42",
            timeout_ms=5000,
            launch_parent_message_id=901,
            launch_child_message_id=900,
            terminal_request="stopped",
        )
        bot._fork_tasks_by_id["task-123"] = record
        bot._fork_task_by_child_route[child_route] = "task-123"

        with patch.object(
            bot,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="SECRET-42")),
        ):
            await bot._execute_fork_task("task-123")

        assert record.status == "stopped"
        queued = parent_state.hook_state.message_queue.get_nowait()
        assert "<status>stopped</status>" in queued

    async def test_fork_task_output_reports_running(self, config, tmp_path):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
        )
        bot._fork_tasks_by_id["task-123"] = record
        output_path = tmp_path / "sid-child.jsonl"
        output_path.write_text('{"type":"user"}\n', encoding="utf-8")
        task = asyncio.create_task(asyncio.sleep(30))
        bot._fork_task_tasks["task-123"] = task
        try:
            with patch("obs_agent.telegram.find_session_jsonl", return_value=output_path):
                result = await bot._fork_task_output(
                    route=record.parent_route,
                    args={"task_id": "task-123", "block": False, "timeout": 1},
                )
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert "<retrieval_status>not_ready</retrieval_status>" in result["content"][0]["text"]
        assert "<status>running</status>" in result["content"][0]["text"]

    async def test_fork_task_output_reports_completed_after_completion(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="completed",
            result_text="READY",
        )
        bot._fork_tasks_by_id["task-123"] = record

        result = await bot._fork_task_output(
            route=record.parent_route,
            args={"task_id": "task-123", "block": False, "timeout": 1},
        )

        assert "<retrieval_status>completed</retrieval_status>" in result["content"][0]["text"]
        assert "<status>completed</status>" in result["content"][0]["text"]
        assert "<output>" in result["content"][0]["text"]
        assert "READY" in result["content"][0]["text"]
        assert result["tool_use_result"]["retrieval_status"] == "completed"
        assert result["tool_use_result"]["task"]["status"] == "completed"
        assert result["tool_use_result"]["task"]["result"] == "READY"

    async def test_fork_task_output_blocking_timeout_returns_timeout(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
        )
        bot._fork_tasks_by_id["task-123"] = record
        task = asyncio.create_task(asyncio.sleep(30))
        bot._fork_task_tasks["task-123"] = task
        try:
            result = await bot._fork_task_output(
                route=record.parent_route,
                args={"task_id": "task-123", "block": True, "timeout": 1},
            )
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert "<retrieval_status>timeout</retrieval_status>" in result["content"][0]["text"]
        assert result["tool_use_result"]["retrieval_status"] == "timeout"

    async def test_fork_task_output_completed_is_stable_across_repeated_calls(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="completed",
            result_text="READY",
        )
        bot._fork_tasks_by_id["task-123"] = record

        first = await bot._fork_task_output(
            route=record.parent_route,
            args={"task_id": "task-123", "block": False, "timeout": 1},
        )
        second = await bot._fork_task_output(
            route=record.parent_route,
            args={"task_id": "task-123", "block": False, "timeout": 1},
        )

        assert first["content"][0]["text"] == second["content"][0]["text"]
        assert first["tool_use_result"] == second["tool_use_result"]

    async def test_fork_task_output_terminal_request_stopped_reports_stopped(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="launched",
            terminal_request="stopped",
            result_text="PARTIAL",
        )
        bot._fork_tasks_by_id["task-123"] = record

        result = await bot._fork_task_output(
            route=record.parent_route,
            args={"task_id": "task-123", "block": False, "timeout": 1},
        )

        assert "<retrieval_status>stopped</retrieval_status>" in result["content"][0]["text"]
        assert "<status>stopped</status>" in result["content"][0]["text"]
        assert result["tool_use_result"]["retrieval_status"] == "stopped"
        assert result["tool_use_result"]["task"]["status"] == "stopped"

    async def test_fork_task_output_includes_output_file_when_available(self, config, tmp_path):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="completed",
            result_text="READY",
        )
        bot._fork_tasks_by_id["task-123"] = record
        output_path = tmp_path / "sid-child.jsonl"
        output_path.write_text('{"type":"assistant"}\n', encoding="utf-8")

        with patch("obs_agent.telegram.find_session_jsonl", return_value=output_path):
            result = await bot._fork_task_output(
                route=record.parent_route,
                args={"task_id": "task-123", "block": False, "timeout": 1},
            )

        assert f"<output_file>{output_path}</output_file>" in result["content"][0]["text"]
        assert result["tool_use_result"]["task"]["output_file"] == str(output_path)

    async def test_fork_task_output_on_stopped_handle_returns_stopped(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="stopped",
        )
        bot._fork_tasks_by_id["task-123"] = record

        result = await bot._fork_task_output(
            route=record.parent_route,
            args={"task_id": "task-123", "block": False, "timeout": 1},
        )

        assert "<retrieval_status>stopped</retrieval_status>" in result["content"][0]["text"]
        assert "<status>stopped</status>" in result["content"][0]["text"]
        assert result["tool_use_result"]["retrieval_status"] == "stopped"
        assert result["tool_use_result"]["task"]["status"] == "stopped"

    async def test_fork_task_output_unknown_handle_returns_not_found(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        result = await bot._fork_task_output(
            route=TelegramRoute(chat_id=-10067890, thread_id=None),
            args={"task_id": "bogus-task", "block": False, "timeout": 1},
        )

        assert result["is_error"] is True
        assert "No task found with ID: bogus-task" in result["content"][0]["text"]

    async def test_fork_task_stop_interrupts_running_child(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Child")
        assert child_state is not None
        fake_client = MagicMock()
        fake_client.interrupt = AsyncMock()
        with patch.object(child_state.session_manager, "get_client", AsyncMock(return_value=fake_client)):
            task = asyncio.create_task(asyncio.sleep(30))
            record = _ForkTaskRecord(
                task_id="task-123",
                parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_route,
                child_session_id="sid-child",
                prompt="Return READY",
                description="Audit",
            )
            bot._fork_tasks_by_id["task-123"] = record
            bot._fork_task_tasks["task-123"] = task
            try:
                result = await bot._fork_task_stop(
                    route=record.parent_route,
                    args={"task_id": "task-123"},
                )
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        assert record.terminal_request == "stopped"
        assert child_state.hook_state.interrupt_flag is True
        fake_client.interrupt.assert_awaited_once()
        assert "Successfully stopped task: task-123" in result["content"][0]["text"]

    async def test_fork_task_stop_on_completed_handle_returns_not_found(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="completed",
        )
        bot._fork_tasks_by_id["task-123"] = record

        result = await bot._fork_task_stop(
            route=record.parent_route,
            args={"task_id": "task-123"},
        )

        assert result["is_error"] is True
        assert "No task found with ID: task-123" in result["content"][0]["text"]

    async def test_fork_task_stop_on_idle_ready_team_worker_stops_successfully(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
            status="completed",
            is_fork=False,
            team_name="team-alpha",
            agent_name="worker-a",
            idle_ready=True,
        )
        bot._fork_tasks_by_id["task-123"] = record
        bot._fork_task_by_child_route[child_route] = "task-123"
        bot._team_worker_records[("team-alpha", "worker-a")] = "task-123"

        result = await bot._fork_task_stop(
            route=record.parent_route,
            args={"task_id": "task-123"},
        )

        assert result.get("is_error") is not True
        assert record.status == "stopped"
        assert record.terminal_request == "stopped"
        assert record.idle_ready is False
        assert child_route not in bot._fork_task_by_child_route
        assert ("team-alpha", "worker-a") not in bot._team_worker_records
        assert "Successfully stopped task: task-123" in result["content"][0]["text"]

    async def test_fork_task_stop_repeated_call_returns_not_found(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Child")
        assert child_state is not None
        fake_client = MagicMock()
        fake_client.interrupt = AsyncMock()
        with patch.object(child_state.session_manager, "get_client", AsyncMock(return_value=fake_client)):
            task = asyncio.create_task(asyncio.sleep(30))
            record = _ForkTaskRecord(
                task_id="task-123",
                parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_route,
                child_session_id="sid-child",
                prompt="Return READY",
                description="Audit",
            )
            bot._fork_tasks_by_id["task-123"] = record
            bot._fork_task_tasks["task-123"] = task
            try:
                await bot._fork_task_stop(route=record.parent_route, args={"task_id": "task-123"})
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        second = await bot._fork_task_stop(route=record.parent_route, args={"task_id": "task-123"})
        assert second["is_error"] is True
        assert "No task found with ID: task-123" in second["content"][0]["text"]

    async def test_fork_task_stop_repeated_same_turn_reports_not_running(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_state = bot._get_state(child_route, topic_title="Child")
        assert child_state is not None
        fake_client = MagicMock()
        fake_client.interrupt = AsyncMock()
        with patch.object(child_state.session_manager, "get_client", AsyncMock(return_value=fake_client)):
            task = asyncio.create_task(asyncio.sleep(30))
            record = _ForkTaskRecord(
                task_id="task-123",
                parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_route,
                child_session_id="sid-child",
                prompt="Return READY",
                description="Audit",
            )
            bot._fork_tasks_by_id["task-123"] = record
            bot._fork_task_tasks["task-123"] = task
            try:
                first = await bot._fork_task_stop(
                    route=record.parent_route,
                    args={"task_id": "task-123"},
                )
                second = await bot._fork_task_stop(
                    route=record.parent_route,
                    args={"task_id": "task-123"},
                )
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        assert first.get("is_error") is not True
        assert second["is_error"] is True
        assert "<tool_use_error>Task task-123 is not running (status: killed)</tool_use_error>" in second["content"][0]["text"]
        assert second["tool_use_result"] == "Error: Task task-123 is not running (status: killed)"

    async def test_fork_task_stop_only_interrupts_target_handle(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_a_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        child_b_route = TelegramRoute(chat_id=-10067890, thread_id=322)
        child_a_state = bot._get_state(child_a_route, topic_title="Child A")
        child_b_state = bot._get_state(child_b_route, topic_title="Child B")
        assert child_a_state is not None
        assert child_b_state is not None
        fake_client_a = MagicMock()
        fake_client_a.interrupt = AsyncMock()
        fake_client_b = MagicMock()
        fake_client_b.interrupt = AsyncMock()
        with (
            patch.object(child_a_state.session_manager, "get_client", AsyncMock(return_value=fake_client_a)),
            patch.object(child_b_state.session_manager, "get_client", AsyncMock(return_value=fake_client_b)),
        ):
            task_a = asyncio.create_task(asyncio.sleep(30))
            task_b = asyncio.create_task(asyncio.sleep(30))
            record_a = _ForkTaskRecord(
                task_id="task-a",
                parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_a_route,
                child_session_id="sid-child-a",
                prompt="Return A",
                description="Audit A",
            )
            record_b = _ForkTaskRecord(
                task_id="task-b",
                parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
                parent_session_id_at_launch="sid-parent",
                parent_source_uuid="parent-source-uuid",
                child_route=child_b_route,
                child_session_id="sid-child-b",
                prompt="Return B",
                description="Audit B",
            )
            bot._fork_tasks_by_id["task-a"] = record_a
            bot._fork_tasks_by_id["task-b"] = record_b
            bot._fork_task_tasks["task-a"] = task_a
            bot._fork_task_tasks["task-b"] = task_b
            try:
                await bot._fork_task_stop(
                    route=record_a.parent_route,
                    args={"task_id": "task-a"},
                )
            finally:
                task_a.cancel()
                task_b.cancel()
                with suppress(asyncio.CancelledError):
                    await task_a
                with suppress(asyncio.CancelledError):
                    await task_b

        assert child_a_state.hook_state.interrupt_flag is True
        assert child_b_state.hook_state.interrupt_flag is False
        fake_client_a.interrupt.assert_awaited_once()
        fake_client_b.interrupt.assert_not_awaited()

    async def test_fork_task_notification_includes_tool_use_id_and_usage(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=-10067890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=TelegramRoute(chat_id=-10067890, thread_id=321),
            child_session_id="sid-child",
            prompt="Return READY",
            description="Audit",
            tool_use_id="toolu_abc123",
            result_text="READY",
            status="completed",
            usage_total_tokens=42,
            usage_tool_uses=3,
            usage_duration_ms=900,
        )

        xml = bot._build_fork_task_callback_payload(record)

        assert "<tool-use-id>toolu_abc123</tool-use-id>" in xml
        assert "<total_tokens>42</total_tokens>" in xml
        assert "<tool_uses>3</tool_uses>" in xml
        assert "<duration_ms>900</duration_ms>" in xml

    async def test_resume_fork_task_reuses_existing_child_route(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Audit")
        assert parent_state is not None
        assert child_state is not None
        fake_bot = MagicMock()
        child_resume = MagicMock()
        child_resume.message_id = 920
        parent_resume = MagicMock()
        parent_resume.message_id = 921
        fake_bot.send_message = AsyncMock(side_effect=[child_resume, parent_resume])
        parent_state.last_bot = fake_bot
        child_state.last_bot = fake_bot
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="OLD",
            description="Old",
            status="completed",
            is_fork=False,
            launch_tool_name="AgentTask",
        )
        bot._fork_tasks_by_id["task-123"] = record
        with patch.object(bot, "_execute_fork_task", new_callable=AsyncMock):
            result = await bot._launch_fork_task(
                route=parent_route,
                args={
                    "prompt": "NEW",
                    "resume": "task-123",
                    "description": "New",
                    "team_name": "team-resume",
                    "agent_name": "worker-resume",
                    "task_tool_name": "AgentTask",
                },
            )

        assert record.child_route == child_route
        assert record.prompt == "NEW"
        assert record.description == "New"
        assert record.team_name == "team-resume"
        assert record.agent_name == "worker-resume"
        assert record.task_id in parent_state.active_fork_task_ids
        assert "agentId: task-123" in result["content"][0]["text"]
        assert "AgentTask launched successfully." in result["content"][0]["text"]
        child_env = child_state.session_manager.create_options().env
        assert child_env["CLAUDE_CODE_ENABLE_TASKS"] == "1"
        assert child_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
        assert child_env["CLAUDE_CODE_TASK_LIST_ID"] == "team-resume"
        assert child_env["CLAUDE_CODE_TEAM_NAME"] == "team-resume"
        assert child_env["CLAUDE_CODE_AGENT_NAME"] == "worker-resume"
        send_calls = fake_bot.send_message.await_args_list
        assert "agent task launched by agent" in send_calls[0].kwargs["text"]
        assert "team_name: team-resume" in send_calls[0].kwargs["text"]
        assert "agent_name: worker-resume" in send_calls[0].kwargs["text"]

    async def test_resume_fork_task_running_handle_returns_error(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=-10067890, thread_id=None)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
        parent_state = bot._get_state(parent_route)
        child_state = bot._get_state(child_route, topic_title="General - Audit")
        assert parent_state is not None
        assert child_state is not None
        parent_state.last_bot = MagicMock()
        child_state.last_bot = MagicMock()
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="OLD",
            description="Old",
            status="launched",
        )
        bot._fork_tasks_by_id["task-123"] = record
        task = asyncio.create_task(asyncio.sleep(30))
        bot._fork_task_tasks["task-123"] = task
        try:
            result = await bot._launch_fork_task(
                route=parent_route,
                args={"prompt": "NEW", "resume": "task-123", "description": "New"},
            )
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert result["is_error"] is True
        assert "already running" in result["content"][0]["text"]

    async def test_resume_fork_task_missing_handle_returns_not_found(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.last_bot = MagicMock()
        state.session_manager.set_session_id("sid-parent")

        result = await bot._launch_fork_task(
            route=route,
            args={"prompt": "NEW", "resume": "bogus-task"},
        )

        assert result["is_error"] is True
        assert "No task found with ID: bogus-task" in result["content"][0]["text"]

    def test_normalize_resume_task_id_treats_false_like_missing(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        assert bot._normalize_resume_task_id(None) is None
        assert bot._normalize_resume_task_id("") is None
        assert bot._normalize_resume_task_id("   ") is None
        assert bot._normalize_resume_task_id("false") is None
        assert bot._normalize_resume_task_id("NULL") is None
        assert bot._normalize_resume_task_id("task-123") == "task-123"

    def test_coerce_timeout_ms_uses_config_default_floor(self, config):
        config.bg_fork_timeout = 600.0
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        assert bot._coerce_timeout_ms(None) is None
        assert bot._coerce_timeout_ms(120_000) == 120_000
        assert bot._coerce_timeout_ms("300000") == 300_000
        assert bot._coerce_timeout_ms(900_000) == 900_000

    def test_coerce_timeout_ms_respects_custom_default_floor(self, config):
        config.bg_fork_timeout = 300.0
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        assert bot._coerce_timeout_ms(None) is None
        assert bot._coerce_timeout_ms(120_000) == 120_000
        assert bot._coerce_timeout_ms(450_000) == 450_000

    async def test_resolve_fork_source_falls_back_to_latest_persisted_uuid(self, config, tmp_path):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.session_manager.set_session_id("sid-root")
        bot._session_heads["sid-root"] = "volatile-uuid"
        bot._record_message_binding(
            route=route,
            message_id=55,
            jsonl_uuid="persisted-uuid",
            session_id="sid-root",
            role="assistant",
        )
        jsonl_path = tmp_path / "sid-root.jsonl"
        jsonl_path.write_text(
            '{"uuid":"persisted-uuid","parentUuid":null}\n',
            encoding="utf-8",
        )

        with patch("obs_agent.telegram.find_session_jsonl", return_value=jsonl_path):
            session_id, source_uuid, source_route, source_message_id = bot._resolve_fork_source(
                state=state
            )

        assert session_id == "sid-root"
        assert source_uuid == "persisted-uuid"
        assert source_route == route
        assert source_message_id == 55

    async def test_stop_clear_and_delete_mark_child_task_terminal_state(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=321)
        child_state = bot._get_state(child_route)
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-child")

        record = _ForkTaskRecord(
            task_id="task-123",
            parent_route=TelegramRoute(chat_id=67890, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-source-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="Return READY",
        )
        bot._fork_tasks_by_id["task-123"] = record
        bot._fork_task_by_child_route[child_route] = "task-123"

        stop_update = _make_update("/stop", thread_id=321)
        stop_ctx = _make_context()
        await bot.handle_stop(stop_update, stop_ctx)
        assert record.terminal_request == "stopped"

        record.terminal_request = None
        clear_update = _make_update("/clear", thread_id=321)
        clear_ctx = _make_context()
        with patch.object(child_state.session_manager, "async_reset", new_callable=AsyncMock):
            await bot.handle_clear(clear_update, clear_ctx)
        assert record.terminal_request == "failed"

        record.terminal_request = None
        delete_update = _make_update("/delete", thread_id=321)
        delete_ctx = _make_context()
        delete_ctx.bot.delete_forum_topic = AsyncMock(return_value=True)
        await bot.handle_delete(delete_update, delete_ctx)
        assert record.terminal_request == "failed"

    async def test_active_fork_tasks_still_emit_completion_summary(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.active_fork_task_ids.add("task-123")

        events = [TextEvent(text="done"), TurnEndEvent(jsonl_uuid="assistant-uuid"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                state.session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("hi")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        assert ctx.bot.send_message.call_count == 4
        assert bot._should_emit_completion_summary(state) is True


class TestTelegramTransport:
    async def test_observability_turns_are_coalesced_before_text_turn(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_chat_action = AsyncMock()
        sent_messages = []

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = 100 + len(sent_messages)
            sent_messages.append(kwargs)
            return message

        fake_bot.send_message = AsyncMock(side_effect=send_side_effect)
        state.last_bot = fake_bot
        await bot._flush_turn(
            state=state,
            bot=fake_bot,
            turn_items=[StatusEvent(type="thinking", summary="thinking a")],
        )
        await bot._flush_turn(
            state=state,
            bot=fake_bot,
            turn_items=[StatusEvent(type="tool_use", summary="Read: file.md")],
        )
        assert fake_bot.send_message.await_count == 0
        await bot._flush_turn(
            state=state,
            bot=fake_bot,
            turn_items=[TextEvent(text="final answer")],
        )
        assert fake_bot.send_message.await_count == 2
        assert "thinking a" in sent_messages[0]["text"]
        assert "Read: file.md" in sent_messages[0]["text"]
        assert "final answer" in sent_messages[1]["text"]
        await bot.shutdown()

    async def test_notification_turn_is_not_coalesced_as_observability(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        assert state is not None
        fake_bot = MagicMock()
        fake_bot.send_chat_action = AsyncMock()
        sent_messages = []

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = 700 + len(sent_messages)
            sent_messages.append(kwargs)
            return message

        fake_bot.send_message = AsyncMock(side_effect=send_side_effect)
        state.last_bot = fake_bot
        await bot._flush_turn(
            state=state,
            bot=fake_bot,
            turn_items=[
                StatusEvent(
                    type="notification",
                    summary="notification: task_started",
                    messages=["task_id: task-123"],
                )
            ],
        )
        assert fake_bot.send_message.await_count == 1
        assert "notification: task_started" in sent_messages[0]["text"]
        assert "<i>task_id: task-123</i>" in sent_messages[0]["text"]
        assert state.route not in bot._observability_buffer
        await bot.shutdown()

    async def test_typing_indicator_task_clears_after_transport_drain(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        fake_bot = MagicMock()
        fake_bot.send_chat_action = AsyncMock()
        sent = MagicMock()
        sent.message_id = 777
        fake_bot.send_message = AsyncMock(return_value=sent)

        await bot._send_system_message(
            route=route,
            bot=fake_bot,
            text="working",
            disable_notification=True,
        )
        for _ in range(20):
            if route.chat_id not in bot._chat_pending_ops and route.chat_id not in bot._typing_tasks:
                break
            await asyncio.sleep(0.05)
        assert route.chat_id not in bot._chat_pending_ops
        assert route.chat_id not in bot._typing_tasks
        await bot.shutdown()


class TestCreateTelegramApp:
    def test_raises_without_token(self, config):
        config.telegram_bot_token = None
        config.telegram_bot_tokens = []
        with pytest.raises(ValueError, match="OBS_TELEGRAM_BOT_TOKEN"):
            create_telegram_app(config)

    def test_raises_without_allowed_users(self, config):
        config.telegram_bot_token = "fake-token"
        config.telegram_allowed_user_ids = []
        with pytest.raises(ValueError, match="OBS_TELEGRAM_ALLOWED_USERS"):
            create_telegram_app(config)

    def test_creates_app_with_token_and_users(self, config):
        config.telegram_bot_token = "fake-token"
        config.telegram_allowed_user_ids = [12345]
        app = create_telegram_app(config)
        assert app is not None

    def test_creates_app_with_token_list_and_users(self, config):
        config.telegram_bot_token = None
        config.telegram_bot_tokens = ["fake-token-a", "fake-token-b"]
        config.telegram_allowed_user_ids = [12345]
        app = create_telegram_app(config)
        assert app is not None

    def test_registers_new_group_and_new_bot_handlers(self, config):
        config.telegram_bot_token = "fake-token"
        config.telegram_allowed_user_ids = [12345]
        app = create_telegram_app(config)
        handlers = app.handlers[0]

        command_map = {
            next(iter(handler.commands)): handler.callback.__name__
            for handler in handlers
            if getattr(handler, "commands", None)
        }
        assert command_map["new_group"] == "handle_new_group"
        assert command_map["new_bot"] == "handle_new_bot"

        callback_names = [getattr(getattr(handler, "callback", None), "__name__", None) for handler in handlers]
        assert "handle_new_group_alias" in callback_names
        assert "handle_new_bot_alias" in callback_names


class TestTelegramCommandRegistration:
    async def test_set_bot_commands_registers_userbot_provisioning_commands(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()

        await _set_bot_commands(app)

        commands = app.bot.set_my_commands.await_args.args[0]
        names = [command.command for command in commands]
        assert "new_group" in names
        assert "new_bot" in names

    async def test_clear_secondary_bot_commands_only_targets_non_primary_tokens(self, config):
        config.telegram_bot_token = "primary-token"
        config.telegram_bot_tokens = ["primary-token", "secondary-a", "secondary-b"]

        fake_secondary_a = MagicMock()
        fake_secondary_a.delete_my_commands = AsyncMock()
        fake_secondary_b = MagicMock()
        fake_secondary_b.delete_my_commands = AsyncMock()

        with patch("obs_agent.telegram.Bot", side_effect=[fake_secondary_a, fake_secondary_b]) as bot_cls:
            await _clear_secondary_bot_commands(config)

        created_tokens = [call.kwargs["token"] for call in bot_cls.call_args_list]
        assert created_tokens == ["secondary-a", "secondary-b"]
        fake_secondary_a.delete_my_commands.assert_awaited_once()
        fake_secondary_b.delete_my_commands.assert_awaited_once()


class TestTelegramProvisioningCommands:
    def test_parse_new_group_request_defaults_to_sender_target(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        target_override, title = bot._parse_new_group_request("Provisioned Group")

        assert target_override is None
        assert title == "Provisioned Group"

    def test_parse_new_group_request_supports_handle_override(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        target_override, title = bot._parse_new_group_request("--user @breedooon Provisioned Group")

        assert target_override == "@breedooon"
        assert title == "Provisioned Group"

    def test_parse_new_group_request_supports_numeric_override(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        target_override, title = bot._parse_new_group_request("--user 5129431382 Provisioned Group")

        assert target_override == "5129431382"
        assert title == "Provisioned Group"

    def test_extract_command_args_supports_addressed_new_group_alias(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new-group@obsTopicTestBot Provisioned Group", thread_id=None)
        ctx = _make_context()

        raw_args = bot._extract_command_args(
            update,
            ctx,
            command_names=("new-group", "new_group"),
        )

        assert raw_args == "Provisioned Group"

    async def test_new_command_redirects_new_bot_dash_arg_alias(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new", thread_id=None)
        ctx = _make_context()
        ctx.args = ["-bot"]

        with patch.object(bot, "handle_new_bot_alias", new=AsyncMock()) as alias_mock:
            await bot.handle_new(update, ctx)

        alias_mock.assert_awaited_once_with(update, ctx)

    async def test_new_command_redirects_new_group_dash_arg_alias(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new", thread_id=None)
        ctx = _make_context()
        ctx.args = ["-group", "Test", "Group"]

        with patch.object(bot, "handle_new_group_alias", new=AsyncMock()) as alias_mock:
            await bot.handle_new(update, ctx)

        alias_mock.assert_awaited_once_with(update, ctx)

    async def test_new_command_redirects_new_bot_hyphen_alias(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new-bot", thread_id=None)
        ctx = _make_context()
        ctx.args = ["-bot"]

        with patch.object(bot, "handle_new_bot_alias", new=AsyncMock()) as alias_mock:
            await bot.handle_new(update, ctx)

        alias_mock.assert_awaited_once_with(update, ctx)

    async def test_new_command_redirects_new_group_hyphen_alias(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new-group Test Group", thread_id=None)
        ctx = _make_context()
        ctx.args = ["-group", "Test", "Group"]

        with patch.object(bot, "handle_new_group_alias", new=AsyncMock()) as alias_mock:
            await bot.handle_new(update, ctx)

        alias_mock.assert_awaited_once_with(update, ctx)

    async def test_new_group_delegates_to_userbot_helper(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new-group Provisioned Group", thread_id=None)
        ctx = _make_context()

        with (
            patch.object(
                bot,
                "_provision_new_group",
                new=AsyncMock(return_value="created group: Provisioned Group"),
            ) as provision_mock,
            patch.object(bot, "_send_system_message", new=AsyncMock()) as send_mock,
        ):
            await bot.handle_new_group_alias(update, ctx)

        provision_mock.assert_awaited_once_with(
            update=update,
            context=ctx,
            raw_args="Provisioned Group",
        )
        assert send_mock.await_count == 2
        started_text = send_mock.await_args_list[0].kwargs["text"]
        completed_text = send_mock.await_args_list[1].kwargs["text"]
        assert "new group started" in started_text
        assert "created group: Provisioned Group" in completed_text

    async def test_new_bot_delegates_to_userbot_helper(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("/new-bot", thread_id=None)
        ctx = _make_context()

        with (
            patch.object(
                bot,
                "_provision_new_bot",
                new=AsyncMock(return_value="created bot: ClaudiaObsTest123Bot"),
            ) as provision_mock,
            patch.object(bot, "_send_system_message", new=AsyncMock()) as send_mock,
        ):
            await bot.handle_new_bot_alias(update, ctx)

        provision_mock.assert_awaited_once_with(
            update=update,
            context=ctx,
            raw_args=None,
        )
        assert send_mock.await_count == 2
        started_text = send_mock.await_args_list[0].kwargs["text"]
        completed_text = send_mock.await_args_list[1].kwargs["text"]
        assert "new bot started" in started_text
        assert "created bot: ClaudiaObsTest123Bot" in completed_text

    async def test_new_chat_members_finalizes_joined_allowed_user(self, config):
        config.telegram_allowed_user_ids = [12345]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("", user_id=999, chat_id=-100777)
        update.effective_message.new_chat_members = [SimpleNamespace(id=12345, username="breedooon")]
        ctx = _make_context()
        finalize_mock = AsyncMock(return_value=True)

        with patch.object(bot, "_maybe_finalize_joined_allowed_user", new=finalize_mock):
            await bot.handle_new_chat_members(update, ctx)

        finalize_mock.assert_awaited_once_with(
            chat_id=-100777,
            joined_user_id=12345,
            joined_username="breedooon",
        )

    async def test_new_chat_members_ignores_non_allowed_members(self, config):
        config.telegram_allowed_user_ids = [12345]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("", user_id=999, chat_id=-100777)
        update.effective_message.new_chat_members = [SimpleNamespace(id=67890, username="someoneelse")]
        ctx = _make_context()
        finalize_mock = AsyncMock()

        with patch.object(bot, "_maybe_finalize_joined_allowed_user", new=finalize_mock):
            await bot.handle_new_chat_members(update, ctx)

        finalize_mock.assert_not_called()

    async def test_handle_message_finalizes_joined_allowed_user_for_group_message(self, config):
        config.telegram_allowed_user_ids = [12345]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        update = _make_update("hello", user_id=12345, chat_id=-100777)
        update.effective_user.username = "breedoon"
        ctx = _make_context()

        with (
            patch.object(bot, "_ensure_background_poller", new=AsyncMock()) as ensure_poller,
            patch.object(bot, "_maybe_finalize_joined_allowed_user", new=AsyncMock()) as finalize_mock,
            patch.object(bot._fragment_buffer, "add", new=AsyncMock()) as fragment_add,
        ):
            await bot.handle_message(update, ctx)

        ensure_poller.assert_awaited_once_with(ctx.bot)
        finalize_mock.assert_awaited_once_with(
            chat_id=-100777,
            joined_user_id=12345,
            joined_username="breedoon",
        )
        fragment_add.assert_awaited_once_with(update, ctx)


class TestTelegramSenderSelection:
    def test_private_chat_uses_primary_bot_only(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        primary = object()
        secondary_a = object()
        secondary_b = object()
        bot._sender_bots = [secondary_a, secondary_b]

        candidates = bot._sender_candidates(
            fallback_bot=primary,
            chat_id=5129431382,
        )

        assert candidates == [primary]


class TestRunTelegramBotSupervisor:
    async def test_restarts_after_runtime_error(self, config):
        run_once = AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()])
        sleep_mock = AsyncMock()

        with patch("obs_agent.telegram._run_telegram_bot_once", run_once), patch(
            "obs_agent.telegram.asyncio.sleep", sleep_mock
        ):
            with pytest.raises(asyncio.CancelledError):
                await run_telegram_bot(config)

        assert run_once.await_count == 2
        assert sleep_mock.await_count == 1

    async def test_fatal_error_is_not_retried(self, config):
        run_once = AsyncMock(side_effect=ValueError("bad config"))
        sleep_mock = AsyncMock()

        with patch("obs_agent.telegram._run_telegram_bot_once", run_once), patch(
            "obs_agent.telegram.asyncio.sleep", sleep_mock
        ):
            with pytest.raises(ValueError, match="bad config"):
                await run_telegram_bot(config)

        assert run_once.await_count == 1
        sleep_mock.assert_not_awaited()


class TestFragmentBuffer:
    async def test_single_message_flushed(self):
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        update = _make_update("hello", message_id=100)
        ctx = _make_context()
        await buf.add(update, ctx)

        assert received == ["hello"]

    async def test_consecutive_fragments_reassembled(self):
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("a" * 4096, message_id=100)
        u2 = _make_update("b" * 4096, message_id=101)
        u3 = _make_update("c" * 200, message_id=102)

        t1 = asyncio.create_task(buf.add(u1, ctx))
        await asyncio.sleep(0)
        await buf.add(u2, ctx)
        await buf.add(u3, ctx)
        await t1

        assert received == [("a" * 4096) + ("b" * 4096) + ("c" * 200)]

    async def test_quick_short_messages_are_batched_together(self):
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("first", user_id=111, message_id=100)
        u2 = _make_update("second", user_id=111, message_id=101)

        await asyncio.gather(buf.add(u1, ctx), buf.add(u2, ctx))
        assert received == ["first\n\nsecond"]

    async def test_message_after_gap_starts_new_batch(self):
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("first", user_id=111, message_id=100)
        u2 = _make_update("second", user_id=111, message_id=101)

        await buf.add(u1, ctx)
        await asyncio.sleep(_TEST_GAP * 1.5)
        await buf.add(u2, ctx)

        assert received == ["first", "second"]


# --- Error handling ---


class TestTelegramErrorHandling:
    """Verify that Telegram API errors don't nuke the SDK session."""

    async def test_telegram_badrequest_handled_without_session_reset(self, config):
        """BadRequest 'too long' is handled by _send_html fallback, not by crashing."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        events = [
            TextEvent(text="x" * 5000),  # oversized
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            state = _state(bot)
            with patch.object(
                state.session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                state.session_manager, "async_reset", new_callable=AsyncMock
            ) as mock_full:
                update = _make_update("test")
                ctx = _make_context()

                # First call: raise "too long", subsequent calls: succeed
                call_count = 0

                async def send_side_effect(**kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise BadRequest("Message is too long")

                ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)

                await bot.handle_message(update, ctx)

                # Neither session reset method should have been called
                mock_soft.assert_not_called()
                mock_full.assert_not_called()

    async def test_telegram_error_retried_no_reset(self, config):
        """Transient Telegram errors are retried and do not reset the SDK session."""
        from telegram.error import TelegramError as TgError

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        events = [
            TextEvent(text="hello"),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            state = _state(bot)
            with patch.object(
                state.session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                state.session_manager, "async_reset", new_callable=AsyncMock
            ) as mock_full:
                update = _make_update("test")
                ctx = _make_context()

                # Fail the first send, then succeed. Retry path should recover.
                call_count = 0

                async def send_side_effect(**kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise TgError("network error")

                ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)

                with patch("obs_agent.telegram.asyncio.sleep", new_callable=AsyncMock):
                    await bot.handle_message(update, ctx)

                # No session resets — only Telegram is broken, not SDK
                mock_soft.assert_not_called()
                mock_full.assert_not_called()

    async def test_sdk_error_triggers_soft_reset(self, config):
        """SDK errors (not Telegram) should trigger soft_reset."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                raise Exception("JSON message exceeded maximum buffer size")
                yield  # noqa: unreachable

            instance.run = mock_run

            with patch.object(_state(bot).session_manager, "soft_reset", new_callable=AsyncMock) as mock_soft:
                update = _make_update("test")
                ctx = _make_context()
                await bot.handle_message(update, ctx)

                mock_soft.assert_called_once()

    async def test_process_error_preserves_session(self, config):
        """ProcessError should trigger soft_reset (session preserved), NOT async_reset."""
        from claude_agent_sdk import ProcessError

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                raise ProcessError("CLI died", exit_code=1)
                yield  # noqa: unreachable

            instance.run = mock_run

            state = _state(bot)
            with patch.object(
                state.session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                state.session_manager, "async_reset", new_callable=AsyncMock
            ) as mock_full:
                update = _make_update("test")
                ctx = _make_context()
                await bot.handle_message(update, ctx)

                # soft_reset called, async_reset NOT called
                mock_soft.assert_called_once()
                mock_full.assert_not_called()

                # Error message stays in the shared system-message format.
                sent_calls = ctx.bot.send_message.call_args_list
                error_texts = [
                    c.kwargs.get("text", "") for c in sent_calls
                    if "error" in c.kwargs.get("text", "").lower()
                ]
                assert any("<u><i>error: ProcessError: CLI died" in t for t in error_texts)

    async def test_transient_transport_error_does_not_truncate_run(self, config):
        """A mid-stream Telegram send failure should retry and finish the turn."""
        from telegram.error import TelegramError as TgError

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        events = [
            TextEvent(text="chunk-1"),
            TurnEndEvent(),
            TextEvent(text="chunk-2"),
            TurnEndEvent(),
            TextEvent(text="FINAL_MARKER"),
            TurnEndEvent(),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            call_count = 0

            async def send_side_effect(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 4:
                    raise TgError("temporary transport failure")

            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)

            with patch("obs_agent.telegram.asyncio.sleep", new_callable=AsyncMock):
                await bot.handle_message(update, ctx)

            texts = [c.kwargs.get("text", "") for c in ctx.bot.send_message.call_args_list]
            assert any("FINAL_MARKER" in t for t in texts)
            assert texts[-1] == "<u><i>context: 0 / 1m</i></u>"


class TestTelegramStatePersistence:
    async def test_initialize_runtime_restores_route_session_and_message_mappings(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route_general = TelegramRoute(chat_id=67890, thread_id=None)
        route_topic = TelegramRoute(chat_id=67890, thread_id=321)
        route_other_chat = TelegramRoute(chat_id=67991, thread_id=None)

        state_general = bot._get_state(route_general)
        state_topic = bot._get_state(route_topic, topic_title="General - Worker")
        assert state_general is not None
        assert state_topic is not None

        state_general.topic_title = "General"
        state_general.session_manager.set_session_id("sid-general")
        bot._bind_state_session(state_general)
        bot._set_session_head(session_id="sid-general", jsonl_uuid="uuid-general")
        bot._record_message_binding(
            route=route_general,
            message_id=55,
            jsonl_uuid="uuid-general",
            session_id="sid-general",
            role="assistant",
        )
        bot._remember_system_message_id(route=route_general, message_id=56)
        bot._last_inbound_message_id_by_route[route_general] = 57
        bot._persist_state_for_route(route_general)

        state_topic.topic_title = "General - Worker"
        state_topic.topic_icon_custom_emoji_id = "emoji-1"
        state_topic.child_fork_count = 2
        state_topic.child_fork_base_title = "General - Worker"
        state_topic.notify_on_completion = False
        state_topic.agent_lineage = ("General", "Worker")
        state_topic.pending_obs_bootstrap = (
            "<obs-bootstrap version='1'><obs-lineage>"
            "<obs-node name='General' /><obs-node name='Worker' />"
            "</obs-lineage><fork_context><origin>agent_task_fresh</origin>"
            "<is_fork>false</is_fork><session_id>sid-topic</session_id></fork_context>"
            "</obs-bootstrap>"
        )
        state_topic.session_manager.set_session_id("sid-topic")
        bot._bind_state_session(state_topic)
        bot._set_session_head(session_id="sid-topic", jsonl_uuid="uuid-topic")
        bot._record_message_binding(
            route=route_topic,
            message_id=88,
            jsonl_uuid="uuid-topic",
            session_id="sid-topic",
            role="assistant",
        )
        bot._last_inbound_message_id_by_route[route_topic] = 90
        bot._persist_state_for_route(route_topic)

        state_other = bot._get_state(route_other_chat)
        assert state_other is not None
        state_other.session_manager.set_session_id("sid-other")
        bot._bind_state_session(state_other)
        bot._set_session_head(session_id="sid-other", jsonl_uuid="uuid-other")
        bot._record_message_binding(
            route=route_other_chat,
            message_id=12,
            jsonl_uuid="uuid-other",
            session_id="sid-other",
            role="assistant",
        )
        bot._last_inbound_message_id_by_route[route_other_chat] = 12
        bot._persist_state_for_route(route_other_chat)
        await bot.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()

        restored_general = restored._get_state(route_general, create=False)
        restored_topic = restored._get_state(route_topic, create=False)
        restored_other = restored._get_state(route_other_chat, create=False)
        assert restored_general is not None
        assert restored_topic is not None
        assert restored_other is not None
        assert restored_general.session_id == "sid-general"
        assert restored_topic.session_id == "sid-topic"
        assert restored_topic.topic_title == "General - Worker"
        assert restored_topic.topic_icon_custom_emoji_id == "emoji-1"
        assert restored_topic.child_fork_count == 2
        assert restored_topic.notify_on_completion is True
        assert restored_topic.agent_lineage == ("General", "Worker")
        assert restored_topic.pending_obs_bootstrap is not None
        assert "<obs-bootstrap" in restored_topic.pending_obs_bootstrap
        assert restored._route_by_session_id["sid-general"] == route_general
        assert restored._route_by_session_id["sid-topic"] == route_topic
        assert restored._route_by_session_id["sid-other"] == route_other_chat
        assert restored._session_heads["sid-general"] == "uuid-general"
        assert restored._session_heads["sid-topic"] == "uuid-topic"
        assert restored._session_heads["sid-other"] == "uuid-other"
        restored_topic_env = restored_topic.session_manager.sdk_env_overrides
        assert restored_topic_env["CLAUDE_CODE_TEAM_NAME"] == root_team_key_for_lineage(("General", "Worker"))
        assert restored_topic_env["CLAUDE_CODE_TASK_LIST_ID"] == root_team_key_for_lineage(("General", "Worker"))
        assert restored_topic_env["CLAUDE_CODE_AGENT_NAME"] == agent_name_for_lineage(("General", "Worker"))
        assert restored._message_map[(67890, 55)].jsonl_uuid == "uuid-general"
        assert restored._message_map[(67890, 88)].jsonl_uuid == "uuid-topic"
        assert restored._message_map[(67991, 12)].jsonl_uuid == "uuid-other"
        assert (67890, 56) in restored._system_message_ids
        assert restored._last_inbound_message_id_by_route[route_general] == 57
        assert restored._last_inbound_message_id_by_route[route_topic] == 90
        assert restored._last_inbound_message_id_by_route[route_other_chat] == 12
        assert restored._resolve_route_inbox_target(
            team_name=root_team_key_for_lineage(("General", "Worker")),
            agent_name=agent_name_for_lineage(("General", "Worker")),
        ) is restored_topic
        await restored.shutdown()

    async def test_initialize_runtime_restores_trunk_agent_name_from_persisted_team_key(self, config):
        route = TelegramRoute(chat_id=67890, thread_id=None)
        team_key = "2026-03-30-10-10-general"

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._state_store.upsert_route_state(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            session_id="sid-root",
            topic_title="General",
            topic_icon_custom_emoji_id=None,
            child_fork_count=0,
            child_fork_base_title=None,
            notify_on_completion=False,
            last_inbound_message_id=None,
            agent_lineage=("General",),
            pending_obs_bootstrap=None,
        )
        await bot.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        original_build_session_state = restored._build_session_state

        def _patched_build_session_state(*args, **kwargs):
            state = original_build_session_state(*args, **kwargs)
            state.session_manager.set_sdk_env_overrides(
                restored._build_team_worker_env(team_name=team_key)
            )
            return state

        with patch.object(restored, "_build_session_state", side_effect=_patched_build_session_state):
            await restored.initialize_runtime()

        restored_state = restored._get_state(route, create=False)
        assert restored_state is not None
        restored_env = restored_state.session_manager.sdk_env_overrides
        assert restored_env["CLAUDE_CODE_TEAM_NAME"] == team_key
        assert restored_env["CLAUDE_CODE_AGENT_NAME"] == team_key
        await restored.shutdown()

    async def test_pre_restart_message_remains_forkable(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        state = bot._get_state(route)
        assert state is not None
        state.session_manager.set_session_id("sid-root")
        bot._bind_state_session(state)
        bot._set_session_head(session_id="sid-root", jsonl_uuid="uuid-latest")
        bot._record_message_binding(
            route=route,
            message_id=42,
            jsonl_uuid="uuid-older",
            session_id="sid-root",
            role="assistant",
        )
        await bot.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()
        restored_state = restored._get_state(route)
        assert restored_state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()
        trigger = QueuedMessage(text="follow up", telegram_message_id=100, reply_to_message_id=42)

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-forked") as mock_fork:
            proceed, reply_to_user_message_id = await restored._resolve_session_for_trigger(
                state=restored_state,
                trigger_message=trigger,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 100
        assert restored_state.session_id == "sid-forked"
        assert mock_fork.call_args.kwargs["target_uuid"] == "uuid-older"
        await restored.shutdown()

    async def test_unclean_previous_exit_does_not_make_chat_unusable(self, config):
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=None)
        state = first._get_state(route)
        assert state is not None
        state.session_manager.set_session_id("sid-root")
        first._bind_state_session(state)
        first._set_session_head(session_id="sid-root", jsonl_uuid="uuid-head")
        first._last_inbound_message_id_by_route[route] = 10
        first._persist_state_for_route(route)

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()
        ctx = _make_context()

        old_update = _make_update("old", message_id=9)
        with patch.object(restored, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await restored._process_message("old", old_update, ctx)
            mock_run.assert_not_called()

        fresh_update = _make_update("fresh", message_id=11)
        with patch.object(restored, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await restored._process_message("fresh", fresh_update, ctx)
            assert mock_run.await_count == 1

        await restored.shutdown()
        await first.shutdown()

    async def test_idle_team_worker_is_restored_and_wakeable_after_restart(self, config):
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=321)
        child_state = first._get_state(child_route, topic_title="General - Team Worker")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-team-child")
        first._bind_state_session(child_state)
        first._set_session_head(session_id="sid-team-child", jsonl_uuid="uuid-team-child")
        first._persist_state_for_route(child_route)

        record = _ForkTaskRecord(
            task_id="task-team-1",
            parent_route=child_route,
            parent_session_id_at_launch="sid-team-child",
            parent_source_uuid="uuid-team-child",
            child_route=child_route,
            child_session_id="sid-team-child",
            prompt="",
            description="Team Worker",
            status="completed",
            is_fork=False,
            team_name="team-alpha",
            agent_name="worker-a",
            idle_ready=True,
            emit_parent_callback=False,
        )
        first._fork_tasks_by_id[record.task_id] = record
        first._register_team_worker_record(record)
        first._fork_task_by_child_route[child_route] = record.task_id
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()

        restored_record = restored._fork_tasks_by_id.get("task-team-1")
        assert restored_record is not None
        assert restored_record.idle_ready is True
        assert restored._team_worker_records[("team-alpha", "worker-a")] == "task-team-1"
        assert restored._fork_task_by_child_route[child_route] == "task-team-1"

        restored_child_state = restored._get_state(child_route)
        assert restored_child_state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=991))
        restored_child_state.last_bot = fake_bot

        with patch.object(restored, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await restored._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=67890, thread_id=444),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process after restart",
                },
            )

        schedule_mock.assert_awaited_once_with(
            task_id="task-team-1",
            parent_state=restored_child_state,
        )
        await restored.shutdown()

    async def test_restored_team_worker_wake_uses_primary_bot_when_route_has_no_last_bot(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=6541)
        child_state = bot._get_state(child_route, topic_title="General - Restarted Worker")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-restarted-child")
        bot._bind_state_session(child_state)
        bot._set_session_head(session_id="sid-restarted-child", jsonl_uuid="uuid-restarted-child")

        record = _ForkTaskRecord(
            task_id="task-team-primary-bot",
            parent_route=child_route,
            parent_session_id_at_launch="sid-restarted-child",
            parent_source_uuid="uuid-restarted-child",
            child_route=child_route,
            child_session_id="sid-restarted-child",
            prompt="",
            description="Restarted Worker",
            status="completed",
            is_fork=False,
            team_name="team-alpha",
            agent_name="worker-a",
            idle_ready=True,
            emit_parent_callback=False,
        )
        bot._fork_tasks_by_id[record.task_id] = record
        bot._register_team_worker_record(record)
        bot._fork_task_by_child_route[child_route] = record.task_id

        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=1991))
        bot._primary_bot = fake_bot
        child_state.last_bot = None

        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=67890, thread_id=444),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process after restart",
                    "_direct_send": True,
                },
            )

        assert child_state.last_bot is fake_bot
        schedule_mock.assert_awaited_once_with(
            task_id="task-team-primary-bot",
            parent_state=child_state,
        )
        await bot.shutdown()

    async def test_idle_fork_team_worker_is_restored_and_wakeable_after_restart(self, config):
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=654)
        child_state = first._get_state(child_route, topic_title="General - Fork Worker")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-fork-child")
        first._bind_state_session(child_state)
        first._set_session_head(session_id="sid-fork-child", jsonl_uuid="uuid-fork-child")
        first._persist_state_for_route(child_route)

        record = _ForkTaskRecord(
            task_id="task-fork-team-1",
            parent_route=TelegramRoute(chat_id=67890, thread_id=321),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="uuid-parent",
            child_route=child_route,
            child_session_id="sid-fork-child",
            prompt="",
            description="Fork Worker",
            status="completed",
            is_fork=True,
            team_name="team-alpha",
            agent_name="worker-fork",
            idle_ready=True,
            emit_parent_callback=False,
        )
        first._fork_tasks_by_id[record.task_id] = record
        first._register_team_worker_record(record)
        first._fork_task_by_child_route[child_route] = record.task_id
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()

        restored_record = restored._fork_tasks_by_id.get("task-fork-team-1")
        assert restored_record is not None
        assert restored_record.idle_ready is True
        assert restored._team_worker_records[("team-alpha", "worker-fork")] == "task-fork-team-1"
        assert restored._fork_task_by_child_route[child_route] == "task-fork-team-1"

        restored_child_state = restored._get_state(child_route)
        assert restored_child_state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=992))
        restored_child_state.last_bot = fake_bot

        with patch.object(restored, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            await restored._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=67890, thread_id=444),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "worker-fork",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process fork after restart",
                },
            )

        schedule_mock.assert_awaited_once_with(
            task_id="task-fork-team-1",
            parent_state=restored_child_state,
        )
        await restored.shutdown()

    async def test_idle_lineage_route_is_restored_and_wakeable_after_restart(self, config):
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=67890, thread_id=655)
        child_state = first._get_state(child_route, topic_title="General - Fork Child")
        assert child_state is not None
        child_state.session_manager.set_session_id("sid-route-lineage")
        first._bind_state_session(child_state)
        first._set_session_head(session_id="sid-route-lineage", jsonl_uuid="uuid-route-lineage")
        first._prime_obs_bootstrap(
            child_state,
            lineage=("General", "Fork Child"),
            origin="user_fork",
            is_fork=True,
            session_id="sid-route-lineage",
        )
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()

        restored_child_state = restored._get_state(child_route)
        assert restored_child_state is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=993))
        restored_child_state.last_bot = fake_bot
        team_name = root_team_key_for_lineage(("General", "Fork Child"))
        agent_name = agent_name_for_lineage(("General", "Fork Child"))

        with patch.object(
            restored,
            "_run_and_send",
            AsyncMock(return_value=_RunOutcome(assistant_text="RESTORED-ROUTE-WAKE-OK")),
        ) as run_mock:
            await restored._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=67890, thread_id=444),
                payload={
                    "team_name": team_name,
                    "recipient": agent_name,
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process restored route wake",
                },
            )

        run_mock.assert_awaited_once()
        assert restored._resolve_route_inbox_target(
            team_name=team_name,
            agent_name=agent_name,
        ) is restored_child_state
        await restored.shutdown()

    async def test_state_inbox_projection_prefers_persisted_env_identity_when_bootstrap_is_missing(
        self,
        config,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=656)
        state = bot._get_state(route, topic_title="General - Restored Route")
        assert state is not None
        state.agent_lineage = ("Smoke Restart Wake", "Grandchild")
        state.pending_obs_bootstrap = None
        state.session_manager.set_sdk_env_overrides(
            bot._build_team_worker_env(
                team_name="2026-03-31-05-03-smoke-restart-wake",
                agent_name="abcdef1234-grandchild",
            )
        )

        assert bot._state_inbox_projection(state) == (
            "2026-03-31-05-03-smoke-restart-wake",
            "abcdef1234-grandchild",
        )
        await bot.shutdown()

    async def test_state_inbox_projection_uses_session_bootstrap_identity_when_env_is_missing(
        self,
        config,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=657)
        state = bot._get_state(route, topic_title="General - Restored Route")
        assert state is not None
        state.agent_lineage = ("Smoke Restart Wake", "Grandchild")
        state.pending_obs_bootstrap = None
        state.session_manager.set_session_id("sid-bootstrap-route")

        with patch(
            "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
            return_value=SimpleNamespace(
                root_team_key="2026-03-31-05-03-smoke-restart-wake",
                agent_name="abcdef1234-grandchild",
                lineage=("Smoke Restart Wake", "Grandchild"),
            ),
        ):
            assert bot._state_inbox_projection(state) == (
                "2026-03-31-05-03-smoke-restart-wake",
                "abcdef1234-grandchild",
            )
        await bot.shutdown()

    async def test_state_inbox_projection_uses_session_bootstrap_when_state_lineage_is_missing(
        self,
        config,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=658)
        state = bot._get_state(route, topic_title="General - Rebind Route")
        assert state is not None
        state.agent_lineage = None
        state.pending_obs_bootstrap = None
        state.session_manager.set_session_id("sid-bootstrap-only")

        with patch(
            "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
            return_value=SimpleNamespace(
                root_team_key="2026-03-31-05-03-smoke-restart-wake",
                agent_name="abcdef1234-grandchild",
                lineage=("Smoke Restart Wake", "Grandchild"),
            ),
        ):
            assert bot._state_inbox_projection(state) == (
                "2026-03-31-05-03-smoke-restart-wake",
                "abcdef1234-grandchild",
            )
        await bot.shutdown()

    async def test_bind_state_session_rebinds_route_target_from_session_bootstrap_identity(
        self,
        config,
    ):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=659)
        state = bot._get_state(route, topic_title="General - Rebind Route")
        assert state is not None
        state.agent_lineage = None
        state.pending_obs_bootstrap = None
        state.session_manager.set_session_id("sid-rebind-bootstrap")

        with patch(
            "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
            return_value=SimpleNamespace(
                root_team_key="2026-03-31-05-03-smoke-restart-wake",
                agent_name="2026-03-31-05-03-smoke-restart-wake",
                lineage=("Smoke Restart Wake",),
            ),
        ):
            bot._bind_state_session(state)

        assert (
            bot._resolve_route_inbox_target(
                team_name="2026-03-31-05-03-smoke-restart-wake",
                agent_name="2026-03-31-05-03-smoke-restart-wake",
            )
            is state
        )
        await bot.shutdown()

    async def test_initialize_runtime_restores_route_identity_from_session_bootstrap_when_lineage_missing(
        self,
        config,
    ):
        route = TelegramRoute(chat_id=67890, thread_id=660)
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        first._state_store.upsert_route_state(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            session_id="sid-missing-lineage",
            topic_title="General - Rebind Route",
            topic_icon_custom_emoji_id=None,
            child_fork_count=0,
            child_fork_base_title=None,
            notify_on_completion=False,
            last_inbound_message_id=None,
            agent_lineage=None,
            pending_obs_bootstrap=None,
        )
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        with patch(
            "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
            return_value=SimpleNamespace(
                root_team_key="2026-03-31-05-03-smoke-restart-wake",
                agent_name="2026-03-31-05-03-smoke-restart-wake",
                lineage=("Smoke Restart Wake",),
            ),
        ):
            await restored.initialize_runtime()

        restored_state = restored._get_state(route, create=False)
        assert restored_state is not None
        assert restored_state.agent_lineage == ("Smoke Restart Wake",)
        assert (
            restored._resolve_route_inbox_target(
                team_name="2026-03-31-05-03-smoke-restart-wake",
                agent_name="2026-03-31-05-03-smoke-restart-wake",
            )
            is restored_state
        )
        restored_env = restored_state.session_manager.sdk_env_overrides
        assert restored_env["CLAUDE_CODE_TEAM_NAME"] == "2026-03-31-05-03-smoke-restart-wake"
        assert restored_env["CLAUDE_CODE_AGENT_NAME"] == "2026-03-31-05-03-smoke-restart-wake"
        await restored.shutdown()

    async def test_initialize_runtime_restores_route_without_lineage_or_bootstrap_by_falling_back_to_title(
        self,
        config,
    ):
        route = TelegramRoute(chat_id=67890, thread_id=661)
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        first._state_store.upsert_route_state(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            session_id="sid-title-fallback",
            topic_title="General - Title Fallback",
            topic_icon_custom_emoji_id=None,
            child_fork_count=0,
            child_fork_base_title=None,
            notify_on_completion=False,
            last_inbound_message_id=None,
            agent_lineage=None,
            pending_obs_bootstrap=None,
        )
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        with patch(
            "obs_agent.telegram.find_latest_obs_bootstrap_for_session",
            return_value=None,
        ):
            await restored.initialize_runtime()

        restored_state = restored._get_state(route, create=False)
        assert restored_state is not None
        expected_lineage = ("General - Title Fallback",)
        expected_team_key = root_team_key_for_lineage(expected_lineage)
        assert restored_state.agent_lineage == expected_lineage
        restored_env = restored_state.session_manager.sdk_env_overrides
        assert restored_env["CLAUDE_CODE_TEAM_NAME"] == expected_team_key
        assert restored_env["CLAUDE_CODE_AGENT_NAME"] == expected_team_key
        assert (
            restored._resolve_route_inbox_target(
                team_name=expected_team_key,
                agent_name=expected_team_key,
            )
            is restored_state
        )
        await restored.shutdown()

    async def test_direct_inbox_notification_marks_poll_dedup_key(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=-10067890, thread_id=222)
        state = bot._get_state(route, topic_title="Busy Root")
        assert state is not None
        bot._route_inbox_targets[("team-alpha", "root-agent")] = route
        bot._route_inbox_target_keys_by_route[route] = ("team-alpha", "root-agent")

        with patch.object(
            bot,
            "_maybe_wake_route_inbox_target",
            new_callable=AsyncMock,
            return_value=True,
        ) as wake_mock:
            await bot._handle_inbox_message_notification(
                sender_route=TelegramRoute(chat_id=-10067890, thread_id=555),
                payload={
                    "team_name": "team-alpha",
                    "recipient": "root-agent",
                    "sender": "worker-b",
                    "summary": "handoff",
                    "content": "process root item 3",
                    "_direct_send": True,
                },
            )

        wake_mock.assert_awaited_once()
        assert (
            "team-alpha",
            "root-agent",
            "worker-b:handoff:process root item 3",
        ) in bot._notified_inbox_keys
        await bot.shutdown()

    async def test_non_team_task_handle_is_restored_and_resumable_after_restart(self, config):
        first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent_route = TelegramRoute(chat_id=67890, thread_id=None)
        child_route = TelegramRoute(chat_id=67890, thread_id=654)
        parent_state = first._get_state(parent_route)
        child_state = first._get_state(child_route, topic_title="General - Worker")
        assert parent_state is not None
        assert child_state is not None
        parent_state.session_manager.set_session_id("sid-parent")
        child_state.session_manager.set_session_id("sid-child")
        first._bind_state_session(parent_state)
        first._bind_state_session(child_state)
        first._set_session_head(session_id="sid-parent", jsonl_uuid="uuid-parent")
        first._set_session_head(session_id="sid-child", jsonl_uuid="uuid-child")
        first._persist_state_for_route(parent_route)
        first._persist_state_for_route(child_route)

        record = _ForkTaskRecord(
            task_id="task-restart-1",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="uuid-parent",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="",
            description="Restartable worker",
            status="completed",
            is_fork=False,
            launch_tool_name="AgentTask",
            team_name=None,
            agent_name=None,
            idle_ready=False,
            emit_parent_callback=False,
        )
        first._fork_tasks_by_id[record.task_id] = record
        first._register_team_worker_record(record)
        await first.shutdown()

        restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        await restored.initialize_runtime()

        restored_record = restored._fork_tasks_by_id.get("task-restart-1")
        assert restored_record is not None
        assert restored_record.child_route == child_route
        assert restored_record.child_session_id == "sid-child"
        assert restored_record.status == "completed"

        restored_parent = restored._get_state(parent_route)
        assert restored_parent is not None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=931), MagicMock(message_id=932)])
        restored_parent.last_bot = fake_bot

        with patch.object(restored, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            launched = await restored._launch_fork_task(
                route=parent_route,
                args={
                    "prompt": "Resume and report READY",
                    "description": "Restart resume",
                    "fork": False,
                    "resume": "task-restart-1",
                    "task_tool_name": "AgentTask",
                },
            )

        assert "AgentTask launched successfully." in launched["content"][0]["text"]
        assert "agentId: task-restart-1" in launched["content"][0]["text"]
        schedule_mock.assert_awaited_once()
        await restored.shutdown()
