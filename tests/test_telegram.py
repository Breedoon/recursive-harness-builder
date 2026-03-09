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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from obs_agent.events import StatusEvent
from obs_agent.queueing import QueuedMessage
from obs_agent.runner import DoneEvent, TextEvent, TurnEndEvent
from obs_agent.telegram import (
    FragmentBuffer,
    TelegramRoute,
    TelegramBot,
    _ForkTaskRecord,
    _RunOutcome,
    _TelegramMessageBinding,
    create_telegram_app,
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
        assert calls[3]["text"] == "<u><i>context: 0 / 200k</i></u>"
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
        assert calls[4] == "<u><i>context: 0 / 200k</i></u>"

    async def test_completion_summary_mentions_username_when_configured(self, config):
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
        assert calls[-1] == "<u><i>context: 0 / 200k\n@breedoon</i></u>"

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

    async def test_route_warning_mentions_username_once(self, config):
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

        mock_send.assert_awaited_once()
        assert mock_send.call_args.kwargs["text"] == "session has been idle for 50 minutes\n@breedoon"
        assert state.warning_sent is True

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
    async def test_clear_resets_route_state(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)
        state.pending_messages = [QueuedMessage(text="x")]
        state.hook_state.interrupt_flag = True

        update = _make_update("/clear")
        ctx = _make_context()

        with patch.object(state.session_manager, "async_reset", new_callable=AsyncMock) as mock_reset:
            await bot.handle_clear(update, ctx)

        mock_reset.assert_called_once()
        assert state.pending_messages == []
        assert state.hook_state.interrupt_flag is False
        ctx.bot.send_message.assert_called_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>session cleared</i></u>"

    async def test_stop_sets_interrupt_flag_and_pauses_auto_delivery(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        state = _state(bot)

        update = _make_update("/stop")
        ctx = _make_context()
        await bot.handle_stop(update, ctx)

        assert state.hook_state.interrupt_flag is True
        assert state.hook_state.pause_queue_delivery is True
        ctx.bot.send_message.assert_called_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent</i></u>"

    async def test_stop_all_sets_interrupt_flag_for_all_routes(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        general = _state(bot)
        topic = _state(bot, thread_id=321)

        update = _make_update("/stop all")
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_stop(update, ctx)

        assert general.hook_state.interrupt_flag is True
        assert topic.hook_state.interrupt_flag is True
        assert general.hook_state.pause_queue_delivery is True
        assert topic.hook_state.pause_queue_delivery is True
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent to all topics</i></u>"

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
            name="Focused topic",
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
        assert record.max_turns == 9
        assert task_id in state.active_fork_task_ids
        send_calls = state.last_bot.send_message.await_args_list
        assert "fork task launched by agent" in send_calls[0].kwargs["text"]
        assert "source message" in send_calls[0].kwargs["text"]
        assert "https://t.me/c/67890/55" in send_calls[0].kwargs["text"]
        assert "session forked, your new session id is sid-child" in send_calls[1].kwargs["text"]
        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=321))
        assert child_state is not None
        assert child_state.notify_on_completion is False
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

        fake_task_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        fake_child_sid = uuid.UUID("22222222-2222-2222-2222-222222222222")
        with patch("obs_agent.telegram.uuid.uuid4", side_effect=[fake_task_id, fake_child_sid]), patch(
            "obs_agent.telegram.fork_session_jsonl"
        ) as mock_fork, patch.object(bot, "_execute_fork_task", new_callable=AsyncMock):
            launched = await bot._launch_fork_task(
                route=route,
                args={
                    "prompt": "Start fresh and return READY-FRESH",
                    "description": "Fresh child",
                    "fork": False,
                    "team_name": "team-alpha",
                    "name": "worker-a",
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
        assert record.child_session_id == "22222222-2222-2222-2222-222222222222"
        assert record.launch_tool_name == "AgentTask"
        assert record.team_name == "team-alpha"
        assert record.agent_name == "worker-a"
        send_calls = state.last_bot.send_message.await_args_list
        assert "agent task launched by agent" in send_calls[0].kwargs["text"]
        assert "team_name: team-alpha" in send_calls[0].kwargs["text"]
        assert "agent_name: worker-a" in send_calls[0].kwargs["text"]
        assert "session launched, your new session id is 22222222-2222-2222-2222-222222222222" in send_calls[1].kwargs["text"]
        child_state = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=333))
        assert child_state is not None
        child_env = child_state.session_manager.create_options().env
        assert child_env["CLAUDE_CODE_ENABLE_TASKS"] == "1"
        assert child_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
        assert child_env["CLAUDE_CODE_TASK_LIST_ID"] == "team-alpha"
        assert child_env["CLAUDE_CODE_TEAM_NAME"] == "team-alpha"
        assert child_env["CLAUDE_CODE_AGENT_NAME"] == "worker-a"
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
        assert "worker-a" in member_names
        assert "worker-b" in member_names
        await bot.shutdown()

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

        rendered = "\n".join(call["text"] for call in sent_messages)
        assert "notification: agent task running" in rendered
        assert "notification: agent task idle" in rendered
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
        assert send_calls[0].kwargs["text"].startswith("<u><i>fork task completed")
        assert "open child completion" in send_calls[1].kwargs["text"]
        assert "https://t.me/c/67890/321/910" in send_calls[1].kwargs["text"]
        run_text = run_mock.await_args.kwargs["user_text"]
        assert "<team_context>" in run_text
        assert "<team_name>team-alpha</team_name>" in run_text
        assert "<agent_name>worker-a</agent_name>" in run_text
        assert "SendInboxMessage" in run_text
        assert "ReadInbox" in run_text
        fake_bot.edit_message_text.assert_awaited_once()
        assert fake_bot.edit_message_text.await_args.kwargs["text"].startswith("<u><i>fork task completed")
        assert "https://t.me/c/67890/911" in fake_bot.edit_message_text.await_args.kwargs["text"]
        assert child_route not in bot._fork_task_by_child_route

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

    async def test_inbox_message_sets_pending_wake_when_worker_is_running(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child_route = TelegramRoute(chat_id=-10067890, thread_id=321)
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

        assert record.wake_requested is True
        assert record.wake_source_sender == "worker-b"
        assert record.wake_source_summary == "handoff"
        assert record.wake_source_content == "process item 8"
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

    async def test_fork_task_output_reports_not_found_after_completion(self, config):
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

        assert result["is_error"] is True
        assert "<tool_use_error>No task found with ID: task-123</tool_use_error>" in result["content"][0]["text"]
        assert result["tool_use_result"] == "Error: No task found with ID: task-123"

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

    async def test_fork_task_output_on_stopped_handle_returns_not_found(self, config):
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

        assert result["is_error"] is True
        assert "No task found with ID: task-123" in result["content"][0]["text"]

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
                    "name": "worker-resume",
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

    async def test_active_fork_tasks_suppress_parent_completion_summary(self, config):
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

        assert ctx.bot.send_message.call_count == 3
        assert bot._should_emit_completion_summary(state) is False


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
            assert texts[-1] == "<u><i>context: 0 / 200k</i></u>"


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
        assert restored_topic.notify_on_completion is False
        assert restored._route_by_session_id["sid-general"] == route_general
        assert restored._route_by_session_id["sid-topic"] == route_topic
        assert restored._route_by_session_id["sid-other"] == route_other_chat
        assert restored._session_heads["sid-general"] == "uuid-general"
        assert restored._session_heads["sid-topic"] == "uuid-topic"
        assert restored._session_heads["sid-other"] == "uuid-other"
        assert restored._message_map[(67890, 55)].jsonl_uuid == "uuid-general"
        assert restored._message_map[(67890, 88)].jsonl_uuid == "uuid-topic"
        assert restored._message_map[(67991, 12)].jsonl_uuid == "uuid-other"
        assert (67890, 56) in restored._system_message_ids
        assert restored._last_inbound_message_id_by_route[route_general] == 57
        assert restored._last_inbound_message_id_by_route[route_topic] == 90
        assert restored._last_inbound_message_id_by_route[route_other_chat] == 12
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
