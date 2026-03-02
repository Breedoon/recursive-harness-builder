"""Telegram bot for OBS Agent.

Receives messages from Telegram, processes them through ConversationRunner,
and sends chronological per-turn updates. Key behavior:
- FragmentBuffer batches rapid same-user messages and reassembles Telegram auto-split long user text
- Per-turn flush: text + status events are interleaved in arrival order
- Per-chat lock serialization (keeps replies ordered within a chat)
- Background queue poller auto-delivers queued results every 3 seconds
- Final idle context summary is sent with notification enabled once the queue is empty
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from obs_agent.context_probe import probe_context_via_claude_cli
from obs_agent.context_stats import (
    apply_context_probe,
    build_context_snapshot,
    format_context_snapshot_compact,
    format_context_snapshot_lines,
)
from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.jsonl_fork import fork_session_jsonl
from obs_agent.queueing import QueuedMessage, coerce_queued_message
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent, TurnEndEvent
from obs_agent.session import SessionManager
from obs_agent.telegram_format import md_to_telegram_html, split_message
from obs_agent.telegram_ingest import TelegramInboundNormalizer

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.telegram")

# Delay between sending split message chunks (Telegram rate limit: ~1 msg/sec/chat)
_CHUNK_DELAY_SECONDS = 1.0

# Inbound batching quiet period. Hold delivery until no new same-chat/user
# message arrives within this window so forwarded multi-message batches are
# injected together.
_FRAGMENT_MAX_GAP_SECONDS = 1.0

# Only near-limit chunks should be considered Telegram auto-split fragments.
_FRAGMENT_MIN_PART_LENGTH = 4000

# Buffer gap for Telegram media groups/albums.
_MEDIA_GROUP_GAP_SECONDS = 0.75

# Background queue polling interval
_BACKGROUND_POLL_SECONDS = 3.0

# One-shot reminder for the root/main session age.
_MAIN_SESSION_WARNING_SECONDS = 50 * 60.0

# Prompt used when auto-delivering queued background results while user is idle.
_AUTO_DELIVERY_PROMPT = (
    "(System: queued updates arrived while idle. Process and summarize them.)"
)


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _drain_queue(queue: asyncio.Queue) -> list[QueuedMessage]:
    """Drain all messages from an asyncio.Queue, returning them as a list."""
    messages: list[QueuedMessage] = []
    while not queue.empty():
        try:
            messages.append(coerce_queued_message(queue.get_nowait()))
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
    segments: list[str] = field(default_factory=list)
    last_seen: float = 0.0
    last_part_length: int = 0
    update: Update | None = None  # The first update (for reply context)
    context: ContextTypes.DEFAULT_TYPE | None = None


@dataclass
class _PendingMediaGroup:
    """A Telegram media group waiting for all items to arrive."""

    chat_id: int
    user_id: int
    media_group_id: str
    updates: list[Update] = field(default_factory=list)
    last_seen: float = 0.0
    context: ContextTypes.DEFAULT_TYPE | None = None


@dataclass(frozen=True)
class _TelegramMessageBinding:
    """Maps a Telegram message to a JSONL UUID in a specific session."""

    jsonl_uuid: str
    session_id: str
    role: str


class FragmentBuffer:
    """Buffers consecutive Telegram messages from the same user/chat.

    When a user pastes text longer than 4096 chars, Telegram auto-splits it
    into multiple updates with consecutive message_ids sent in rapid succession.
    This buffer also batches separate same-chat/user messages arriving within a
    short quiet period, so forwarded multi-message dumps are injected together.
    Only true near-limit Telegram fragments are concatenated directly; other
    batched messages are separated by paragraph breaks.

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
            within_gap = (now - pending.last_seen) < self._gap
            if within_gap:
                is_fragment = (
                    message_id == pending.last_message_id + 1
                    and pending.last_part_length >= _FRAGMENT_MIN_PART_LENGTH
                )
                if is_fragment:
                    pending.segments[-1] += text
                else:
                    pending.segments.append(text)
                pending.last_message_id = message_id
                pending.last_seen = now
                pending.last_part_length = len(text)
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
            segments=[text],
            last_seen=now,
            last_part_length=len(text),
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

        full_text = "\n\n".join(pending.segments)
        if pending.update is not None and pending.context is not None:
            try:
                await self._on_complete(full_text, pending.update, pending.context)
            except Exception:
                logger.exception("Error in fragment flush callback")


class MediaGroupBuffer:
    """Buffers Telegram media groups so albums become one logical turn."""

    def __init__(self, on_complete, gap_seconds: float = _MEDIA_GROUP_GAP_SECONDS) -> None:
        self._on_complete = on_complete
        self._gap = gap_seconds
        self._pending: dict[tuple[int, int, str], _PendingMediaGroup] = {}
        self._flush_events: dict[tuple[int, int, str], asyncio.Event] = {}

    async def add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None or update.effective_user is None:
            return
        media_group_id = update.effective_message.media_group_id
        if media_group_id is None:
            return

        key = (
            update.effective_message.chat_id,
            update.effective_user.id,
            str(media_group_id),
        )
        now = time.monotonic()
        pending = self._pending.get(key)
        if pending is not None:
            pending.updates.append(update)
            pending.last_seen = now
            pending.context = context
            evt = self._flush_events.get(key)
            if evt is not None:
                evt.set()
            return

        done_event = asyncio.Event()
        self._flush_events[key] = done_event
        self._pending[key] = _PendingMediaGroup(
            chat_id=update.effective_message.chat_id,
            user_id=update.effective_user.id,
            media_group_id=str(media_group_id),
            updates=[update],
            last_seen=now,
            context=context,
        )

        while True:
            done_event.clear()
            try:
                await asyncio.wait_for(done_event.wait(), timeout=self._gap)
            except asyncio.TimeoutError:
                break

        await self._flush(key)

    async def _flush(self, key: tuple[int, int, str]) -> None:
        pending = self._pending.pop(key, None)
        self._flush_events.pop(key, None)
        if pending is None or pending.context is None:
            return
        try:
            await self._on_complete(pending.updates, pending.context)
        except Exception:
            logger.exception("Error in media-group flush callback")


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
        self._pending_messages: list[QueuedMessage] = []
        self._fragment_buffer = FragmentBuffer(
            on_complete=self._process_message, gap_seconds=fragment_gap
        )
        self._media_group_buffer = MediaGroupBuffer(
            on_complete=self._process_media_group,
        )
        self._normalizer = TelegramInboundNormalizer(
            temp_root=config.telegram_temp_root,
            transcription_script=config.telegram_transcription_script,
        )

        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._busy_chats: set[int] = set()

        # SINGLE-USER ASSUMPTION: these track only the most recent active chat.
        # Multi-user support must use per-chat/per-user routing for auto-delivery.
        self._last_chat_id: int | None = None
        self._last_bot = None
        self._message_map: dict[int, _TelegramMessageBinding] = {}
        self._session_heads: dict[str, str] = {}
        self._main_session_id: str | None = None
        self._main_session_started_at: float | None = None
        self._main_session_warning_sent = False
        self._main_session_warning_seconds = _MAIN_SESSION_WARNING_SECONDS
        self._media_group_receipt_ids: dict[tuple[int, int, str], list[int]] = {}

        self._enable_background_poller = enable_background_poller
        self._background_poll_seconds = background_poll_seconds
        self._background_task: asyncio.Task | None = None

    async def initialize_runtime(self) -> None:
        """Purge the Telegram temp root once and create a fresh workspace."""
        self._normalizer.initialize()

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
        reply_to_message_id: int | None = None,
    ):
        """Send one Telegram message with retry/backoff on transport errors."""
        attempt = 0
        while True:
            try:
                return await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                    disable_notification=disable_notification,
                    reply_to_message_id=reply_to_message_id,
                )
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

    def _format_system_html(self, text: str) -> str:
        return f"<u><i>{html.escape(text)}</i></u>"

    def _format_status_html(self, text: str) -> str:
        return f"<i>{html.escape(text)}</i>"

    async def _send_system_message(
        self,
        *,
        chat_id: int,
        bot,
        text: str,
        disable_notification: bool,
        reply_to_message_id: int | None = None,
    ):
        return await self._send_plain_with_retry(
            chat_id=chat_id,
            bot=bot,
            text=self._format_system_html(text),
            parse_mode=ParseMode.HTML,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
        )

    def _build_completion_summary(self) -> str:
        snapshot = build_context_snapshot(
            session_id=self._session_manager.session_id,
            data=self._hook_state.last_result_data,
            context_window_estimate_tokens=self._config.context_window_estimate_tokens,
            cwd=self._config.vault_path,
        )
        return self._append_notify_username(format_context_snapshot_compact(snapshot))

    def _append_notify_username(self, text: str) -> str:
        lines = [text]
        username = (self._config.telegram_notify_username or "").strip().lstrip("@")
        if username:
            lines.append(f"@{username}")
        return "\n".join(lines)

    def _has_queue_idle_state(self, pending_messages: list[QueuedMessage]) -> bool:
        return not pending_messages and self._hook_state.message_queue.empty()

    async def _activate_session(self, session_id: str | None) -> None:
        current_id = self._session_manager.session_id
        if current_id == session_id:
            return

        old_manager = self._session_manager
        old_hook_state = self._hook_state

        self._hook_state = HookState()
        self._session_manager = SessionManager(config=self._config, hook_state=self._hook_state)
        if session_id:
            self._session_manager.set_session_id(session_id)

        self._pending_messages = []

        try:
            await old_manager.disconnect()
        finally:
            old_hook_state.reset()

    def _reset_main_session_tracking(self) -> None:
        self._main_session_id = None
        self._main_session_started_at = None
        self._main_session_warning_sent = False

    def _register_main_session_if_needed(self) -> None:
        session_id = self._session_manager.session_id
        if self._main_session_id is not None or not session_id:
            return
        self._main_session_id = session_id
        self._main_session_started_at = self._session_manager.last_activity or time.time()
        self._main_session_warning_sent = False
        logger.info(
            "[main_session] registered session_id=%s started_at=%.3f",
            session_id,
            self._main_session_started_at,
        )

    async def _maybe_send_main_session_warning(self, *, chat_id: int, bot) -> None:
        if (
            self._main_session_warning_sent
            or self._main_session_id is None
            or self._main_session_started_at is None
        ):
            return
        if self._session_manager.session_id != self._main_session_id:
            return
        elapsed = time.time() - self._main_session_started_at
        if elapsed < self._main_session_warning_seconds:
            return
        try:
            await self._send_system_message(
                chat_id=chat_id,
                bot=bot,
                text=self._append_notify_username("main session has been active for 50 minutes"),
                disable_notification=False,
            )
        except Exception:
            logger.debug("Failed to send main-session reminder", exc_info=True)
            return
        self._main_session_warning_sent = True

    async def _send_received_marker(
        self,
        *,
        chat_id: int,
        bot,
        reply_to_message_id: int | None,
    ) -> list[int]:
        receipt_message = await self._send_system_message(
            chat_id=chat_id,
            bot=bot,
            text="received",
            disable_notification=True,
            reply_to_message_id=reply_to_message_id,
        )
        receipt_message_id = self._sent_message_id(receipt_message)
        if receipt_message_id is None:
            return []
        return [receipt_message_id]

    def _record_message_binding(
        self,
        *,
        message_id: int,
        jsonl_uuid: str,
        session_id: str,
        role: str,
    ) -> None:
        self._message_map[message_id] = _TelegramMessageBinding(
            jsonl_uuid=jsonl_uuid,
            session_id=session_id,
            role=role,
        )
        logger.info(
            "[message_map] telegram_msg_id=%d session_id=%s role=%s uuid=%s",
            message_id,
            session_id,
            role,
            jsonl_uuid,
        )

    def _sent_message_id(self, message) -> int | None:
        candidate = getattr(message, "message_id", None)
        if isinstance(candidate, int):
            return candidate
        candidate = getattr(message, "id", None)
        if isinstance(candidate, int):
            return candidate
        return None

    def _record_or_defer_message_bindings(
        self,
        *,
        message_ids: list[int],
        jsonl_uuid: str,
        role: str,
        deferred_bindings: list[tuple[int, str, str]] | None = None,
    ) -> None:
        session_id = self._session_manager.session_id
        for message_id in message_ids:
            if session_id:
                self._record_message_binding(
                    message_id=message_id,
                    jsonl_uuid=jsonl_uuid,
                    session_id=session_id,
                    role=role,
                )
            elif deferred_bindings is not None:
                deferred_bindings.append((message_id, jsonl_uuid, role))

    def _flush_deferred_bindings(
        self,
        *,
        deferred_bindings: list[tuple[int, str, str]],
        session_id: str | None,
    ) -> None:
        if not session_id or not deferred_bindings:
            return
        while deferred_bindings:
            message_id, jsonl_uuid, role = deferred_bindings.pop(0)
            self._record_message_binding(
                message_id=message_id,
                jsonl_uuid=jsonl_uuid,
                session_id=session_id,
                role=role,
            )

    async def _resolve_session_for_trigger(
        self,
        *,
        trigger_message: QueuedMessage | None,
        chat_id: int,
        bot,
    ) -> tuple[bool, int | None]:
        reply_to_user_message_id = trigger_message.telegram_message_id if trigger_message else None
        target_message_id = trigger_message.reply_to_message_id if trigger_message else None
        if target_message_id is None:
            return True, reply_to_user_message_id

        binding = self._message_map.get(target_message_id)
        logger.info(
            "[reply_lookup] target_message_id=%s found=%s",
            target_message_id,
            bool(binding),
        )
        if binding is None:
            await self._send_system_message(
                chat_id=chat_id,
                bot=bot,
                text="can't fork from this message",
                disable_notification=True,
                reply_to_message_id=reply_to_user_message_id,
            )
            return False, reply_to_user_message_id

        latest_uuid = self._session_heads.get(binding.session_id)
        if latest_uuid == binding.jsonl_uuid:
            await self._activate_session(binding.session_id)
            return True, reply_to_user_message_id

        fork_session_id = fork_session_jsonl(
            session_id=binding.session_id,
            target_uuid=binding.jsonl_uuid,
            cwd=self._config.vault_path,
            new_session_id=str(uuid.uuid4()),
        )
        self._session_heads[fork_session_id] = binding.jsonl_uuid
        await self._activate_session(fork_session_id)
        return True, reply_to_user_message_id

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle an incoming Telegram message (entry point from handler)."""
        if update.effective_user is None:
            return
        if update.effective_message is None:
            return

        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            logger.warning("Unauthorized Telegram user: %d", user_id)
            return

        await self._ensure_background_poller(context.bot)
        message = update.effective_message
        if message.media_group_id is not None:
            media_group_key = (
                message.chat_id,
                user_id,
                str(message.media_group_id),
            )
            if media_group_key not in self._media_group_receipt_ids:
                try:
                    self._media_group_receipt_ids[media_group_key] = await self._send_received_marker(
                        chat_id=message.chat_id,
                        bot=context.bot,
                        reply_to_message_id=message.message_id,
                    )
                except Exception:
                    logger.debug("Failed to send media-group receipt marker", exc_info=True)
                    self._media_group_receipt_ids[media_group_key] = []
            await self._media_group_buffer.add(update, context)
            return
        if message.text is not None:
            await self._fragment_buffer.add(update, context)
            return
        receipt_message_ids: list[int] = []
        try:
            receipt_message_ids = await self._send_received_marker(
                chat_id=message.chat_id,
                bot=context.bot,
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logger.debug("Failed to send attachment receipt marker", exc_info=True)
        normalized = await self._normalizer.normalize_update(update)
        if not normalized.agent_text.strip():
            logger.info("Skipping empty normalized Telegram message id=%s", message.message_id)
            return
        await self._process_message(
            normalized.agent_text,
            update,
            context,
            pre_sent_status_message_ids=receipt_message_ids,
            user_warnings=normalized.user_warnings,
        )

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
            self._message_map.clear()
            self._session_heads.clear()
            self._media_group_receipt_ids.clear()
            self._reset_main_session_tracking()
            logger.info("Session reset via /new from user %d", update.effective_user.id)
            await self._send_system_message(
                chat_id=chat_id,
                bot=context.bot,
                text="session cleared",
                disable_notification=True,
            )

    async def handle_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /context - show session and context window info (no agent involved)."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        snapshot = build_context_snapshot(
            session_id=self._session_manager.session_id,
            data=self._hook_state.last_result_data,
            context_window_estimate_tokens=self._config.context_window_estimate_tokens,
            cwd=self._config.vault_path,
        )
        probe = None
        if self._config.context_probe_claude_cli:
            probe = await probe_context_via_claude_cli(
                session_id=snapshot.get("session_id"),
                cwd=self._config.vault_path,
            )
        snapshot = apply_context_probe(snapshot, probe)
        lines = format_context_snapshot_lines(snapshot)

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
        await self._send_system_message(
            chat_id=update.effective_message.chat_id,
            bot=context.bot,
            text="interrupt sent",
            disable_notification=True,
        )

    def _status_to_text(self, event: StatusEvent) -> str:
        if event.type == "queue_delivered":
            return ""
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
                    parts.append(self._format_status_html(status_text))
        return "\n".join(parts).strip()

    async def _send_html(
        self,
        *,
        chat_id: int,
        bot,
        html_text: str,
        disable_notification: bool,
        reply_to_message_id: int | None = None,
    ) -> list:
        text = html_text.strip() if html_text.strip() else "(no response)"
        chunks = split_message(text)
        sent_messages: list = []

        for index, chunk in enumerate(chunks):
            if index > 0:
                await asyncio.sleep(_CHUNK_DELAY_SECONDS)
            try:
                sent = await self._send_plain_with_retry(
                    chat_id=chat_id,
                    bot=bot,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_notification=disable_notification,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                )
                sent_messages.append(sent)
            except BadRequest as exc:
                msg_lower = str(exc).lower()
                if "can't parse entities" in msg_lower:
                    logger.warning("HTML parse failed, sending plain text: %s", exc)
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    sent = await self._send_plain_with_retry(
                        chat_id=chat_id,
                        bot=bot,
                        text=plain,
                        disable_notification=disable_notification,
                        reply_to_message_id=reply_to_message_id if index == 0 else None,
                    )
                    sent_messages.append(sent)
                    continue
                if "too long" in msg_lower:
                    logger.warning(
                        "Chunk too long (%d chars), re-splitting as plain text: %s",
                        len(chunk), exc,
                    )
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    sub_chunks = split_message(plain)
                    for sub_index, sub in enumerate(sub_chunks):
                        sent = await self._send_plain_with_retry(
                            chat_id=chat_id,
                            bot=bot,
                            text=sub,
                            disable_notification=disable_notification,
                            reply_to_message_id=(
                                reply_to_message_id
                                if index == 0 and sub_index == 0
                                else None
                            ),
                        )
                        sent_messages.append(sent)
                    continue
                raise
        return sent_messages

    async def _flush_turn(
        self,
        *,
        chat_id: int,
        bot,
        turn_items: list[TextEvent | StatusEvent],
        jsonl_uuid: str | None = None,
        deferred_bindings: list[tuple[int, str, str]] | None = None,
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
        sent_messages = await self._send_html(
            chat_id=chat_id,
            bot=bot,
            html_text=html_text,
            disable_notification=True,
        )
        session_id = self._session_manager.session_id
        if not jsonl_uuid:
            logger.info(
                "[flush_turn] not mapping turn jsonl_uuid=%s session_id=%s turn_items=%d",
                jsonl_uuid,
                session_id,
                len(turn_items),
            )
            return
        for sent in sent_messages:
            message_id = self._sent_message_id(sent)
            if message_id is None:
                continue
            if session_id:
                self._record_message_binding(
                    message_id=message_id,
                    jsonl_uuid=jsonl_uuid,
                    session_id=session_id,
                    role="assistant",
                )
            elif deferred_bindings is not None:
                deferred_bindings.append((message_id, jsonl_uuid, "assistant"))

    async def _run_and_send(
        self,
        *,
        user_text: str,
        chat_id: int,
        bot,
        trigger_message: QueuedMessage | None = None,
        extra_pending: list[QueuedMessage] | None = None,
        trigger_status_message_ids: list[int] | None = None,
    ) -> None:
        pending_messages = list(self._pending_messages)
        if extra_pending:
            pending_messages.extend(extra_pending)

        proceed, reply_to_message_id = await self._resolve_session_for_trigger(
            trigger_message=trigger_message,
            chat_id=chat_id,
            bot=bot,
        )
        if not proceed:
            return

        runner = ConversationRunner(
            self._session_manager,
            self._hook_state,
            self._config,
            pending_messages=pending_messages,
        )

        turn_items: list[TextEvent | StatusEvent] = []
        deferred_bindings: list[tuple[int, str, str]] = []
        completion_sent = False
        event_count = 0
        turn_count = 0
        latest_turn_uuid: str | None = None
        self._busy_chats.add(chat_id)
        trigger_status_ids = list(trigger_status_message_ids or [])
        logger.info(
            "[run_and_send] START chat=%d msg=%s",
            chat_id, user_text[:80],
        )

        try:
            working_message = await self._send_system_message(
                chat_id=chat_id,
                bot=bot,
                text="working",
                disable_notification=True,
                reply_to_message_id=reply_to_message_id,
            )
            working_message_id = self._sent_message_id(working_message)
            if working_message_id is not None:
                trigger_status_ids.append(working_message_id)
            async for event in runner.run(user_text):
                event_count += 1
                self._flush_deferred_bindings(
                    deferred_bindings=deferred_bindings,
                    session_id=self._session_manager.session_id,
                )
                self._register_main_session_if_needed()
                if isinstance(event, TextEvent | StatusEvent):
                    turn_items.append(event)
                    continue

                if isinstance(event, TurnEndEvent):
                    if (
                        event.jsonl_uuid
                        and event.message_role == "user"
                        and trigger_message is not None
                        and isinstance(trigger_message.telegram_message_id, int)
                    ):
                        self._record_or_defer_message_bindings(
                            message_ids=[
                                trigger_message.telegram_message_id,
                                *trigger_status_ids,
                            ],
                            jsonl_uuid=event.jsonl_uuid,
                            role="user",
                            deferred_bindings=deferred_bindings,
                        )
                        if self._session_manager.session_id:
                            self._session_heads[self._session_manager.session_id] = event.jsonl_uuid
                        latest_turn_uuid = event.jsonl_uuid
                        trigger_status_ids.clear()

                    turn_count += 1
                    logger.info(
                        "[run_and_send] TurnEnd #%d items=%d chat=%d",
                        turn_count, len(turn_items), chat_id,
                    )
                    mapped_uuid = (
                        event.jsonl_uuid
                        if event.jsonl_uuid and turn_items
                        else None
                    )
                    if mapped_uuid and trigger_status_ids:
                        self._record_or_defer_message_bindings(
                            message_ids=trigger_status_ids,
                            jsonl_uuid=mapped_uuid,
                            role="assistant",
                            deferred_bindings=deferred_bindings,
                        )
                        trigger_status_ids.clear()
                    if mapped_uuid and self._session_manager.session_id:
                        self._session_heads[self._session_manager.session_id] = mapped_uuid
                    if mapped_uuid:
                        latest_turn_uuid = mapped_uuid
                    await self._flush_turn(
                        chat_id=chat_id,
                        bot=bot,
                        turn_items=turn_items,
                        jsonl_uuid=mapped_uuid,
                        deferred_bindings=deferred_bindings,
                    )
                    turn_items.clear()
                    continue

                if isinstance(event, DoneEvent):
                    logger.info(
                        "[run_and_send] DoneEvent events=%d turns=%d chat=%d",
                        event_count, turn_count, chat_id,
                    )
                    # Safety flush in case a producer emitted text without TurnEndEvent.
                    if turn_items:
                        await self._flush_turn(
                            chat_id=chat_id,
                            bot=bot,
                            turn_items=turn_items,
                            deferred_bindings=deferred_bindings,
                        )
                        turn_items.clear()

                    self._flush_deferred_bindings(
                        deferred_bindings=deferred_bindings,
                        session_id=self._session_manager.session_id,
                    )
                    self._register_main_session_if_needed()
                    self._pending_messages = runner.remaining_pending
                    if self._has_queue_idle_state(self._pending_messages):
                        summary_message = await self._send_system_message(
                            chat_id=chat_id,
                            bot=bot,
                            text=self._build_completion_summary(),
                            disable_notification=False,
                            reply_to_message_id=reply_to_message_id,
                        )
                        summary_message_id = self._sent_message_id(summary_message)
                        latest_uuid = latest_turn_uuid or self._session_heads.get(
                            self._session_manager.session_id or ""
                        )
                        if (
                            summary_message_id is not None
                            and latest_uuid
                        ):
                            self._record_or_defer_message_bindings(
                                message_ids=[summary_message_id],
                                jsonl_uuid=latest_uuid,
                                role="assistant",
                                deferred_bindings=deferred_bindings,
                            )
                        else:
                            logger.info(
                                "[summary_map] skipped message_id=%s latest_uuid=%s session_id=%s",
                                summary_message_id,
                                latest_uuid,
                                self._session_manager.session_id,
                            )
                        completion_sent = True
                    break

            self._pending_messages = runner.remaining_pending
            self._flush_deferred_bindings(
                deferred_bindings=deferred_bindings,
                session_id=self._session_manager.session_id,
            )
            self._register_main_session_if_needed()

            if not completion_sent:
                summary_message = await self._send_system_message(
                    chat_id=chat_id,
                    bot=bot,
                    text=self._build_completion_summary(),
                    disable_notification=False,
                    reply_to_message_id=reply_to_message_id,
                )
                summary_message_id = self._sent_message_id(summary_message)
                latest_uuid = latest_turn_uuid or self._session_heads.get(
                    self._session_manager.session_id or ""
                )
                if (
                    summary_message_id is not None
                    and latest_uuid
                ):
                    self._record_or_defer_message_bindings(
                        message_ids=[summary_message_id],
                        jsonl_uuid=latest_uuid,
                        role="assistant",
                        deferred_bindings=deferred_bindings,
                    )
                else:
                    logger.info(
                        "[summary_map] skipped message_id=%s latest_uuid=%s session_id=%s",
                        summary_message_id,
                        latest_uuid,
                        self._session_manager.session_id,
                    )
                completion_sent = True

        except Exception as exc:
            logger.exception("Error in ConversationRunner")

            error_detail = f"{type(exc).__name__}: {exc}"
            if len(error_detail) > 200:
                error_detail = error_detail[:200] + "..."

            try:
                await self._send_system_message(
                    chat_id=chat_id,
                    bot=bot,
                    text=f"error: {error_detail}",
                    disable_notification=False,
                    reply_to_message_id=reply_to_message_id,
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
            # Always send the final completion summary so Telegram collectors
            # have a stable end-of-turn marker even on error paths.
            if not completion_sent:
                try:
                    summary_message = await self._send_system_message(
                        chat_id=chat_id,
                        bot=bot,
                        text=self._build_completion_summary(),
                        disable_notification=False,
                        reply_to_message_id=reply_to_message_id,
                    )
                    summary_message_id = self._sent_message_id(summary_message)
                    latest_uuid = latest_turn_uuid or self._session_heads.get(
                        self._session_manager.session_id or ""
                    )
                    if (
                        summary_message_id is not None
                        and latest_uuid
                    ):
                        self._record_or_defer_message_bindings(
                            message_ids=[summary_message_id],
                            jsonl_uuid=latest_uuid,
                            role="assistant",
                            deferred_bindings=deferred_bindings,
                        )
                    else:
                        logger.info(
                            "[summary_map] skipped message_id=%s latest_uuid=%s session_id=%s",
                            summary_message_id,
                            latest_uuid,
                            self._session_manager.session_id,
                        )
                except Exception:
                    logger.debug("Failed to send completion summary in finally", exc_info=True)

    async def _process_message(
        self,
        user_text: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        pre_sent_status_message_ids: list[int] | None = None,
        user_warnings: list[str] | None = None,
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
        reply_to_message_id = None
        reply_to_message = update.effective_message.reply_to_message
        candidate_reply_id = getattr(reply_to_message, "message_id", None)
        if isinstance(candidate_reply_id, int):
            reply_to_message_id = candidate_reply_id
        incoming = QueuedMessage(
            text=user_text,
            telegram_message_id=update.effective_message.message_id,
            reply_to_message_id=reply_to_message_id,
        )
        trigger_status_message_ids: list[int] = list(pre_sent_status_message_ids or [])
        if not trigger_status_message_ids:
            try:
                trigger_status_message_ids.extend(
                    await self._send_received_marker(
                        chat_id=chat_id,
                        bot=context.bot,
                        reply_to_message_id=incoming.telegram_message_id,
                    )
                )
            except Exception:
                logger.debug("Failed to send receipt marker", exc_info=True)
        for warning in user_warnings or []:
            try:
                warning_message = await self._send_system_message(
                    chat_id=chat_id,
                    bot=context.bot,
                    text=warning,
                    disable_notification=True,
                    reply_to_message_id=incoming.telegram_message_id,
                )
                warning_message_id = self._sent_message_id(warning_message)
                if warning_message_id is not None:
                    trigger_status_message_ids.append(warning_message_id)
            except Exception:
                logger.debug("Failed to send normalization warning", exc_info=True)
        if lock.locked() or chat_id in self._busy_chats:
            self._hook_state.message_queue.put_nowait(incoming)
            logger.info("[process_message] queued while busy chat=%d", chat_id)
            return
        async with lock:
            logger.info("[process_message] lock acquired chat=%d", chat_id)
            await self._run_and_send(
                user_text=user_text,
                chat_id=chat_id,
                bot=context.bot,
                trigger_message=incoming,
                trigger_status_message_ids=trigger_status_message_ids,
            )
            logger.info("[process_message] _run_and_send returned chat=%d", chat_id)

    async def _process_media_group(
        self,
        updates: list[Update],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not updates:
            return
        first_message = updates[0].effective_message
        first_user = updates[0].effective_user
        receipt_message_ids: list[int] = []
        if first_message is not None and first_user is not None and first_message.media_group_id is not None:
            media_group_key = (
                first_message.chat_id,
                first_user.id,
                str(first_message.media_group_id),
            )
            receipt_message_ids = self._media_group_receipt_ids.pop(media_group_key, [])
        normalized = await self._normalizer.normalize_media_group(updates)
        if not normalized.agent_text.strip():
            logger.info("Skipping empty normalized Telegram media group")
            return
        await self._process_message(
            normalized.agent_text,
            updates[0],
            context,
            pre_sent_status_message_ids=receipt_message_ids,
            user_warnings=normalized.user_warnings,
        )

    async def _background_poller_loop(self) -> None:
        """Poll for queued background messages and auto-deliver when idle."""
        while True:
            try:
                await asyncio.sleep(self._background_poll_seconds)

                chat_id = self._last_chat_id
                bot = self._last_bot
                if chat_id is None or bot is None:
                    continue
                await self._maybe_send_main_session_warning(chat_id=chat_id, bot=bot)
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
                        trigger_message=(
                            (queued or self._pending_messages)[-1]
                            if (queued or self._pending_messages)
                            else None
                        ),
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
    inbound_filter = (
        filters.TEXT
        | filters.CAPTION
        | filters.PHOTO
        | filters.VIDEO
        | filters.Document.ALL
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.ANIMATION
        | filters.ATTACHMENT
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(inbound_filter, bot.handle_message))
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
    await tg_bot.initialize_runtime()
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
