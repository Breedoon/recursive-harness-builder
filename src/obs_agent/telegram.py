"""Telegram bot for OBS Agent.

Receives messages from Telegram, processes them through ConversationRunner,
and sends chronological per-turn updates. Key behavior:
- FragmentBuffer reassembles user text auto-split by Telegram (>4096 chars)
- Per-turn flush: text + status events are interleaved in arrival order
- Per-chat lock serialization (keeps replies ordered within a chat)
- Background queue poller auto-delivers queued results every 3 seconds
- Final "(done)" sentinel is sent with notification enabled
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent, TurnEndEvent
from obs_agent.session import SessionManager
from obs_agent.telegram_format import md_to_telegram_html, split_message

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.telegram")

# Delay between sending split message chunks (Telegram rate limit: ~1 msg/sec/chat)
_CHUNK_DELAY_SECONDS = 1.0

# FragmentBuffer: max gap between consecutive message_ids to be considered fragments
_FRAGMENT_MAX_GAP_SECONDS = 1.5

# Background queue polling interval
_BACKGROUND_POLL_SECONDS = 3.0

# Prompt used when auto-delivering queued background results while user is idle.
_AUTO_DELIVERY_PROMPT = (
    "(System: queued updates arrived while idle. Process and summarize them.)"
)


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _drain_queue(queue: asyncio.Queue) -> list[str]:
    """Drain all messages from an asyncio.Queue, returning them as a list."""
    messages: list[str] = []
    while not queue.empty():
        try:
            messages.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return messages


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
    """

    def __init__(
        self,
        on_complete,  # async callable(full_text, update, context)
        gap_seconds: float = _FRAGMENT_MAX_GAP_SECONDS,
    ) -> None:
        self._on_complete = on_complete
        self._gap = gap_seconds
        self._pending: dict[tuple[int, int], _PendingFragment] = {}
        self._flush_events: dict[tuple[int, int], asyncio.Event] = {}

    async def add(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Add an incoming message. Blocks until the message is fully processed."""
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
            is_fragment = (
                message_id == pending.last_message_id + 1
                and (now - pending.last_seen) < self._gap
            )
            if is_fragment:
                pending.parts.append(text)
                pending.last_message_id = message_id
                pending.last_seen = now
                evt = self._flush_events.get(key)
                if evt is not None:
                    evt.set()
                return
            await self._flush(key)

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

        while True:
            done_event.clear()
            try:
                await asyncio.wait_for(done_event.wait(), timeout=self._gap)
            except asyncio.TimeoutError:
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


class TelegramBot:
    """Telegram bot that wraps ConversationRunner."""

    def __init__(
        self,
        config: OBSConfig,
        *,
        fragment_gap: float = _FRAGMENT_MAX_GAP_SECONDS,
        background_poll_seconds: float = _BACKGROUND_POLL_SECONDS,
        enable_background_poller: bool = True,
    ) -> None:
        self._config = config
        self._hook_state = HookState()
        self._session_manager = SessionManager(config=config, hook_state=self._hook_state)
        self._pending_messages: list[str] = []
        self._fragment_buffer = FragmentBuffer(
            on_complete=self._process_message, gap_seconds=fragment_gap
        )

        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._busy_chats: set[int] = set()

        # SINGLE-USER ASSUMPTION: these track only the most recent active chat.
        # Multi-user support must use per-chat/per-user routing for auto-delivery.
        self._last_chat_id: int | None = None
        self._last_bot = None

        self._enable_background_poller = enable_background_poller
        self._background_poll_seconds = background_poll_seconds
        self._background_task: asyncio.Task | None = None

    def _get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    def _is_authorized(self, user_id: int) -> bool:
        # SECURITY: empty allowed list = NO ONE can use the bot (deny by default)
        allowed = self._config.telegram_allowed_user_ids
        return bool(allowed) and user_id in allowed

    async def _ensure_background_poller(self, bot) -> None:
        if not self._enable_background_poller:
            return
        if self._background_task is not None and not self._background_task.done():
            return
        self._last_bot = bot
        self._background_task = asyncio.create_task(self._background_poller_loop())

    async def shutdown(self) -> None:
        """Stop background tasks owned by this adapter."""
        if self._background_task is not None and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass

    async def _send_plain_with_retry(
        self,
        *,
        chat_id: int,
        bot,
        text: str,
        disable_notification: bool,
        parse_mode: ParseMode | None = None,
    ) -> None:
        """Send one Telegram message with retry/backoff on transport errors."""
        attempt = 0
        while True:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                    disable_notification=disable_notification,
                )
                return
            except RetryAfter as exc:
                delay = max(float(exc.retry_after), 1.0)
                attempt += 1
                logger.warning(
                    "Telegram rate limit chat=%d attempt=%d retry_in=%.1fs",
                    chat_id, attempt, delay,
                )
                await asyncio.sleep(delay)
            except BadRequest:
                # Caller handles permanent payload errors (e.g. bad HTML entities).
                raise
            except TelegramError as exc:
                attempt += 1
                delay = min(30.0, 2 ** min(attempt, 5))
                logger.warning(
                    "Telegram send failed chat=%d attempt=%d retry_in=%.1fs: %s",
                    chat_id, attempt, delay, exc,
                )
                await asyncio.sleep(delay)

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle an incoming Telegram message (entry point from handler)."""
        if update.effective_message is None or update.effective_message.text is None:
            return
        if update.effective_user is None:
            return

        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            logger.warning("Unauthorized Telegram user: %d", user_id)
            return

        await self._ensure_background_poller(context.bot)
        await self._fragment_buffer.add(update, context)

    async def handle_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /new - reset session, start fresh conversation."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        await self._ensure_background_poller(context.bot)

        chat_id = update.effective_message.chat_id
        # Set interrupt BEFORE acquiring lock — if a response is in-progress
        # (holding the lock), this causes it to exit at the next tool boundary,
        # releasing the lock sooner.
        self._hook_state.interrupt_flag = True
        lock = self._get_chat_lock(chat_id)
        async with lock:
            await self._session_manager.async_reset()
            self._pending_messages.clear()
            self._hook_state.reset()  # Drain queues, cancel background tasks
            logger.info("Session reset via /new from user %d", update.effective_user.id)
            await update.effective_message.reply_text(
                "Session cleared. Starting fresh.",
                disable_web_page_preview=True,
            )

    async def handle_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /context - show session and context window info (no agent involved)."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        lines: list[str] = []
        sid = self._session_manager.session_id
        lines.append(f"session_id: {sid or '(none)'}")

        data = self._hook_state.last_result_data
        if data:
            lines.append(f"num_turns: {data.get('num_turns', '?')}")
            cost = data.get("total_cost_usd")
            lines.append(f"total_cost_usd: {cost if cost is not None else '?'}")
            lines.append(f"duration_ms: {data.get('duration_ms', '?')}")

            usage = data.get("usage")
            if usage:
                inp = usage.get("input_tokens") or 0
                out = usage.get("output_tokens") or 0
                cache_create = usage.get("cache_creation_input_tokens") or 0
                cache_rd = usage.get("cache_read_input_tokens") or 0
                total = inp + out + cache_create + cache_rd
                pct = max(0.0, (1 - total / 200_000) * 100)
                lines.append(f"input_tokens: {inp}")
                lines.append(f"output_tokens: {out}")
                lines.append(f"cache_creation: {cache_create}")
                lines.append(f"cache_read: {cache_rd}")
                lines.append(f"total_used: {total}")
                lines.append(f"context_remaining: {pct:.1f}%")
            else:
                lines.append("(no usage data yet)")
        else:
            lines.append("(no session data yet)")

        await update.effective_message.reply_text(
            "\n".join(lines),
            disable_web_page_preview=True,
        )

    async def handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop - interrupt current response at next tool boundary."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        await self._ensure_background_poller(context.bot)
        self._hook_state.interrupt_flag = True
        logger.info("Interrupt via /stop from user %d", update.effective_user.id)
        await update.effective_message.reply_text(
            "Interrupt sent.",
            disable_web_page_preview=True,
        )

    def _status_to_text(self, event: StatusEvent) -> str:
        if event.type == "queue_delivered" and event.messages:
            message_blob = " | ".join(event.messages)
            if event.count is not None:
                return f"{event.summary} ({event.count}): {message_blob}"
            return f"{event.summary}: {message_blob}"
        return event.summary

    def _render_turn_html(self, turn_items: list[TextEvent | StatusEvent]) -> str:
        parts: list[str] = []
        for item in turn_items:
            if isinstance(item, TextEvent):
                rendered = md_to_telegram_html(item.text)
                if rendered:
                    parts.append(rendered)
            elif isinstance(item, StatusEvent):
                status_text = self._status_to_text(item)
                if status_text.strip():
                    parts.append(f"<i>{html.escape(status_text)}</i>")
        return "\n".join(parts).strip()

    async def _send_html(
        self,
        *,
        chat_id: int,
        bot,
        html_text: str,
        disable_notification: bool,
    ) -> None:
        text = html_text.strip() if html_text.strip() else "(no response)"
        chunks = split_message(text)

        for index, chunk in enumerate(chunks):
            if index > 0:
                await asyncio.sleep(_CHUNK_DELAY_SECONDS)
            try:
                await self._send_plain_with_retry(
                    chat_id=chat_id,
                    bot=bot,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_notification=disable_notification,
                )
            except BadRequest as exc:
                msg_lower = str(exc).lower()
                if "can't parse entities" in msg_lower:
                    logger.warning("HTML parse failed, sending plain text: %s", exc)
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    await self._send_plain_with_retry(
                        chat_id=chat_id,
                        bot=bot,
                        text=plain,
                        disable_notification=disable_notification,
                    )
                    continue
                if "too long" in msg_lower:
                    logger.warning(
                        "Chunk too long (%d chars), re-splitting as plain text: %s",
                        len(chunk), exc,
                    )
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    sub_chunks = split_message(plain)
                    for sub in sub_chunks:
                        await self._send_plain_with_retry(
                            chat_id=chat_id,
                            bot=bot,
                            text=sub,
                            disable_notification=disable_notification,
                        )
                    continue
                raise

    async def _flush_turn(
        self,
        *,
        chat_id: int,
        bot,
        turn_items: list[TextEvent | StatusEvent],
    ) -> None:
        if not turn_items:
            return
        html_text = self._render_turn_html(turn_items)
        if not html_text:
            return
        logger.info(
            "[flush_turn] sending %d chars (%d items) chat=%d",
            len(html_text), len(turn_items), chat_id,
        )
        await self._send_html(
            chat_id=chat_id,
            bot=bot,
            html_text=html_text,
            disable_notification=True,
        )

    async def _run_and_send(
        self,
        *,
        user_text: str,
        chat_id: int,
        bot,
        extra_pending: list[str] | None = None,
    ) -> None:
        pending_messages = list(self._pending_messages)
        if extra_pending:
            pending_messages.extend(extra_pending)

        runner = ConversationRunner(
            self._session_manager,
            self._hook_state,
            self._config,
            pending_messages=pending_messages,
        )

        turn_items: list[TextEvent | StatusEvent] = []
        done_sent = False
        event_count = 0
        turn_count = 0
        self._busy_chats.add(chat_id)
        logger.info(
            "[run_and_send] START chat=%d msg=%s",
            chat_id, user_text[:80],
        )

        try:
            await self._send_plain_with_retry(
                chat_id=chat_id,
                bot=bot,
                text="(working)",
                disable_notification=True,
            )
            async for event in runner.run(user_text):
                event_count += 1
                if isinstance(event, TextEvent | StatusEvent):
                    turn_items.append(event)
                    continue

                if isinstance(event, TurnEndEvent):
                    turn_count += 1
                    logger.info(
                        "[run_and_send] TurnEnd #%d items=%d chat=%d",
                        turn_count, len(turn_items), chat_id,
                    )
                    await self._flush_turn(chat_id=chat_id, bot=bot, turn_items=turn_items)
                    turn_items.clear()
                    continue

                if isinstance(event, DoneEvent):
                    logger.info(
                        "[run_and_send] DoneEvent events=%d turns=%d chat=%d",
                        event_count, turn_count, chat_id,
                    )
                    # Safety flush in case a producer emitted text without TurnEndEvent.
                    if turn_items:
                        await self._flush_turn(chat_id=chat_id, bot=bot, turn_items=turn_items)
                        turn_items.clear()

                    await self._send_plain_with_retry(
                        chat_id=chat_id,
                        bot=bot,
                        text="(done)",
                        disable_notification=False,
                    )
                    done_sent = True
                    break

            self._pending_messages = runner.remaining_pending

            if not done_sent:
                await self._send_plain_with_retry(
                    chat_id=chat_id,
                    bot=bot,
                    text="(done)",
                    disable_notification=False,
                )
                done_sent = True

        except Exception as exc:
            logger.exception("Error in ConversationRunner")

            error_detail = f"{type(exc).__name__}: {exc}"
            if len(error_detail) > 200:
                error_detail = error_detail[:200] + "..."

            try:
                await self._send_plain_with_retry(
                    chat_id=chat_id,
                    bot=bot,
                    text=f"(error: {error_detail} — session preserved)",
                    disable_notification=False,
                )
            except Exception:
                logger.exception("Failed to send error reply")

            try:
                await self._session_manager.soft_reset()
                logger.info("Soft reset after error (session_id preserved)")
            except Exception:
                logger.warning("Soft reset failed", exc_info=True)

        finally:
            self._busy_chats.discard(chat_id)
            # Always send the (done) sentinel so consumers (eval harness, etc.)
            # know the turn is complete. Without this, _collect_response with
            # require_done=True hangs until timeout on error paths.
            if not done_sent:
                try:
                    await self._send_plain_with_retry(
                        chat_id=chat_id,
                        bot=bot,
                        text="(done)",
                        disable_notification=False,
                    )
                except Exception:
                    logger.debug("Failed to send (done) sentinel in finally", exc_info=True)

    async def _process_message(
        self,
        user_text: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Process a complete (possibly reassembled) user message."""
        if update.effective_message is None:
            return

        chat_id = update.effective_message.chat_id
        self._last_chat_id = chat_id
        self._last_bot = context.bot

        logger.info("Processing message from chat %d: %s", chat_id, user_text[:100])

        lock = self._get_chat_lock(chat_id)
        logger.info(
            "[process_message] lock.locked=%s busy=%s chat=%d",
            lock.locked(), bool(self._busy_chats), chat_id,
        )
        try:
            await self._send_plain_with_retry(
                chat_id=chat_id,
                bot=context.bot,
                text="(received)",
                disable_notification=True,
            )
        except Exception:
            logger.debug("Failed to send receipt marker", exc_info=True)
        async with lock:
            logger.info("[process_message] lock acquired chat=%d", chat_id)
            await self._run_and_send(
                user_text=user_text,
                chat_id=chat_id,
                bot=context.bot,
            )
            logger.info("[process_message] _run_and_send returned chat=%d", chat_id)

    async def _background_poller_loop(self) -> None:
        """Poll for queued background messages and auto-deliver when idle."""
        while True:
            try:
                await asyncio.sleep(self._background_poll_seconds)

                chat_id = self._last_chat_id
                bot = self._last_bot
                if chat_id is None or bot is None:
                    continue
                if self._busy_chats:
                    continue

                queued = _drain_queue(self._hook_state.message_queue)
                has_pending = bool(self._pending_messages)
                if not queued and not has_pending:
                    continue

                lock = self._get_chat_lock(chat_id)
                if lock.locked():
                    # Put drained items back; active work should process them.
                    for message in queued:
                        self._hook_state.message_queue.put_nowait(message)
                    continue

                logger.info(
                    "Auto-delivering queued updates queued=%d pending=%d",
                    len(queued), len(self._pending_messages),
                )
                async with lock:
                    # Re-check idle under lock, otherwise requeue.
                    if self._busy_chats:
                        for message in queued:
                            self._hook_state.message_queue.put_nowait(message)
                        continue

                    await self._run_and_send(
                        user_text=_AUTO_DELIVERY_PROMPT,
                        chat_id=chat_id,
                        bot=bot,
                        extra_pending=queued if queued else None,
                    )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Background poller iteration failed")


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
    app.bot_data["obs_telegram_bot"] = bot
    app.add_handler(CommandHandler("new", bot.handle_new))
    app.add_handler(CommandHandler("stop", bot.handle_stop))
    app.add_handler(CommandHandler("context", bot.handle_context))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    return app


async def _set_bot_commands(app: Application) -> None:
    """Register bot commands with Telegram for menu auto-suggestion."""
    from telegram import BotCommand

    await app.bot.set_my_commands([
        BotCommand("new", "Clear context and start a fresh session"),
        BotCommand("stop", "Interrupt the current response"),
        BotCommand("context", "Show session and context window info"),
    ])


async def run_telegram_bot(config: OBSConfig) -> None:
    """Start the Telegram bot (blocking)."""
    app = create_telegram_app(config)
    tg_bot: TelegramBot = app.bot_data["obs_telegram_bot"]

    logger.info("Starting Telegram bot...")
    await app.initialize()
    await _set_bot_commands(app)
    await app.start()
    await app.updater.start_polling()

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await tg_bot.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
