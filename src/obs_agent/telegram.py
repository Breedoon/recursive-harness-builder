"""Telegram bot for OBS Agent.

Receives messages from Telegram, processes them through ConversationRunner,
and sends back formatted HTML responses. Features:
- FragmentBuffer: reassembles user text auto-split by Telegram (>4096 chars)
- Typing indicator loop (re-sends every 4s since Telegram expires at 5s)
- HTML formatting with plain-text fallback on parse errors
- Link preview disabled (Claude outputs many URLs)
- Rate-limit-safe delay between split message chunks

Usage:
    from obs_agent.telegram import run_telegram_bot
    await run_telegram_bot(config)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent
from obs_agent.session import SessionManager
from obs_agent.telegram_format import md_to_telegram_html, split_message

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.telegram")

# Delay between sending split message chunks (Telegram rate limit: ~1 msg/sec/chat)
_CHUNK_DELAY_SECONDS = 1.0

# Typing indicator refresh interval (expires after 5s on Telegram)
_TYPING_INTERVAL_SECONDS = 4.0

# FragmentBuffer: max gap between consecutive message_ids to be considered fragments
_FRAGMENT_MAX_GAP_SECONDS = 1.5

# StatusMessageManager: minimum interval between edits (Telegram rate limit)
_STATUS_DEBOUNCE_SECONDS = 2.0

# Maximum number of status lines to show (prevents message overflow)
_STATUS_MAX_LINES = 15


async def _typing_loop(
    chat_id: int, bot, stop_event: asyncio.Event
) -> None:
    """Re-send typing indicator every 4 seconds until stopped."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_TYPING_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# FragmentBuffer: reassemble user messages auto-split by Telegram
# ---------------------------------------------------------------------------

@dataclass
class _PendingFragment:
    """A fragment of a split user message waiting for more parts."""
    chat_id: int
    user_id: int
    last_message_id: int
    parts: list[str] = field(default_factory=list)
    last_seen: float = 0.0
    update: Update | None = None  # The first update (for reply context)
    context: ContextTypes.DEFAULT_TYPE | None = None


class FragmentBuffer:
    """Buffers consecutive Telegram messages from the same user/chat.

    When a user pastes text longer than 4096 chars, Telegram auto-splits it
    into multiple updates with consecutive message_ids sent in rapid succession.
    This buffer collects fragments within a time window and concatenates them.

    IMPORTANT: add() blocks (awaits) until the message is fully processed.
    This keeps processing within the python-telegram-bot handler context,
    which is required for the SDK's anyio task groups to work correctly.
    Background asyncio.create_task detached from the handler context causes
    the SDK's ClaudeSDKClient.connect() to hang.

    Usage:
        buf = FragmentBuffer(on_complete=bot.process_complete_message)
        # In handler: await buf.add(update, context)
    """

    def __init__(
        self,
        on_complete,  # async callable(full_text, update, context)
        gap_seconds: float = _FRAGMENT_MAX_GAP_SECONDS,
    ) -> None:
        self._on_complete = on_complete
        self._gap = gap_seconds
        self._pending: dict[tuple[int, int], _PendingFragment] = {}  # (chat_id, user_id) -> fragment
        self._flush_events: dict[tuple[int, int], asyncio.Event] = {}

    async def add(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Add an incoming message. Blocks until the message is fully processed.

        If this is the first fragment, waits for the gap period then processes.
        If this is a continuation fragment, resets the gap timer on the existing
        pending entry and returns (the first handler is still waiting).
        """
        if update.effective_message is None or update.effective_message.text is None:
            return
        if update.effective_user is None:
            return

        chat_id = update.effective_message.chat_id
        user_id = update.effective_user.id
        message_id = update.effective_message.message_id
        text = update.effective_message.text
        now = time.monotonic()
        key = (chat_id, user_id)

        pending = self._pending.get(key)

        if pending is not None:
            # Check if this is a continuation fragment:
            # consecutive message_id AND within time gap
            is_fragment = (
                message_id == pending.last_message_id + 1
                and (now - pending.last_seen) < self._gap
            )
            if is_fragment:
                pending.parts.append(text)
                pending.last_message_id = message_id
                pending.last_seen = now
                # Signal the waiting handler to reset its timer
                evt = self._flush_events.get(key)
                if evt is not None:
                    evt.set()
                # This continuation handler can return immediately;
                # the first handler for this key is still waiting.
                return
            else:
                # Not a fragment — flush the old pending first
                await self._flush(key)

        # Start new pending fragment — this handler will wait for the gap
        # period to expire, then process the complete message.
        done_event = asyncio.Event()
        self._flush_events[key] = done_event
        self._pending[key] = _PendingFragment(
            chat_id=chat_id,
            user_id=user_id,
            last_message_id=message_id,
            parts=[text],
            last_seen=now,
            update=update,
            context=context,
        )

        # Wait for gap period, resetting if new fragments arrive
        while True:
            done_event.clear()
            try:
                await asyncio.wait_for(done_event.wait(), timeout=self._gap)
                # Event was set — a new fragment arrived, loop to reset timer
            except asyncio.TimeoutError:
                # Gap expired with no new fragment — flush now
                break

        await self._flush(key)

    async def _flush(self, key: tuple[int, int]) -> None:
        """Concatenate pending fragments and deliver to on_complete."""
        pending = self._pending.pop(key, None)
        self._flush_events.pop(key, None)
        if pending is None:
            return

        full_text = "".join(pending.parts)
        if pending.update is not None and pending.context is not None:
            try:
                await self._on_complete(full_text, pending.update, pending.context)
            except Exception:
                logger.exception("Error in fragment flush callback")


class StatusMessageManager:
    """Manages an editable Telegram status message showing tool activity.

    Sends a single "status" message that gets edited in-place as new tool
    events arrive. Debounces edits to respect Telegram's rate limits
    (~30 edits/minute). Caps the number of status lines to prevent overflow.
    """

    def __init__(
        self,
        chat_id: int,
        bot,
        *,
        debounce_seconds: float = _STATUS_DEBOUNCE_SECONDS,
        max_lines: int = _STATUS_MAX_LINES,
    ) -> None:
        self._chat_id = chat_id
        self._bot = bot
        self._debounce = debounce_seconds
        self._max_lines = max_lines
        self._message_id: int | None = None
        self._lines: list[str] = []
        self._last_edit: float = 0.0
        self._pending_flush: asyncio.Task | None = None

    async def add(self, status_text: str) -> None:
        """Add a status line and schedule an edit (debounced)."""
        self._lines.append(status_text)
        # Trim old lines if we exceed max
        if len(self._lines) > self._max_lines:
            overflow = len(self._lines) - self._max_lines
            self._lines = self._lines[overflow:]
            self._lines[0] = f"... (+{overflow} earlier)"

        await self._schedule_flush()

    async def _schedule_flush(self) -> None:
        """Flush immediately if debounce elapsed, otherwise schedule."""
        now = time.monotonic()
        elapsed = now - self._last_edit

        if elapsed >= self._debounce:
            await self._flush()
        elif self._pending_flush is None or self._pending_flush.done():
            remaining = self._debounce - elapsed
            self._pending_flush = asyncio.create_task(self._delayed_flush(remaining))

    async def _delayed_flush(self, delay: float) -> None:
        """Wait then flush (used when debounce not yet elapsed)."""
        await asyncio.sleep(delay)
        await self._flush()

    async def _flush(self) -> None:
        """Send or edit the status message with current lines."""
        text = "\n".join(self._lines)
        if not text:
            return

        try:
            if self._message_id is None:
                msg = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    disable_notification=True,
                    disable_web_page_preview=True,
                )
                self._message_id = msg.message_id
            else:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=text,
                    disable_web_page_preview=True,
                )
            self._last_edit = time.monotonic()
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                pass  # Telegram rejects no-op edits — that's fine
            else:
                logger.warning("Status message edit failed: %s", e)
        except Exception:
            logger.warning("Status message send/edit failed", exc_info=True)

    async def finish(self) -> None:
        """Cancel pending flush and do a final edit with italic styling."""
        if self._pending_flush is not None and not self._pending_flush.done():
            self._pending_flush.cancel()
            try:
                await self._pending_flush
            except asyncio.CancelledError:
                pass

        if self._message_id is not None and self._lines:
            # Final edit: italicize the whole status block to show it's done
            text = "<i>" + "\n".join(self._lines) + "</i>"
            try:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.debug("Final status edit failed (non-critical)", exc_info=True)

    @property
    def has_content(self) -> bool:
        """Whether any status lines have been added."""
        return bool(self._lines)


class TelegramBot:
    """Telegram bot that wraps ConversationRunner."""

    def __init__(self, config: OBSConfig, *, fragment_gap: float = _FRAGMENT_MAX_GAP_SECONDS) -> None:
        self._config = config
        self._hook_state = HookState()
        self._session_manager = SessionManager(config=config, hook_state=self._hook_state)
        self._pending_messages: list[str] = []
        self._fragment_buffer = FragmentBuffer(
            on_complete=self._process_message, gap_seconds=fragment_gap
        )

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle an incoming Telegram message (entry point from handler).

        Passes through the FragmentBuffer for reassembly of auto-split messages.
        """
        if update.effective_message is None or update.effective_message.text is None:
            return
        if update.effective_user is None:
            return

        # Auth check (before buffering to reject early)
        # SECURITY: empty allowed list = NO ONE can use the bot (deny by default)
        user_id = update.effective_user.id
        allowed = self._config.telegram_allowed_user_ids
        if not allowed or user_id not in allowed:
            logger.warning("Unauthorized Telegram user: %d", user_id)
            return

        await self._fragment_buffer.add(update, context)

    async def handle_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /new — reset session, start fresh conversation."""
        if update.effective_user is None:
            return
        allowed = self._config.telegram_allowed_user_ids
        if not allowed or update.effective_user.id not in allowed:
            return

        await self._session_manager.async_reset()
        self._pending_messages.clear()
        self._hook_state.interrupt_flag = False
        logger.info("Session reset via /new from user %d", update.effective_user.id)
        await update.effective_message.reply_text(
            "Session cleared. Starting fresh.", disable_web_page_preview=True
        )

    async def handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop — interrupt current response at next tool boundary."""
        if update.effective_user is None:
            return
        allowed = self._config.telegram_allowed_user_ids
        if not allowed or update.effective_user.id not in allowed:
            return

        self._hook_state.interrupt_flag = True
        logger.info("Interrupt via /stop from user %d", update.effective_user.id)
        await update.effective_message.reply_text(
            "Interrupt sent.", disable_web_page_preview=True
        )

    async def _run_conversation(
        self,
        user_text: str,
        chat_id: int,
        bot,
    ) -> tuple[list[str], list[str]]:
        """Run the conversation and collect text + status events.

        Returns (text_parts, remaining_pending). Handles StatusEvent
        by forwarding to the StatusMessageManager.

        If the runner raises mid-stream, any partial text collected so far
        is attached to the exception as `partial_text_parts` for the caller
        to deliver to the user.
        """
        runner = ConversationRunner(
            self._session_manager,
            self._hook_state,
            self._config,
            pending_messages=self._pending_messages,
        )

        status_mgr = StatusMessageManager(chat_id, bot)
        text_parts: list[str] = []

        try:
            async for event in runner.run(user_text):
                if isinstance(event, TextEvent):
                    text_parts.append(event.text)
                elif isinstance(event, StatusEvent):
                    await status_mgr.add(event.summary)
                elif isinstance(event, DoneEvent):
                    break
        except Exception as exc:
            await status_mgr.finish()
            # Attach partial text to the exception for the caller
            exc.partial_text_parts = text_parts  # type: ignore[attr-defined]
            raise

        await status_mgr.finish()
        return text_parts, runner.remaining_pending

    async def _send_response(
        self,
        text_parts: list[str],
        update: Update,
    ) -> None:
        """Format and send the response as HTML chunks."""
        full_text = "\n".join(text_parts)
        if not full_text.strip():
            full_text = "(no response)"

        html = md_to_telegram_html(full_text)
        chunks = split_message(html)

        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(_CHUNK_DELAY_SECONDS)
            try:
                await update.effective_message.reply_text(
                    chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest as e:
                if "can't parse entities" in str(e).lower():
                    logger.warning("HTML parse failed, sending as plain text: %s", e)
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    await update.effective_message.reply_text(
                        plain, disable_web_page_preview=True
                    )
                else:
                    raise

    async def _process_message(
        self,
        user_text: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Process a complete (possibly reassembled) user message."""
        chat_id = update.effective_message.chat_id
        logger.info("Processing message from chat %d: %s", chat_id, user_text[:100])

        # Start typing indicator loop
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(
            _typing_loop(chat_id, context.bot, typing_stop)
        )

        try:
            text_parts, remaining = await self._run_conversation(
                user_text, chat_id, context.bot
            )
            self._pending_messages = remaining
            logger.info("Runner completed, %d text parts collected", len(text_parts))
        except Exception as exc:
            logger.exception("Error in ConversationRunner")
            # Deliver partial text if any was collected before the error
            partial_parts = getattr(exc, "partial_text_parts", [])
            error_detail = f"{type(exc).__name__}: {exc}"
            if len(error_detail) > 200:
                error_detail = error_detail[:200] + "..."
            try:
                if partial_parts:
                    # Send partial response with error appended
                    partial_parts.append(f"\n\n---\n(error: {error_detail})")
                    await self._send_response(partial_parts, update)
                else:
                    await update.effective_message.reply_text(
                        f"(error: {error_detail})",
                        disable_web_page_preview=True,
                    )
            except Exception:
                logger.exception("Failed to send error reply")
            # Reset session on unrecoverable errors to avoid stuck state
            try:
                await self._session_manager.async_reset()
                logger.info("Session reset after error")
            except Exception:
                logger.warning("Session reset after error also failed", exc_info=True)
            return
        finally:
            typing_stop.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        await self._send_response(text_parts, update)


def create_telegram_app(config: OBSConfig) -> Application:
    """Create and configure a python-telegram-bot Application."""
    if not config.telegram_bot_token:
        raise ValueError("OBS_TELEGRAM_BOT_TOKEN is required")
    if not config.telegram_allowed_user_ids:
        raise ValueError(
            "OBS_TELEGRAM_ALLOWED_USERS is required (comma-separated Telegram user IDs). "
            "The bot will not start without an explicit allowlist."
        )

    bot = TelegramBot(config)
    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("new", bot.handle_new))
    app.add_handler(CommandHandler("stop", bot.handle_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    return app


async def _set_bot_commands(app: Application) -> None:
    """Register bot commands with Telegram for menu auto-suggestion."""
    from telegram import BotCommand

    await app.bot.set_my_commands([
        BotCommand("new", "Clear context and start a fresh session"),
        BotCommand("stop", "Interrupt the current response"),
    ])


async def run_telegram_bot(config: OBSConfig) -> None:
    """Start the Telegram bot (blocking)."""
    app = create_telegram_app(config)
    logger.info("Starting Telegram bot...")
    await app.initialize()
    await _set_bot_commands(app)
    await app.start()
    await app.updater.start_polling()
    # Block until stopped
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
