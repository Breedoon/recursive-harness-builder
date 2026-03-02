"""Tests for obs_agent.telegram - Telegram bot integration.

Covers the simplified Telegram runtime:
- per-turn chronological message flushing
- inline status + text rendering
- final context-summary completion behavior
- per-chat lock serialization
- background queue auto-delivery poller
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from obs_agent.events import StatusEvent
from obs_agent.queueing import QueuedMessage
from obs_agent.runner import DoneEvent, TextEvent, TurnEndEvent
from obs_agent.telegram import (
    FragmentBuffer,
    TelegramBot,
    _TelegramMessageBinding,
    create_telegram_app,
)

# Near-zero gap for fast test execution (real default is 1.0s)
_TEST_GAP = 0.05


def _make_update(
    text: str, user_id: int = 12345, chat_id: int = 67890, message_id: int = 1
) -> MagicMock:
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_message.text = text
    update.effective_message.caption = None
    update.effective_message.chat_id = chat_id
    update.effective_message.message_id = message_id
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
    return ctx


class TestTelegramBotAuth:
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
                bot._session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        binding = bot._message_map[103]
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

        sent_ids = [101, 102, 103]

        async def send_side_effect(**kwargs):
            message = MagicMock()
            message.message_id = sent_ids.pop(0)
            return message

        with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
            instance = mock_runner.return_value

            async def mock_run(msg):
                bot._session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        binding = bot._message_map[103]
        assert binding.jsonl_uuid == "assistant-uuid"
        assert binding.role == "assistant"

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
                bot._session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        binding = bot._message_map[42]
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
                bot._session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        assert bot._message_map[101].jsonl_uuid == "user-uuid"
        assert bot._message_map[102].jsonl_uuid == "user-uuid"
        assert bot._message_map[104].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[104].role == "assistant"

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
                bot._session_manager.set_session_id("sid-1")
                for event in events:
                    yield event

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test", message_id=42)
            ctx = _make_context()
            ctx.bot.send_message = AsyncMock(side_effect=send_side_effect)
            await bot.handle_message(update, ctx)

        assert bot._message_map[101].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[101].role == "assistant"
        assert bot._message_map[102].jsonl_uuid == "assistant-uuid"
        assert bot._message_map[102].role == "assistant"

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
        assert bot._media_group_receipt_ids[(67890, 12345, "album-1")] == [901]


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
            assert bot._hook_state.message_queue.get_nowait() == QueuedMessage(
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

        bot._last_chat_id = 67890
        bot._last_bot = fake_ptb_bot
        bot._hook_state.message_queue.put_nowait("queued bg result")

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            await asyncio.sleep(0.06)
            await bot.shutdown()

            assert mock_run.called
            kwargs = mock_run.call_args.kwargs
            assert kwargs["chat_id"] == 67890
            assert kwargs["extra_pending"] == [QueuedMessage(text="queued bg result")]
            assert "queued updates arrived while idle" in kwargs["user_text"]

    async def test_run_and_send_preserves_pending_across_session_switch(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._pending_messages = [
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
                bot._pending_messages = []
                return True, 10

            with patch.object(
                bot,
                "_resolve_session_for_trigger",
                side_effect=fake_resolve_session_for_trigger,
            ):
                await bot._run_and_send(
                    user_text="(System: queued updates arrived while idle. Process and summarize them.)",
                    chat_id=67890,
                    bot=fake_bot,
                    trigger_message=bot._pending_messages[-1],
                )

        assert mock_runner.call_args.kwargs["pending_messages"] == [
            QueuedMessage(
                text="reply while busy",
                telegram_message_id=10,
                reply_to_message_id=5,
            )
        ]

    async def test_main_session_warning_mentions_username_once(self, config):
        config.telegram_notify_username = "breedoon"
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._session_manager.set_session_id("sid-main")
        bot._main_session_id = "sid-main"
        bot._main_session_started_at = 0.0

        with patch("obs_agent.telegram.time.time", return_value=(50 * 60) + 1), patch.object(
            bot, "_send_system_message", new_callable=AsyncMock
        ) as mock_send:
            await bot._maybe_send_main_session_warning(chat_id=67890, bot=MagicMock())
            await bot._maybe_send_main_session_warning(chat_id=67890, bot=MagicMock())

        mock_send.assert_awaited_once()
        assert mock_send.call_args.kwargs["text"] == "main session has been active for 50 minutes\n@breedoon"
        assert bot._main_session_warning_sent is True

    async def test_main_session_warning_does_not_fire_for_fork_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._main_session_id = "sid-main"
        bot._main_session_started_at = 0.0
        bot._session_manager.set_session_id("sid-fork")

        with patch("obs_agent.telegram.time.time", return_value=(50 * 60) + 1), patch.object(
            bot, "_send_system_message", new_callable=AsyncMock
        ) as mock_send:
            await bot._maybe_send_main_session_warning(chat_id=67890, bot=MagicMock())

        mock_send.assert_not_called()


class TestForkViaReply:
    async def test_reply_to_old_assistant_message_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._message_map[5] = _TelegramMessageBinding(
            jsonl_uuid="assistant-1",
            session_id="sid-root",
            role="assistant",
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
                trigger_message=trigger,
                chat_id=67890,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert bot._session_manager.session_id == "sid-fork"
        mock_fork.assert_called_once()

    async def test_reply_to_old_user_message_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._message_map[5] = _TelegramMessageBinding(
            jsonl_uuid="user-1",
            session_id="sid-root",
            role="user",
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
                trigger_message=trigger,
                chat_id=67890,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert bot._session_manager.session_id == "sid-fork"
        mock_fork.assert_called_once()

    async def test_reply_to_mapped_system_marker_forks_session(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._message_map[7] = _TelegramMessageBinding(
            jsonl_uuid="assistant-1",
            session_id="sid-root",
            role="assistant",
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
                trigger_message=trigger,
                chat_id=67890,
                bot=fake_bot,
            )

        assert proceed is True
        assert reply_to_user_message_id == 11
        assert bot._session_manager.session_id == "sid-fork"
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

        proceed, reply_to_user_message_id = await bot._resolve_session_for_trigger(
            trigger_message=trigger,
            chat_id=67890,
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

        bot._last_chat_id = 67890
        bot._last_bot = fake_ptb_bot
        bot._busy_chats.add(67890)
        bot._hook_state.message_queue.put_nowait("queued bg result")

        with patch.object(bot, "_run_and_send", new_callable=AsyncMock) as mock_run:
            await bot._ensure_background_poller(fake_ptb_bot)
            await asyncio.sleep(0.05)
            await bot.shutdown()

            mock_run.assert_not_called()
            assert not bot._hook_state.message_queue.empty()


class TestCommands:
    async def test_new_resets_session_state(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._pending_messages = ["x"]
        bot._hook_state.interrupt_flag = True

        update = _make_update("/new")
        ctx = _make_context()

        with patch.object(bot._session_manager, "async_reset", new_callable=AsyncMock) as mock_reset:
            await bot.handle_new(update, ctx)

        mock_reset.assert_called_once()
        assert bot._pending_messages == []
        assert bot._hook_state.interrupt_flag is False
        ctx.bot.send_message.assert_called_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>session cleared</i></u>"

    async def test_stop_sets_interrupt_flag(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        update = _make_update("/stop")
        ctx = _make_context()
        await bot.handle_stop(update, ctx)

        assert bot._hook_state.interrupt_flag is True
        ctx.bot.send_message.assert_called_once()
        assert ctx.bot.send_message.call_args.kwargs["text"] == "<u><i>interrupt sent</i></u>"


class TestCreateTelegramApp:
    def test_raises_without_token(self, config):
        config.telegram_bot_token = None
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

            with patch.object(
                bot._session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                bot._session_manager, "async_reset", new_callable=AsyncMock
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

            with patch.object(
                bot._session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                bot._session_manager, "async_reset", new_callable=AsyncMock
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

            with patch.object(bot._session_manager, "soft_reset", new_callable=AsyncMock) as mock_soft:
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

            with patch.object(
                bot._session_manager, "soft_reset", new_callable=AsyncMock
            ) as mock_soft, patch.object(
                bot._session_manager, "async_reset", new_callable=AsyncMock
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
