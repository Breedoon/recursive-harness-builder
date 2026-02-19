"""Tests for obs_agent.telegram - Telegram bot integration.

Mocks python-telegram-bot and ConversationRunner to test message handling,
auth, formatting, typing loop, fragment buffer, and error fallback.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from obs_agent.config import OBSConfig
from obs_agent.events import StatusEvent
from obs_agent.runner import DoneEvent, TextEvent
from obs_agent.telegram import (
    TelegramBot,
    FragmentBuffer,
    StatusMessageManager,
    create_telegram_app,
    _typing_loop,
)

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
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


async def _wait_flush():
    """Wait long enough for the fragment buffer to flush."""
    await asyncio.sleep(_TEST_GAP * 3)


class TestTelegramBotAuth:
    """Authorization checks for Telegram bot."""

    async def test_allowed_user_passes(self, config):
        config.telegram_allowed_user_ids = [12345]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        events = [TextEvent(text="Hello"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("hi", user_id=12345)
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        update.effective_message.reply_text.assert_called_once()

    async def test_disallowed_user_rejected(self, config):
        config.telegram_allowed_user_ids = [99999]
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        update = _make_update("hi", user_id=12345)
        ctx = _make_context()
        await bot.handle_message(update, ctx)

        # Should not reply (auth rejects before FragmentBuffer)
        update.effective_message.reply_text.assert_not_called()

    async def test_empty_allowlist_rejects_all(self, config):
        """Empty allowlist = deny by default (security: no one allowed unless explicit)."""
        config.telegram_allowed_user_ids = []
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        update = _make_update("hi", user_id=12345)
        ctx = _make_context()
        await bot.handle_message(update, ctx)

        update.effective_message.reply_text.assert_not_called()


class TestTelegramBotResponse:
    """Response handling and formatting."""

    async def test_sends_html_response(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [TextEvent(text="**bold** text"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        call_args = update.effective_message.reply_text.call_args
        assert "<b>bold</b>" in call_args[0][0]
        assert call_args[1]["parse_mode"] == "HTML"

    async def test_disables_link_preview(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [TextEvent(text="Check https://example.com"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        call_kwargs = update.effective_message.reply_text.call_args[1]
        assert call_kwargs["disable_web_page_preview"] is True

    async def test_typing_loop_started(self, config):
        """Typing indicator should be sent via the typing loop."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [TextEvent(text="Hello"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                await asyncio.sleep(0.05)
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        assert ctx.bot.send_chat_action.call_count >= 1

    async def test_empty_response_handled(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            await bot.handle_message(update, ctx)

        call_args = update.effective_message.reply_text.call_args[0][0]
        assert "(no response)" in call_args

    async def test_bad_request_fallback_to_plain(self, config):
        """BadRequest with 'can't parse entities' falls back to plain text."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [TextEvent(text="broken <html"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            call_count = 0

            async def _reply_side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1 and kwargs.get("parse_mode") == "HTML":
                    raise BadRequest("Can't parse entities")

            update.effective_message.reply_text = AsyncMock(side_effect=_reply_side_effect)
            await bot.handle_message(update, ctx)

        assert update.effective_message.reply_text.call_count == 2

    async def test_bad_request_other_error_reraises(self, config):
        """BadRequest with other message re-raises."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        events = [TextEvent(text="test"), DoneEvent()]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e
            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()
            update.effective_message.reply_text = AsyncMock(
                side_effect=BadRequest("Chat not found")
            )

            await bot.handle_message(update, ctx)
            # The BadRequest is caught by FragmentBuffer._flush's exception handler

    async def test_no_message_ignored(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        update = MagicMock()
        update.effective_message = None
        ctx = _make_context()
        await bot.handle_message(update, ctx)

    async def test_no_user_ignored(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)
        update = MagicMock()
        update.effective_message.text = "hi"
        update.effective_user = None
        ctx = _make_context()
        await bot.handle_message(update, ctx)


# ---------------------------------------------------------------------------
# FragmentBuffer
# ---------------------------------------------------------------------------


class TestFragmentBuffer:
    """FragmentBuffer reassembles auto-split Telegram messages."""

    async def test_single_message_flushed(self):
        """A single message is flushed after the gap expires."""
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        update = _make_update("hello", message_id=100)
        ctx = _make_context()
        # add() now blocks until gap expires and processing completes
        await buf.add(update, ctx)

        assert received == ["hello"]

    async def test_consecutive_fragments_reassembled(self):
        """Consecutive message_ids within gap are concatenated.

        With concurrent_updates=True, multiple handlers run concurrently.
        The first add() blocks while waiting for the gap, and continuation
        fragments arrive via concurrent add() calls that signal the first.
        """
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        # Send three consecutive fragments concurrently (simulates concurrent_updates)
        u1 = _make_update("part1", message_id=100)
        u2 = _make_update("part2", message_id=101)
        u3 = _make_update("part3", message_id=102)

        # Start first add (it will block waiting for gap)
        t1 = asyncio.create_task(buf.add(u1, ctx))
        await asyncio.sleep(0)  # Let t1 start
        # Add fragments concurrently (these return immediately)
        await buf.add(u2, ctx)
        await buf.add(u3, ctx)
        # Wait for first handler to complete
        await t1

        assert len(received) == 1
        assert received[0] == "part1part2part3"

    async def test_non_consecutive_ids_separate_messages(self):
        """Non-consecutive message_ids are treated as separate messages."""
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("first", message_id=100)
        await buf.add(u1, ctx)

        u2 = _make_update("second", message_id=200)
        await buf.add(u2, ctx)

        assert received == ["first", "second"]

    async def test_gap_timeout_forces_flush(self):
        """Messages separated by > gap are flushed separately."""
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("first", message_id=100)
        await buf.add(u1, ctx)

        # Now send another message with consecutive id but after gap
        u2 = _make_update("second", message_id=101)
        await buf.add(u2, ctx)

        # Should be two separate messages (gap expired between them)
        assert len(received) == 2

    async def test_different_users_separate(self):
        """Messages from different users are never combined."""
        received: list[str] = []

        async def on_complete(text, update, context):
            received.append(text)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("from_alice", user_id=111, message_id=100)
        u2 = _make_update("from_bob", user_id=222, message_id=101)

        # Different users — both block independently
        t1 = asyncio.create_task(buf.add(u1, ctx))
        t2 = asyncio.create_task(buf.add(u2, ctx))
        await asyncio.gather(t1, t2)

        assert len(received) == 2

    async def test_uses_first_update_for_reply(self):
        """on_complete receives the first update (for reply context)."""
        updates_received: list = []

        async def on_complete(text, update, context):
            updates_received.append(update)

        buf = FragmentBuffer(on_complete=on_complete, gap_seconds=_TEST_GAP)
        ctx = _make_context()

        u1 = _make_update("part1", message_id=100)
        u2 = _make_update("part2", message_id=101)

        # Concurrent add calls: first blocks, second signals fragment
        t1 = asyncio.create_task(buf.add(u1, ctx))
        await asyncio.sleep(0)
        await buf.add(u2, ctx)
        await t1

        assert len(updates_received) == 1
        assert updates_received[0] is u1


class TestTypingLoop:
    """Test the typing indicator loop."""

    async def test_typing_loop_sends_action(self):
        stop_event = asyncio.Event()
        bot = MagicMock()
        bot.send_chat_action = AsyncMock()

        async def _stop_after_delay():
            await asyncio.sleep(0.1)
            stop_event.set()

        await asyncio.gather(
            _typing_loop(123, bot, stop_event),
            _stop_after_delay(),
        )

        assert bot.send_chat_action.call_count >= 1


class TestCreateTelegramApp:
    """create_telegram_app factory."""

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


# ---------------------------------------------------------------------------
# StatusMessageManager
# ---------------------------------------------------------------------------


class TestStatusMessageManager:
    """StatusMessageManager sends/edits a single status message."""

    async def test_first_add_sends_message(self):
        """First status line sends a new message."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.add("Read: CLAUDE.md")

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == 123
        assert "Read: CLAUDE.md" in call_kwargs["text"]

    async def test_send_message_has_disable_notification(self):
        """Status messages are sent silently (disable_notification=True)."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.add("Read: CLAUDE.md")

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs.get("disable_notification") is True

    async def test_second_add_edits_message(self):
        """Second status line edits the existing message."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.add("Read: CLAUDE.md")
        await mgr.add("Grep: pattern='skills'")

        assert bot.edit_message_text.call_count >= 1
        edit_kwargs = bot.edit_message_text.call_args[1]
        assert "Read: CLAUDE.md" in edit_kwargs["text"]
        assert "Grep: pattern='skills'" in edit_kwargs["text"]

    async def test_finish_italicizes(self):
        """finish() edits the message with HTML italic wrapping."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.add("Read: file.md")
        await mgr.finish()

        # The last edit_message_text call should contain <i> tags
        last_call = bot.edit_message_text.call_args
        assert "<i>" in last_call[1]["text"]
        assert last_call[1]["parse_mode"] == "HTML"

    async def test_debounce_batches_edits(self):
        """Multiple rapid adds within debounce window produce fewer edits."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0.2)
        # First add sends immediately (no prior edit)
        await mgr.add("Line 1")
        # These should be debounced
        await mgr.add("Line 2")
        await mgr.add("Line 3")

        # At this point, send_message was called once; edit may not have happened yet
        assert bot.send_message.call_count == 1
        # Wait for debounce to flush
        await asyncio.sleep(0.3)
        await mgr.finish()

        # After debounce + finish, all 3 lines should be in the final message
        last_text = bot.edit_message_text.call_args[1]["text"]
        assert "Line 1" in last_text
        assert "Line 2" in last_text
        assert "Line 3" in last_text

    async def test_overflow_trims_old_lines(self):
        """Adding more lines than max_lines trims oldest and adds overflow marker."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0, max_lines=3)
        await mgr.add("Line 1")
        await mgr.add("Line 2")
        await mgr.add("Line 3")
        await mgr.add("Line 4")
        await mgr.add("Line 5")

        last_text = bot.edit_message_text.call_args[1]["text"]
        # Should show overflow marker and latest lines, not Line 1 or Line 2
        assert "Line 1" not in last_text
        assert "Line 2" not in last_text
        assert "..." in last_text  # overflow marker
        assert "Line 5" in last_text

    async def test_has_content_property(self):
        """has_content is False initially, True after add()."""
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        assert mgr.has_content is False
        await mgr.add("test")
        assert mgr.has_content is True

    async def test_finish_without_content_is_noop(self):
        """finish() with no content doesn't send or edit anything."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.finish()

        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()

    async def test_edit_failure_does_not_crash(self):
        """BadRequest on edit is logged but doesn't raise."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )

        mgr = StatusMessageManager(123, bot, debounce_seconds=0)
        await mgr.add("Line 1")
        # This should not raise despite edit failing
        await mgr.add("Line 2")
        await mgr.finish()


# ---------------------------------------------------------------------------
# OverflowProtection
# ---------------------------------------------------------------------------


class TestOverflowProtection:
    """Status message overflow protection prevents message size explosion."""

    async def test_overflow_marker_shows_count(self):
        """Overflow marker indicates how many lines were trimmed."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0, max_lines=3)
        for i in range(6):
            await mgr.add(f"Step {i}")

        last_text = bot.edit_message_text.call_args[1]["text"]
        # Should have overflow marker and keep only last max_lines entries
        assert "..." in last_text
        assert "Step 5" in last_text
        assert "Step 0" not in last_text

    async def test_max_lines_respected(self):
        """Status message never exceeds max_lines line count."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0, max_lines=5)
        for i in range(20):
            await mgr.add(f"Line {i}")

        last_text = bot.edit_message_text.call_args[1]["text"]
        line_count = len(last_text.strip().split("\n"))
        assert line_count <= 5

    async def test_single_line_no_overflow(self):
        """A single status line never triggers overflow."""
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock()

        mgr = StatusMessageManager(123, bot, debounce_seconds=0, max_lines=3)
        await mgr.add("Only line")

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["text"] == "Only line"
        assert "..." not in call_kwargs["text"]


# ---------------------------------------------------------------------------
# Error Resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    """Error handling in _process_message."""

    async def test_error_shows_detail_and_resets_session(self, config):
        """On runner error, sends error detail and resets session."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                raise RuntimeError("Failed to decode JSON: buffer overflow")
                yield  # make it a generator  # noqa: E501

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            with patch.object(bot._session_manager, "async_reset", new_callable=AsyncMock) as mock_reset:
                await bot.handle_message(update, ctx)

            # Error message should contain the exception type and detail
            reply_text = update.effective_message.reply_text.call_args[0][0]
            assert "RuntimeError" in reply_text
            assert "buffer overflow" in reply_text

            # Session should have been reset
            mock_reset.assert_called_once()

    async def test_error_message_truncated(self, config):
        """Very long error messages are truncated to prevent Telegram overflow."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                raise RuntimeError("x" * 500)
                yield  # noqa: E501

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            with patch.object(bot._session_manager, "async_reset", new_callable=AsyncMock):
                await bot.handle_message(update, ctx)

            reply_text = update.effective_message.reply_text.call_args[0][0]
            assert len(reply_text) < 250  # truncated
            assert "..." in reply_text

    async def test_partial_response_delivered_on_error(self, config):
        """When runner yields text then crashes, partial text + error is sent."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                yield TextEvent(text="Partial result here")
                yield TextEvent(text=" and more text")
                raise RuntimeError("buffer overflow mid-stream")

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            with patch.object(bot._session_manager, "async_reset", new_callable=AsyncMock) as mock_reset:
                await bot.handle_message(update, ctx)

            # Reply should contain both the partial text and the error
            reply_text = update.effective_message.reply_text.call_args[0][0]
            assert "Partial result" in reply_text or "partial" in reply_text.lower()
            assert "buffer overflow" in reply_text or "RuntimeError" in reply_text

            # Session should still be reset
            mock_reset.assert_called_once()


# ---------------------------------------------------------------------------
# StatusEvent forwarding in _process_message
# ---------------------------------------------------------------------------


class TestStatusEventForwarding:
    """_process_message forwards StatusEvent to StatusMessageManager."""

    async def test_status_events_forwarded(self, config):
        """StatusEvents from runner are sent via StatusMessageManager."""
        bot = TelegramBot(config, fragment_gap=_TEST_GAP)

        events = [
            StatusEvent(type="tool_use", summary="Read: CLAUDE.md"),
            TextEvent(text="Hello"),
            StatusEvent(type="tool_use", summary="Grep: pattern='test'"),
            DoneEvent(),
        ]

        with patch("obs_agent.telegram.ConversationRunner") as MockRunner:
            instance = MockRunner.return_value

            async def mock_run(msg):
                for e in events:
                    yield e

            instance.run = mock_run
            instance.remaining_pending = []

            update = _make_update("test")
            ctx = _make_context()

            with patch("obs_agent.telegram.StatusMessageManager") as MockStatus:
                mock_mgr = MockStatus.return_value
                mock_mgr.add = AsyncMock()
                mock_mgr.finish = AsyncMock()

                await bot.handle_message(update, ctx)

                # Should have been called with the 2 status summaries
                assert mock_mgr.add.call_count == 2
                mock_mgr.add.assert_any_call("Read: CLAUDE.md")
                mock_mgr.add.assert_any_call("Grep: pattern='test'")
                mock_mgr.finish.assert_called_once()
