"""Tests for obs_agent.telegram - Telegram bot integration.

Covers the simplified Telegram runtime:
- per-turn chronological message flushing
- inline status + text rendering
- final (done) sentinel behavior
- per-chat lock serialization
- background queue auto-delivery poller
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.events import StatusEvent
from obs_agent.runner import DoneEvent, TextEvent, TurnEndEvent
from obs_agent.telegram import FragmentBuffer, TelegramBot, create_telegram_app

# Near-zero gap for fast test execution (real default is 1.5s)
_TEST_GAP = 0.05


def _make_update(
    text: str, user_id: int = 12345, chat_id: int = 67890, message_id: int = 1
) -> MagicMock:
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_message.text = text
    update.effective_message.chat_id = chat_id
    update.effective_message.message_id = message_id
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

        # One content message + one done sentinel
        assert ctx.bot.send_message.call_count == 2

    async def test_disallowed_user_rejected(self, config):
        config.telegram_allowed_user_ids = [99999]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        update = _make_update("hi", user_id=12345)
        ctx = _make_context()
        await bot.handle_message(update, ctx)

        ctx.bot.send_message.assert_not_called()


class TestTelegramMessageFlow:
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

        assert ctx.bot.send_message.call_count == 2

        first = ctx.bot.send_message.call_args_list[0].kwargs
        assert first["parse_mode"] == "HTML"
        assert first["disable_notification"] is True
        assert "<i>Read: CLAUDE.md</i>" in first["text"]
        assert "Hello from tool run" in first["text"]

        second = ctx.bot.send_message.call_args_list[1].kwargs
        assert second["text"] == "(done)"
        assert second["disable_notification"] is False

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

        assert ctx.bot.send_message.call_count == 3
        assert "turn one" in ctx.bot.send_message.call_args_list[0].kwargs["text"]
        assert "turn two" in ctx.bot.send_message.call_args_list[1].kwargs["text"]
        assert ctx.bot.send_message.call_args_list[2].kwargs["text"] == "(done)"


class TestPerChatLock:
    async def test_same_chat_processing_serialized(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        started: list[str] = []
        finished: list[str] = []

        async def fake_run_and_send(**kwargs):
            text = kwargs["user_text"]
            started.append(text)
            if text == "first":
                await asyncio.sleep(0.05)
            finished.append(text)

        with patch.object(bot, "_run_and_send", side_effect=fake_run_and_send):
            ctx = _make_context()
            u1 = _make_update("first", message_id=1)
            u2 = _make_update("second", message_id=2)

            t1 = asyncio.create_task(bot._process_message("first", u1, ctx))
            await asyncio.sleep(0)
            t2 = asyncio.create_task(bot._process_message("second", u2, ctx))
            await asyncio.gather(t1, t2)

        assert started == ["first", "second"]
        assert finished == ["first", "second"]


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
            assert kwargs["extra_pending"] == ["queued bg result"]
            assert "queued updates arrived while idle" in kwargs["user_text"]

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
        update.effective_message.reply_text.assert_called_once()

    async def test_stop_sets_interrupt_flag(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

        update = _make_update("/stop")
        ctx = _make_context()
        await bot.handle_stop(update, ctx)

        assert bot._hook_state.interrupt_flag is True
        update.effective_message.reply_text.assert_called_once()


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

        u1 = _make_update("part1", message_id=100)
        u2 = _make_update("part2", message_id=101)
        u3 = _make_update("part3", message_id=102)

        t1 = asyncio.create_task(buf.add(u1, ctx))
        await asyncio.sleep(0)
        await buf.add(u2, ctx)
        await buf.add(u3, ctx)
        await t1

        assert received == ["part1part2part3"]

    async def test_different_users_never_combined(self):
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("from_alice", user_id=111, message_id=100)
        u2 = _make_update("from_bob", user_id=222, message_id=101)

        await asyncio.gather(buf.add(u1, ctx), buf.add(u2, ctx))
        assert len(received) == 2
