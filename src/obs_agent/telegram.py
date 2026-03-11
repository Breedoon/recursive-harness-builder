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
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from croniter import croniter
except Exception:  # pragma: no cover - optional import error surfaced on cron create
    croniter = None

from telegram import Bot, Update
from telegram.constants import ChatAction, ParseMode
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
from obs_agent.context_jsonl import find_session_jsonl
from obs_agent.jsonl_fork import fork_session_jsonl
from obs_agent.queueing import QueuedMessage, coerce_queued_message
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent, TurnEndEvent
from obs_agent.session import SessionManager
from obs_agent.telegram_format import md_to_telegram_html, split_message
from obs_agent.telegram_ingest import TelegramInboundNormalizer
from obs_agent.telegram_state_store import TelegramStateStore

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.telegram")

# Delay between sending split message chunks (Telegram rate limit: ~1 msg/sec/chat)
_CHUNK_DELAY_SECONDS = 1.0
_TRANSPORT_BASE_CHAT_INTERVAL_SECONDS = 0.35
_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS = 5.0
_TYPING_ACTION_INTERVAL_SECONDS = 4.0
_OBSERVABILITY_COALESCE_SECONDS = 1.5

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

# AgentTask lifecycle visibility cadence (parent-topic notifications).
_SUPER_TASK_HEARTBEAT_SECONDS = 15.0
_SUPER_TASK_IDLE_SECONDS = 30.0
_SUPER_TASK_MONITOR_TICK_SECONDS = 1.0
_SCHEDULE_STOP_SUPPRESS_SECONDS = 3.0
_SCHEDULE_STOP_MAX_DEFERS = 5

# Prompt used when auto-delivering queued background results while user is idle.
_AUTO_DELIVERY_PROMPT = (
    "(System: queued updates arrived while idle. Process and summarize them.)"
)

_PRIORITY_SYSTEM = 0
_PRIORITY_ASSISTANT = 10
_PRIORITY_OBSERVABILITY = 30
_TOPIC_CREATE_MAX_ATTEMPTS = 3
_TASK_LAUNCH_SEND_MAX_ATTEMPTS = 3


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


def _path_is_within(path: Path, parent: Path) -> bool:
    normalized_path = path.expanduser().resolve(strict=False)
    normalized_parent = parent.expanduser().resolve(strict=False)
    return normalized_path == normalized_parent or normalized_parent in normalized_path.parents


# ---------------------------------------------------------------------------
# FragmentBuffer: reassemble user messages auto-split by Telegram
# ---------------------------------------------------------------------------

@dataclass
class _PendingFragment:
    """A fragment of a split user message waiting for more parts."""

    chat_id: int
    user_id: int
    thread_id: int | None
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
    thread_id: int | None
    media_group_id: str
    updates: list[Update] = field(default_factory=list)
    last_seen: float = 0.0
    context: ContextTypes.DEFAULT_TYPE | None = None


@dataclass(frozen=True)
class TelegramRoute:
    """Logical Telegram delivery route: one chat + optional topic thread."""

    chat_id: int
    thread_id: int | None = None


@dataclass(frozen=True)
class _TelegramMessageBinding:
    """Maps a Telegram message to a JSONL UUID in a specific session."""

    jsonl_uuid: str
    session_id: str
    role: str
    route: TelegramRoute


@dataclass(frozen=True)
class _ObservabilityChunk:
    """Buffered observability-only render fragment with optional binding data."""

    html_text: str
    jsonl_uuid: str | None
    session_id: str | None


@dataclass
class TelegramSessionState:
    """Runtime state for one Telegram route (DM or forum topic)."""

    route: TelegramRoute
    hook_state: HookState
    session_manager: SessionManager
    pending_messages: list[QueuedMessage] = field(default_factory=list)
    busy: bool = False
    last_bot: object | None = None
    topic_title: str | None = None
    topic_icon_custom_emoji_id: str | None = None
    warning_sent: bool = False
    child_fork_count: int = 0
    child_fork_base_title: str | None = None
    notify_on_completion: bool = True
    active_fork_task_ids: set[str] = field(default_factory=set)

    @property
    def session_id(self) -> str | None:
        return self.session_manager.session_id


@dataclass
class _ForkTaskRecord:
    """In-memory lifecycle record for an agent-launched child topic."""

    task_id: str
    parent_route: TelegramRoute
    parent_session_id_at_launch: str
    parent_source_uuid: str
    child_route: TelegramRoute
    child_session_id: str
    prompt: str
    description: str | None = None
    timeout_ms: int | None = None
    max_turns: int | None = None
    status: str = "launched"
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    launch_parent_message_id: int | None = None
    launch_child_message_id: int | None = None
    child_completion_message_id: int | None = None
    parent_callback_message_id: int | None = None
    error: str | None = None
    terminal_request: str | None = None
    result_text: str | None = None
    tool_use_id: str | None = None
    usage_total_tokens: int | None = None
    usage_tool_uses: int | None = None
    usage_duration_ms: int | None = None
    is_fork: bool = True
    launch_tool_name: str = "ForkTask"
    team_name: str | None = None
    agent_name: str | None = None
    idle_ready: bool = False
    wake_requested: bool = False
    wake_source_sender: str | None = None
    wake_source_summary: str | None = None
    wake_source_content: str | None = None
    emit_parent_callback: bool = True


@dataclass
class _TopicScheduleRecord:
    schedule_id: str
    route: TelegramRoute
    description: str | None
    schedule_mode: str  # interval | cron
    cron_expr: str | None
    trigger_kind: str  # interval | cron | on_topic_stop
    interval_seconds: int | None
    prompt: str
    reset_session: bool = False
    recurring: bool = True
    enabled: bool = True
    run_count: int = 0
    max_runs: int | None = None
    from_ts: float | None = None
    until_ts: float | None = None
    inherit_mode: str = "none"
    next_run_at: float | None = None
    last_run_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    max_retry_attempts: int = 0
    retry_delay_seconds: int = 30
    retry_attempt_count: int = 0


@dataclass(frozen=True)
class _DefaultScheduleTemplate:
    schedule_mode: str
    cron_expr: str | None
    interval_seconds: int | None
    prompt: str
    reset_session: bool
    recurring: bool
    description: str | None
    max_runs: int | None
    from_ts: float | None
    until_ts: float | None
    inherit_mode: str
    max_retry_attempts: int
    retry_delay_seconds: int


@dataclass(frozen=True)
class _RunOutcome:
    """Observable outcome for one route run used by ForkTask bookkeeping."""

    assistant_text: str
    failed: bool = False
    error: str | None = None


@dataclass
class _TransportSendOp:
    route: TelegramRoute
    text: str
    disable_notification: bool
    parse_mode: ParseMode | None
    reply_to_message_id: int | None
    fallback_bot: Any | None
    max_attempts: int | None
    future: asyncio.Future


@dataclass
class _TransportDeleteTopicOp:
    route: TelegramRoute
    fallback_bot: Any | None
    future: asyncio.Future


@dataclass
class _TransportCreateTopicOp:
    route: TelegramRoute
    name: str
    icon_custom_emoji_id: str | None
    fallback_bot: Any | None
    future: asyncio.Future


@dataclass
class _TopicMetadata:
    title: str | None = None
    icon_custom_emoji_id: str | None = None


@dataclass(order=True)
class _TransportEnvelope:
    priority: int
    sequence: int
    op: _TransportSendOp | _TransportDeleteTopicOp | _TransportCreateTopicOp = field(compare=False)


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
        self._pending: dict[tuple[int, int, int | None], _PendingFragment] = {}
        self._flush_events: dict[tuple[int, int, int | None], asyncio.Event] = {}

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
        key = (chat_id, user_id, getattr(update.effective_message, "message_thread_id", None))

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
            thread_id=getattr(update.effective_message, "message_thread_id", None),
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

    async def _flush(self, key: tuple[int, int, int | None]) -> None:
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
        self._pending: dict[tuple[int, int, int | None, str], _PendingMediaGroup] = {}
        self._flush_events: dict[tuple[int, int, int | None, str], asyncio.Event] = {}

    async def add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None or update.effective_user is None:
            return
        media_group_id = update.effective_message.media_group_id
        if media_group_id is None:
            return

        key = (
            update.effective_message.chat_id,
            update.effective_user.id,
            getattr(update.effective_message, "message_thread_id", None),
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
            thread_id=getattr(update.effective_message, "message_thread_id", None),
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

    async def _flush(self, key: tuple[int, int, int | None, str]) -> None:
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
        super_task_heartbeat_seconds: float = _SUPER_TASK_HEARTBEAT_SECONDS,
        super_task_idle_seconds: float = _SUPER_TASK_IDLE_SECONDS,
        super_task_monitor_tick_seconds: float = _SUPER_TASK_MONITOR_TICK_SECONDS,
    ) -> None:
        self._config = config
        if _path_is_within(config.telegram_state_db_path, config.telegram_temp_root):
            raise ValueError(
                "Invalid Telegram state DB path: OBS_TELEGRAM_STATE_DB_PATH must be outside "
                "OBS_TELEGRAM_TEMP_ROOT to avoid startup cleanup deleting persistence data."
            )
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

        self._route_locks: dict[TelegramRoute, asyncio.Lock] = {}
        self._states_by_route: dict[TelegramRoute, TelegramSessionState] = {}
        self._route_by_session_id: dict[str, TelegramRoute] = {}
        self._message_map: dict[tuple[int, int], _TelegramMessageBinding] = {}
        self._session_heads: dict[str, str] = {}
        self._fork_tasks_by_id: dict[str, _ForkTaskRecord] = {}
        self._fork_task_by_child_route: dict[TelegramRoute, str] = {}
        self._fork_task_tasks: dict[str, asyncio.Task] = {}
        self._team_worker_records: dict[tuple[str, str], str] = {}
        self._topic_schedules_by_id: dict[str, _TopicScheduleRecord] = {}
        self._schedule_ids_by_route: dict[TelegramRoute, set[str]] = {}
        self._schedule_running_by_route: set[TelegramRoute] = set()
        self._active_schedule_execution_by_route: dict[TelegramRoute, str] = {}
        self._schedule_stop_events: asyncio.Queue[tuple[TelegramRoute, dict[str, Any]]] = asyncio.Queue()
        self._schedule_stop_suppress_until: dict[TelegramRoute, float] = {}
        self._primary_bot: Any | None = None
        self._media_group_receipt_ids: dict[tuple[int, int, int | None, str], list[int]] = {}
        self._settings_payload = self._load_settings_payload()
        self._schedule_retry_max_attempts, self._schedule_retry_delay_seconds = (
            self._load_schedule_retry_policy(self._settings_payload)
        )
        self._default_schedule_template = self._load_default_schedule_template(self._settings_payload)

        self._enable_background_poller = enable_background_poller
        self._background_poll_seconds = background_poll_seconds
        self._super_task_heartbeat_seconds = max(float(super_task_heartbeat_seconds), 0.05)
        self._super_task_idle_seconds = max(
            float(super_task_idle_seconds),
            self._super_task_heartbeat_seconds,
        )
        self._super_task_monitor_tick_seconds = max(
            float(super_task_monitor_tick_seconds),
            0.01,
        )
        self._background_task: asyncio.Task | None = None
        self._transport_queue: asyncio.PriorityQueue[_TransportEnvelope] = asyncio.PriorityQueue()
        self._transport_sequence = 0
        self._transport_worker_task: asyncio.Task | None = None
        self._chat_next_send_at: dict[int, float] = {}
        self._chat_send_interval: dict[int, float] = {}
        self._chat_pending_ops: dict[int, int] = {}
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._sender_bots: list[Bot] = []
        self._sender_rr_by_chat: dict[int, int] = {}
        self._sender_chat_blacklist: dict[int, set[int]] = {}
        self._observability_buffer: dict[TelegramRoute, list[_ObservabilityChunk]] = {}
        self._observability_flush_tasks: dict[TelegramRoute, asyncio.Task] = {}
        self._system_message_ids: set[tuple[int, int]] = set()
        self._system_message_routes: dict[tuple[int, int], TelegramRoute] = {}
        self._last_inbound_message_id_by_route: dict[TelegramRoute, int] = {}
        self._topic_metadata_by_route: dict[TelegramRoute, _TopicMetadata] = {}
        self._state_store = TelegramStateStore(config.telegram_state_db_path)
        self._state_store.initialize()

    async def initialize_runtime(self) -> None:
        """Purge the Telegram temp root once and create a fresh workspace."""
        self._normalizer.initialize()
        self._state_store.initialize()
        self._state_store.prune(
            retention_days=self._config.telegram_state_retention_days
        )
        self._restore_state_from_store()
        await self._ensure_transport_worker()
        await self._ensure_background_poller(None)

    def _next_transport_sequence(self) -> int:
        self._transport_sequence += 1
        return self._transport_sequence

    async def _ensure_sender_pool(self) -> None:
        if self._sender_bots:
            return
        tokens = self._config.telegram_sender_bot_tokens
        if len(tokens) <= 1:
            return
        # Token[0] is the polling bot handled by python-telegram-bot Application.
        for token in tokens[1:]:
            try:
                self._sender_bots.append(Bot(token=token))
            except Exception:
                logger.warning("Failed creating Telegram sender bot from token", exc_info=True)

    async def _ensure_transport_worker(self) -> None:
        await self._ensure_sender_pool()
        if self._transport_worker_task is not None and not self._transport_worker_task.done():
            return
        self._transport_worker_task = asyncio.create_task(self._transport_worker_loop())

    async def _maybe_stop_transport_worker(self) -> None:
        task = self._transport_worker_task
        if task is None or task.done():
            self._transport_worker_task = None
            return
        if not self._transport_queue.empty():
            return
        if self._chat_pending_ops:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._transport_worker_task = None

    def _increment_pending_chat_ops(self, chat_id: int) -> None:
        self._chat_pending_ops[chat_id] = self._chat_pending_ops.get(chat_id, 0) + 1
        task = self._typing_tasks.get(chat_id)
        if task is None or task.done():
            self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _decrement_pending_chat_ops(self, chat_id: int) -> None:
        remaining = self._chat_pending_ops.get(chat_id, 0) - 1
        if remaining <= 0:
            self._chat_pending_ops.pop(chat_id, None)
            return
        self._chat_pending_ops[chat_id] = remaining

    def _sender_candidates(self, *, fallback_bot: Any | None, chat_id: int) -> list[Any]:
        candidates: list[Any] = []
        if fallback_bot is not None:
            candidates.append(fallback_bot)
        for sender in self._sender_bots:
            if all(id(existing) != id(sender) for existing in candidates):
                candidates.append(sender)
        blocked = self._sender_chat_blacklist.get(chat_id, set())
        if blocked:
            candidates = [candidate for candidate in candidates if id(candidate) not in blocked]
        if not candidates and fallback_bot is not None:
            candidates.append(fallback_bot)
        if len(candidates) <= 1:
            return candidates
        start = self._sender_rr_by_chat.get(chat_id, 0) % len(candidates)
        self._sender_rr_by_chat[chat_id] = start + 1
        return candidates[start:] + candidates[:start]

    async def _wait_for_chat_send_slot(self, chat_id: int) -> None:
        next_allowed = self._chat_next_send_at.get(chat_id, 0.0)
        now = time.monotonic()
        if next_allowed > now:
            await asyncio.sleep(next_allowed - now)
            now = time.monotonic()
        interval = self._chat_send_interval.get(chat_id, _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS)
        self._chat_next_send_at[chat_id] = now + interval

    def _note_chat_send_success(self, chat_id: int) -> None:
        current = self._chat_send_interval.get(chat_id, _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS)
        if current <= _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS:
            self._chat_send_interval[chat_id] = _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS
            return
        # Recover gradually after temporary flood waits.
        self._chat_send_interval[chat_id] = max(
            _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS,
            current * 0.92,
        )

    def _note_chat_retry_after(self, chat_id: int, delay: float) -> None:
        current = self._chat_send_interval.get(chat_id, _TRANSPORT_BASE_CHAT_INTERVAL_SECONDS)
        boosted = max(current * 1.4, min(_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS, max(1.0, delay / 10.0)))
        self._chat_send_interval[chat_id] = min(_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS, boosted)

    async def _execute_send_with_retry(self, op: _TransportSendOp):
        candidates = self._sender_candidates(fallback_bot=op.fallback_bot, chat_id=op.route.chat_id)
        if not candidates:
            raise RuntimeError("No Telegram sender bot available")

        attempt = 0
        last_error: Exception | None = None
        max_attempts = op.max_attempts if isinstance(op.max_attempts, int) and op.max_attempts > 0 else None
        while True:
            for candidate in candidates:
                await self._wait_for_chat_send_slot(op.route.chat_id)
                try:
                    sent = await candidate.send_message(
                        chat_id=op.route.chat_id,
                        text=op.text,
                        parse_mode=op.parse_mode,
                        disable_web_page_preview=True,
                        disable_notification=op.disable_notification,
                        reply_to_message_id=op.reply_to_message_id,
                        message_thread_id=op.route.thread_id,
                    )
                    self._note_chat_send_success(op.route.chat_id)
                    return sent
                except RetryAfter as exc:
                    delay = max(float(exc.retry_after), 1.0)
                    attempt += 1
                    if max_attempts is not None and attempt >= max_attempts:
                        raise
                    self._chat_next_send_at[op.route.chat_id] = max(
                        self._chat_next_send_at.get(op.route.chat_id, 0.0),
                        time.monotonic() + delay,
                    )
                    self._note_chat_retry_after(op.route.chat_id, delay)
                    logger.warning(
                        "Telegram rate limit route=%s attempt=%d retry_in=%.1fs",
                        op.route, attempt, delay,
                    )
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
                except BadRequest as exc:
                    msg_lower = str(exc).lower()
                    # Payload/HTML errors are handled by the caller.
                    if "can't parse entities" in msg_lower or "too long" in msg_lower:
                        raise
                    if (
                        op.reply_to_message_id is not None
                        and (
                            "message to be replied not found" in msg_lower
                            or "reply message not found" in msg_lower
                        )
                    ):
                        logger.info(
                            "Telegram reply target missing route=%s reply_to=%s; retrying without reply",
                            op.route,
                            op.reply_to_message_id,
                        )
                        op.reply_to_message_id = None
                        continue
                    if (
                        "not enough rights" in msg_lower
                        or "chat not found" in msg_lower
                        or "bot is not a member" in msg_lower
                    ):
                        self._sender_chat_blacklist.setdefault(op.route.chat_id, set()).add(id(candidate))
                        last_error = exc
                        continue
                    raise
                except TelegramError as exc:
                    attempt += 1
                    if max_attempts is not None and attempt >= max_attempts:
                        raise
                    delay = min(30.0, 2 ** min(attempt, 5))
                    logger.warning(
                        "Telegram send failed route=%s attempt=%d retry_in=%.1fs: %s",
                        op.route, attempt, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
            else:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("All Telegram sender candidates failed")

    async def _execute_delete_topic_with_retry(self, op: _TransportDeleteTopicOp) -> None:
        if op.route.thread_id is None:
            raise RuntimeError("Cannot delete General topic via transport delete")
        # Topic lifecycle operations require elevated chat permissions.
        # Keep them pinned to the primary polling bot to avoid sender-pool
        # members lacking admin rights (which causes noisy 400 failures).
        candidates = [op.fallback_bot] if op.fallback_bot is not None else []
        if not candidates:
            candidates = self._sender_candidates(fallback_bot=op.fallback_bot, chat_id=op.route.chat_id)
        if not candidates:
            raise RuntimeError("No Telegram sender bot available")
        attempt = 0
        last_error: Exception | None = None
        while True:
            for candidate in candidates:
                await self._wait_for_chat_send_slot(op.route.chat_id)
                try:
                    await candidate.delete_forum_topic(
                        chat_id=op.route.chat_id,
                        message_thread_id=op.route.thread_id,
                    )
                    self._note_chat_send_success(op.route.chat_id)
                    return
                except RetryAfter as exc:
                    delay = max(float(exc.retry_after), 1.0)
                    attempt += 1
                    self._chat_next_send_at[op.route.chat_id] = max(
                        self._chat_next_send_at.get(op.route.chat_id, 0.0),
                        time.monotonic() + delay,
                    )
                    self._note_chat_retry_after(op.route.chat_id, delay)
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
                except TelegramError as exc:
                    attempt += 1
                    delay = min(30.0, 2 ** min(attempt, 5))
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
            else:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("All Telegram sender candidates failed for delete")

    async def _execute_create_topic_with_retry(self, op: _TransportCreateTopicOp):
        # Topic lifecycle operations require elevated chat permissions.
        # Keep them pinned to the primary polling bot to avoid sender-pool
        # members lacking admin rights (which causes noisy 400 failures).
        candidates = [op.fallback_bot] if op.fallback_bot is not None else []
        if not candidates:
            candidates = self._sender_candidates(fallback_bot=op.fallback_bot, chat_id=op.route.chat_id)
        if not candidates:
            raise RuntimeError("No Telegram sender bot available")
        attempt = 0
        last_error: Exception | None = None
        while True:
            for candidate in candidates:
                await self._wait_for_chat_send_slot(op.route.chat_id)
                try:
                    topic = await candidate.create_forum_topic(
                        chat_id=op.route.chat_id,
                        name=op.name,
                        icon_custom_emoji_id=op.icon_custom_emoji_id,
                    )
                    self._note_chat_send_success(op.route.chat_id)
                    return topic
                except RetryAfter as exc:
                    delay = max(float(exc.retry_after), 1.0)
                    attempt += 1
                    if attempt >= _TOPIC_CREATE_MAX_ATTEMPTS:
                        raise RuntimeError(
                            f"Failed to create forum topic '{op.name}' in chat {op.route.chat_id} "
                            f"after {attempt} attempts: {exc}"
                        ) from exc
                    self._chat_next_send_at[op.route.chat_id] = max(
                        self._chat_next_send_at.get(op.route.chat_id, 0.0),
                        time.monotonic() + delay,
                    )
                    self._note_chat_retry_after(op.route.chat_id, delay)
                    logger.warning(
                        "Telegram create_forum_topic rate limit route=%s attempt=%d retry_in=%.1fs",
                        op.route,
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
                except TelegramError as exc:
                    attempt += 1
                    if isinstance(exc, BadRequest):
                        raise RuntimeError(
                            f"Failed to create forum topic '{op.name}' in chat {op.route.chat_id}: {exc}. "
                            "AgentTask/ForkTask cannot continue without creating the child topic."
                        ) from exc
                    if attempt >= _TOPIC_CREATE_MAX_ATTEMPTS:
                        raise RuntimeError(
                            f"Failed to create forum topic '{op.name}' in chat {op.route.chat_id} "
                            f"after {attempt} attempts: {exc}"
                        ) from exc
                    delay = min(30.0, 2 ** min(attempt, 5))
                    logger.warning(
                        "Telegram create_forum_topic failed route=%s attempt=%d retry_in=%.1fs: %s",
                        op.route,
                        attempt,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    last_error = exc
                    break
            else:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("All Telegram sender candidates failed for create_topic")

    async def _enqueue_send(
        self,
        *,
        route: TelegramRoute,
        text: str,
        disable_notification: bool,
        parse_mode: ParseMode | None,
        reply_to_message_id: int | None,
        fallback_bot: Any | None,
        priority: int,
        max_attempts: int | None = None,
    ):
        await self._ensure_transport_worker()
        future = asyncio.get_running_loop().create_future()
        self._increment_pending_chat_ops(route.chat_id)
        await self._transport_queue.put(
            _TransportEnvelope(
                priority=priority,
                sequence=self._next_transport_sequence(),
                op=_TransportSendOp(
                    route=route,
                    text=text,
                    disable_notification=disable_notification,
                    parse_mode=parse_mode,
                    reply_to_message_id=reply_to_message_id,
                    fallback_bot=fallback_bot,
                    max_attempts=max_attempts,
                    future=future,
                ),
            )
        )
        try:
            return await future
        finally:
            await self._maybe_stop_transport_worker()

    async def _enqueue_delete_topic(
        self,
        *,
        route: TelegramRoute,
        fallback_bot: Any | None,
        priority: int = _PRIORITY_SYSTEM,
    ) -> None:
        await self._ensure_transport_worker()
        future = asyncio.get_running_loop().create_future()
        self._increment_pending_chat_ops(route.chat_id)
        await self._transport_queue.put(
            _TransportEnvelope(
                priority=priority,
                sequence=self._next_transport_sequence(),
                op=_TransportDeleteTopicOp(
                    route=route,
                    fallback_bot=fallback_bot,
                    future=future,
                ),
            )
        )
        try:
            await future
        finally:
            await self._maybe_stop_transport_worker()

    async def _enqueue_create_topic(
        self,
        *,
        route: TelegramRoute,
        topic_name: str,
        icon_custom_emoji_id: str | None = None,
        fallback_bot: Any | None,
        priority: int = _PRIORITY_SYSTEM,
    ):
        await self._ensure_transport_worker()
        future = asyncio.get_running_loop().create_future()
        self._increment_pending_chat_ops(route.chat_id)
        await self._transport_queue.put(
            _TransportEnvelope(
                priority=priority,
                sequence=self._next_transport_sequence(),
                op=_TransportCreateTopicOp(
                    route=route,
                    name=topic_name,
                    icon_custom_emoji_id=icon_custom_emoji_id,
                    fallback_bot=fallback_bot,
                    future=future,
                ),
            )
        )
        try:
            return await future
        finally:
            await self._maybe_stop_transport_worker()

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while self._chat_pending_ops.get(chat_id, 0) > 0:
                sender = None
                if self._sender_bots:
                    sender = self._sender_candidates(
                        fallback_bot=self._sender_bots[0], chat_id=chat_id
                    )[0]
                for state in self._states_by_route.values():
                    if state.route.chat_id == chat_id and state.last_bot is not None:
                        sender = state.last_bot
                        break
                if sender is not None:
                    try:
                        await sender.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    except Exception:
                        logger.debug("typing action failed chat_id=%s", chat_id, exc_info=True)
                remaining = _TYPING_ACTION_INTERVAL_SECONDS
                while remaining > 0:
                    if self._chat_pending_ops.get(chat_id, 0) <= 0:
                        break
                    step = min(0.2, remaining)
                    await asyncio.sleep(step)
                    remaining -= step
        finally:
            self._typing_tasks.pop(chat_id, None)

    async def _transport_worker_loop(self) -> None:
        while True:
            try:
                envelope = await self._transport_queue.get()
                op = envelope.op
                try:
                    if isinstance(op, _TransportSendOp):
                        result = await self._execute_send_with_retry(op)
                    elif isinstance(op, _TransportDeleteTopicOp):
                        result = await self._execute_delete_topic_with_retry(op)
                    else:
                        result = await self._execute_create_topic_with_retry(op)
                    if not op.future.done():
                        op.future.set_result(result)
                except Exception as exc:
                    if not op.future.done():
                        op.future.set_exception(exc)
                finally:
                    chat_id = op.route.chat_id
                    self._decrement_pending_chat_ops(chat_id)
                    self._transport_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Transport worker loop failed")

    def _route_for_message(self, message) -> TelegramRoute:
        thread_id = getattr(message, "message_thread_id", None)
        if not isinstance(thread_id, int):
            reply_to = getattr(message, "reply_to_message", None)
            if reply_to is not None:
                reply_thread = getattr(reply_to, "message_thread_id", None)
                if isinstance(reply_thread, int):
                    thread_id = reply_thread
        if not isinstance(thread_id, int):
            reply_to = getattr(message, "reply_to", None)
            if reply_to is not None and getattr(reply_to, "forum_topic", False):
                top_id = getattr(reply_to, "reply_to_top_id", None)
                if isinstance(top_id, int):
                    thread_id = top_id
                else:
                    reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
                    if isinstance(reply_to_msg_id, int):
                        thread_id = reply_to_msg_id
        return TelegramRoute(chat_id=message.chat_id, thread_id=thread_id if isinstance(thread_id, int) else None)

    def _default_topic_title(self, route: TelegramRoute) -> str:
        if route.thread_id is None:
            return "General"
        return f"Topic {route.thread_id}"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _load_settings_payload(self) -> dict[str, Any] | None:
        settings_path = self._config.claude_path / "settings.json"
        if not settings_path.exists():
            return None
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed parsing settings file at %s", settings_path, exc_info=True)
            return None
        return loaded if isinstance(loaded, dict) else None

    def _load_schedule_retry_policy(self, settings: dict[str, Any] | None) -> tuple[int, int]:
        max_attempts = 0
        delay_seconds = 30
        if not isinstance(settings, dict):
            return max_attempts, delay_seconds
        obs = settings.get("obs")
        if not isinstance(obs, dict):
            return max_attempts, delay_seconds
        scheduling = obs.get("scheduling")
        if not isinstance(scheduling, dict):
            return max_attempts, delay_seconds
        retry = scheduling.get("retry")
        if not isinstance(retry, dict):
            return max_attempts, delay_seconds
        max_attempts = max(self._safe_int(retry.get("max_attempts"), 0), 0)
        delay_seconds = max(self._safe_int(retry.get("delay_seconds"), 30), 1)
        return max_attempts, delay_seconds

    def _parse_default_schedule_template(
        self,
        default_schedule: dict[str, Any],
        *,
        source_label: str,
    ) -> _DefaultScheduleTemplate | None:
        if default_schedule.get("enabled") is False:
            return None
        prompt = str(default_schedule.get("prompt") or "").strip()
        if not prompt:
            logger.warning("Ignoring %s: prompt is required", source_label)
            return None

        schedule_mode = str(default_schedule.get("schedule_mode") or "").strip().lower()
        cron_expr = str(default_schedule.get("cron") or "").strip() or None
        interval_seconds_raw = default_schedule.get("interval_seconds")
        interval_seconds: int | None = None
        if interval_seconds_raw is not None:
            try:
                interval_seconds = int(interval_seconds_raw)
            except (TypeError, ValueError):
                logger.warning("Ignoring %s: interval_seconds must be an integer", source_label)
                return None
            if interval_seconds < 0:
                logger.warning("Ignoring %s: interval_seconds must be non-negative", source_label)
                return None
        if not schedule_mode:
            if interval_seconds is not None:
                schedule_mode = "interval"
            elif cron_expr:
                schedule_mode = "cron"
        if schedule_mode not in {"interval", "cron"}:
            logger.warning("Ignoring %s: schedule_mode must be interval or cron", source_label)
            return None
        if schedule_mode == "interval" and interval_seconds is None:
            logger.warning("Ignoring %s: interval_seconds is required for interval mode", source_label)
            return None
        if schedule_mode == "cron":
            if not cron_expr:
                logger.warning("Ignoring %s: cron is required for cron mode", source_label)
                return None
            if croniter is None:
                logger.warning("Ignoring %s: croniter is required for cron mode", source_label)
                return None
            try:
                self._next_cron_fire_ts(cron_expr=cron_expr, base_ts=time.time())
            except ValueError:
                logger.warning("Ignoring %s: invalid cron expression %s", source_label, cron_expr)
                return None

        reset_session_raw = default_schedule.get("reset_session")
        reset_session = bool(reset_session_raw) if isinstance(reset_session_raw, bool) else False
        if reset_session_raw is None:
            run_mode = str(default_schedule.get("run_mode") or "").strip().lower()
            if run_mode in {"continue", "reset_session"}:
                reset_session = run_mode == "reset_session"
            elif run_mode:
                logger.warning("Ignoring %s: unsupported run_mode %s", source_label, run_mode)
                return None

        max_runs_raw = default_schedule.get("max_runs")
        max_runs: int = 1
        if max_runs_raw is not None:
            try:
                max_runs = int(max_runs_raw)
            except (TypeError, ValueError):
                logger.warning("Ignoring %s: max_runs must be an integer", source_label)
                return None
            if max_runs <= 0:
                logger.warning("Ignoring %s: max_runs must be positive", source_label)
                return None

        from_raw = str(default_schedule.get("from") or "").strip() or None
        until_raw = str(default_schedule.get("until") or "").strip() or None
        try:
            from_ts = self._parse_rfc3339_timestamp(from_raw)
            until_ts = self._parse_rfc3339_timestamp(until_raw)
        except ValueError:
            logger.warning("Ignoring %s: from/until must be RFC3339 timestamp", source_label)
            return None
        if from_ts is not None and until_ts is not None and from_ts > until_ts:
            logger.warning("Ignoring %s: from must be <= until", source_label)
            return None

        inherit_mode = str(default_schedule.get("inherit") or "none").strip().lower() or "none"
        if inherit_mode not in {"none", "fork", "all"}:
            logger.warning("Ignoring %s: inherit must be none, fork, or all", source_label)
            return None

        return _DefaultScheduleTemplate(
            schedule_mode=schedule_mode,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            prompt=prompt,
            reset_session=reset_session,
            recurring=max_runs != 1,
            description=str(default_schedule.get("description") or "").strip() or None,
            max_runs=max_runs,
            from_ts=from_ts,
            until_ts=until_ts,
            inherit_mode=inherit_mode,
            max_retry_attempts=self._schedule_retry_max_attempts,
            retry_delay_seconds=self._schedule_retry_delay_seconds,
        )

    def _load_default_schedule_template(self, settings: dict[str, Any] | None) -> _DefaultScheduleTemplate | None:
        if not isinstance(settings, dict):
            return None

        obs = settings.get("obs")
        if isinstance(obs, dict):
            scheduling = obs.get("scheduling")
            if isinstance(scheduling, dict):
                defaults = scheduling.get("defaults")
                if isinstance(defaults, dict):
                    if defaults.get("auto_create_on_session_start") is False:
                        return None
                    schedule = defaults.get("schedule")
                    if isinstance(schedule, dict):
                        return self._parse_default_schedule_template(
                            schedule,
                            source_label="obs.scheduling.defaults.schedule",
                        )

        obs_agent = settings.get("obs_agent")
        if not isinstance(obs_agent, dict):
            return None
        schedule_defaults = obs_agent.get("schedule_defaults")
        if not isinstance(schedule_defaults, dict):
            return None
        if schedule_defaults.get("auto_create_on_session_start") is False:
            return None
        legacy = schedule_defaults.get("default_interval")
        if not isinstance(legacy, dict):
            return None
        logger.warning(
            "settings.json uses deprecated obs_agent.schedule_defaults; migrate to obs.scheduling.defaults"
        )
        normalized = dict(legacy)
        if "schedule_mode" not in normalized:
            normalized["schedule_mode"] = "interval"
        return self._parse_default_schedule_template(
            normalized,
            source_label="obs_agent.schedule_defaults.default_interval",
        )

    def _maybe_seed_default_schedule(self, *, route: TelegramRoute) -> None:
        template = self._default_schedule_template
        if template is None:
            return
        if self._schedule_ids_by_route.get(route):
            return
        now = time.time()
        trigger_kind = "on_topic_stop"
        if template.schedule_mode == "cron":
            trigger_kind = "cron"
        elif template.interval_seconds and template.interval_seconds > 0:
            trigger_kind = "interval"
        record = _TopicScheduleRecord(
            schedule_id=uuid.uuid4().hex[:8],
            route=route,
            description=template.description,
            schedule_mode=template.schedule_mode,
            cron_expr=template.cron_expr,
            trigger_kind=trigger_kind,
            interval_seconds=template.interval_seconds,
            prompt=template.prompt,
            reset_session=template.reset_session,
            recurring=template.recurring,
            enabled=True,
            run_count=0,
            max_runs=template.max_runs,
            from_ts=template.from_ts,
            until_ts=template.until_ts,
            inherit_mode=template.inherit_mode,
            next_run_at=None,
            max_retry_attempts=template.max_retry_attempts,
            retry_delay_seconds=template.retry_delay_seconds,
            retry_attempt_count=0,
        )
        if record.trigger_kind in {"interval", "cron"}:
            try:
                record.next_run_at = self._next_timed_run_at(record, base_ts=now)
            except ValueError:
                logger.warning(
                    "Skipping default schedule due to invalid timing config route=%s mode=%s",
                    route,
                    record.schedule_mode,
                )
                return
        overlap_error = self._validate_schedule_overlap(
            route=route,
            start_ts=record.from_ts,
            end_ts=record.until_ts,
        )
        if overlap_error:
            logger.warning("Skipping default schedule for %s: %s", route, overlap_error)
            return
        self._register_topic_schedule(record)
        logger.info(
            "Applied default schedule from settings route=%s mode=%s max_runs=%s",
            route,
            template.schedule_mode,
            template.max_runs,
        )

    def _topic_metadata_for_route(self, route: TelegramRoute) -> _TopicMetadata:
        return self._topic_metadata_by_route.get(route, _TopicMetadata())

    def _persist_state_for_route(self, route: TelegramRoute) -> None:
        state = self._states_by_route.get(route)
        if state is None:
            return
        last_inbound = self._last_inbound_message_id_by_route.get(route)
        session_id = state.session_id if isinstance(state.session_id, str) and state.session_id else None
        topic_title = state.topic_title if isinstance(state.topic_title, str) and state.topic_title else None
        topic_icon = (
            state.topic_icon_custom_emoji_id
            if isinstance(state.topic_icon_custom_emoji_id, str) and state.topic_icon_custom_emoji_id
            else None
        )
        child_fork_base_title = (
            state.child_fork_base_title
            if isinstance(state.child_fork_base_title, str) and state.child_fork_base_title
            else None
        )
        self._state_store.upsert_route_state(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            session_id=session_id,
            topic_title=topic_title,
            topic_icon_custom_emoji_id=topic_icon,
            child_fork_count=state.child_fork_count,
            child_fork_base_title=child_fork_base_title,
            notify_on_completion=state.notify_on_completion,
            last_inbound_message_id=last_inbound,
        )

    def _restore_state_from_store(self) -> None:
        snapshot = self._state_store.load_snapshot()
        for entry in snapshot.route_states:
            route = TelegramRoute(chat_id=entry.chat_id, thread_id=entry.thread_id)
            if entry.topic_title or entry.topic_icon_custom_emoji_id:
                self._topic_metadata_by_route[route] = _TopicMetadata(
                    title=entry.topic_title,
                    icon_custom_emoji_id=entry.topic_icon_custom_emoji_id,
                )
            state = self._build_session_state(route, topic_title=entry.topic_title)
            state.topic_title = (
                entry.topic_title
                or state.topic_title
                or self._default_topic_title(route)
            )
            state.topic_icon_custom_emoji_id = entry.topic_icon_custom_emoji_id
            state.child_fork_count = max(entry.child_fork_count, 0)
            state.child_fork_base_title = entry.child_fork_base_title
            state.notify_on_completion = entry.notify_on_completion
            if entry.session_id:
                state.session_manager.set_session_id(entry.session_id)
                self._route_by_session_id[entry.session_id] = route
            self._states_by_route[route] = state
            if entry.last_inbound_message_id is not None:
                self._last_inbound_message_id_by_route[route] = entry.last_inbound_message_id

        for entry in snapshot.message_bindings:
            route = TelegramRoute(
                chat_id=entry.route_chat_id,
                thread_id=entry.route_thread_id,
            )
            self._message_map[(entry.chat_id, entry.message_id)] = _TelegramMessageBinding(
                jsonl_uuid=entry.jsonl_uuid,
                session_id=entry.session_id,
                role=entry.role,
                route=route,
            )

        for entry in snapshot.system_messages:
            key = (entry.chat_id, entry.message_id)
            route = TelegramRoute(
                chat_id=entry.route_chat_id,
                thread_id=entry.route_thread_id,
            )
            self._system_message_ids.add(key)
            self._system_message_routes[key] = route

        self._session_heads.update(snapshot.session_heads)

        for entry in snapshot.topic_schedules:
            route = TelegramRoute(chat_id=entry.chat_id, thread_id=entry.thread_id)
            record = _TopicScheduleRecord(
                schedule_id=entry.schedule_id,
                route=route,
                description=entry.description,
                schedule_mode=entry.schedule_mode,
                cron_expr=entry.cron_expr,
                trigger_kind=entry.trigger_kind,
                interval_seconds=entry.interval_seconds,
                prompt=entry.prompt,
                reset_session=(entry.run_mode == "reset_session"),
                recurring=entry.recurring,
                enabled=entry.enabled,
                run_count=entry.run_count,
                max_runs=entry.max_runs,
                from_ts=entry.from_ts,
                until_ts=entry.until_ts,
                inherit_mode=entry.inherit_mode,
                next_run_at=entry.next_run_at,
                last_run_at=entry.last_run_at,
                last_success_at=entry.last_success_at,
                last_error=entry.last_error,
                max_retry_attempts=entry.max_retry_attempts,
                retry_delay_seconds=entry.retry_delay_seconds,
                retry_attempt_count=entry.retry_attempt_count,
            )
            self._topic_schedules_by_id[record.schedule_id] = record
            self._schedule_ids_by_route.setdefault(route, set()).add(record.schedule_id)

        for entry in snapshot.team_worker_states:
            key = self._team_worker_key(entry.team_name, entry.agent_name)
            if key is None:
                continue
            child_route = TelegramRoute(
                chat_id=entry.child_chat_id,
                thread_id=entry.child_thread_id,
            )
            child_state = self._get_state(child_route, create=True)
            assert child_state is not None
            if entry.child_session_id and child_state.session_id != entry.child_session_id:
                child_state.session_manager.set_session_id(entry.child_session_id)
                self._route_by_session_id[entry.child_session_id] = child_route
                self._persist_state_for_route(child_route)

            restored_idle_ready = entry.idle_ready
            if not restored_idle_ready and entry.status not in {"failed", "stopped"}:
                # Pragmatic restart policy: if we cannot prove terminal shutdown,
                # restore as idle-ready so teammate messages can wake the worker.
                restored_idle_ready = True

            record = _ForkTaskRecord(
                task_id=entry.task_id,
                parent_route=child_route,
                parent_session_id_at_launch=entry.child_session_id,
                parent_source_uuid=self._session_heads.get(entry.child_session_id, ""),
                child_route=child_route,
                child_session_id=entry.child_session_id,
                prompt="",
                description=entry.description,
                status="completed" if restored_idle_ready else entry.status,
                is_fork=False,
                launch_tool_name="AgentTask",
                team_name=entry.team_name,
                agent_name=entry.agent_name,
                idle_ready=restored_idle_ready,
                emit_parent_callback=False,
            )
            self._fork_tasks_by_id[record.task_id] = record
            self._team_worker_records[key] = record.task_id
            if record.idle_ready and record.status not in {"failed", "stopped"}:
                self._fork_task_by_child_route[child_route] = record.task_id
            self._persist_team_worker_record(record)

    def _set_topic_metadata(
        self,
        *,
        route: TelegramRoute,
        title: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> None:
        existing = self._topic_metadata_by_route.get(route, _TopicMetadata())
        next_title = existing.title
        next_icon = existing.icon_custom_emoji_id
        if isinstance(title, str):
            stripped = title.strip()
            next_title = stripped or next_title
        if isinstance(icon_custom_emoji_id, str):
            stripped_icon = icon_custom_emoji_id.strip()
            next_icon = stripped_icon or next_icon
        updated = _TopicMetadata(title=next_title, icon_custom_emoji_id=next_icon)
        self._topic_metadata_by_route[route] = updated

        state = self._get_state(route, create=False)
        if state is not None:
            if updated.title:
                state.topic_title = updated.title
            if updated.icon_custom_emoji_id:
                state.topic_icon_custom_emoji_id = updated.icon_custom_emoji_id
            self._persist_state_for_route(route)

    def _update_topic_metadata_from_message(self, message) -> None:
        route = self._route_for_message(message)
        if route.thread_id is None:
            return
        created = getattr(message, "forum_topic_created", None)
        edited = getattr(message, "forum_topic_edited", None)
        title = None
        icon_custom_emoji_id = None
        if created is not None:
            title = getattr(created, "name", None) or title
            icon_custom_emoji_id = (
                getattr(created, "icon_custom_emoji_id", None) or icon_custom_emoji_id
            )
        if edited is not None:
            title = (
                getattr(edited, "name", None)
                or getattr(edited, "title", None)
                or title
            )
            edited_icon = getattr(edited, "icon_custom_emoji_id", None)
            if isinstance(edited_icon, str):
                icon_custom_emoji_id = edited_icon
        self._set_topic_metadata(
            route=route,
            title=title,
            icon_custom_emoji_id=icon_custom_emoji_id,
        )

    def _build_session_state(
        self,
        route: TelegramRoute,
        *,
        topic_title: str | None = None,
    ) -> TelegramSessionState:
        hook_state = HookState()
        state = TelegramSessionState(
            route=route,
            hook_state=hook_state,
            session_manager=SessionManager(config=self._config, hook_state=hook_state),
            topic_title=topic_title
            or self._topic_metadata_for_route(route).title
            or self._default_topic_title(route),
            topic_icon_custom_emoji_id=self._topic_metadata_for_route(route).icon_custom_emoji_id,
        )
        hook_state.fork_task_launcher = self._make_fork_task_launcher(route)
        hook_state.fork_task_outputter = self._make_fork_task_outputter(route)
        hook_state.fork_task_stopper = self._make_fork_task_stopper(route)
        hook_state.cron_creator = self._make_cron_creator(route)
        hook_state.cron_lister = self._make_cron_lister(route)
        hook_state.cron_deleter = self._make_cron_deleter(route)
        hook_state.inbox_message_notifier = self._make_inbox_message_notifier(route)
        hook_state.stop_event_notifier = self._make_stop_event_notifier(route)
        return state

    def _get_state(
        self,
        route: TelegramRoute,
        *,
        create: bool = True,
        topic_title: str | None = None,
    ) -> TelegramSessionState | None:
        state = self._states_by_route.get(route)
        if state is None and create:
            state = self._build_session_state(route, topic_title=topic_title)
            self._states_by_route[route] = state
            self._persist_state_for_route(route)
            self._maybe_seed_default_schedule(route=route)
        if state is not None and topic_title:
            if not state.topic_title:
                state.topic_title = topic_title
            self._set_topic_metadata(route=route, title=topic_title)
        return state

    def _bind_state_session(self, state: TelegramSessionState) -> None:
        session_id = state.session_id
        if session_id:
            self._route_by_session_id[session_id] = state.route
            self._persist_state_for_route(state.route)

    def _unbind_route_sessions(self, route: TelegramRoute) -> None:
        stale = [session_id for session_id, mapped_route in self._route_by_session_id.items() if mapped_route == route]
        for session_id in stale:
            self._route_by_session_id.pop(session_id, None)

    def _get_route_lock(self, route: TelegramRoute) -> asyncio.Lock:
        lock = self._route_locks.get(route)
        if lock is None:
            lock = asyncio.Lock()
            self._route_locks[route] = lock
        return lock

    def _make_fork_task_launcher(self, route: TelegramRoute):
        async def _launch(args: dict[str, Any]) -> dict[str, Any]:
            return await self._launch_fork_task(route=route, args=args)

        return _launch

    def _make_fork_task_outputter(self, route: TelegramRoute):
        async def _output(args: dict[str, Any]) -> dict[str, Any]:
            return await self._fork_task_output(route=route, args=args)

        return _output

    def _make_fork_task_stopper(self, route: TelegramRoute):
        async def _stop(args: dict[str, Any]) -> dict[str, Any]:
            return await self._fork_task_stop(route=route, args=args)

        return _stop

    def _make_cron_creator(self, route: TelegramRoute):
        async def _create(args: dict[str, Any]) -> dict[str, Any]:
            return await self._cron_create(route=route, args=args)

        return _create

    def _make_cron_lister(self, route: TelegramRoute):
        async def _list(args: dict[str, Any]) -> dict[str, Any]:
            _ = args
            return await self._cron_list(route=route)

        return _list

    def _make_cron_deleter(self, route: TelegramRoute):
        async def _delete(args: dict[str, Any]) -> dict[str, Any]:
            return await self._cron_delete(route=route, args=args)

        return _delete

    def _make_inbox_message_notifier(self, route: TelegramRoute):
        async def _notify(payload: dict[str, Any]) -> None:
            await self._handle_inbox_message_notification(
                sender_route=route,
                payload=payload,
            )

        return _notify

    def _make_stop_event_notifier(self, route: TelegramRoute):
        async def _notify(payload: dict[str, Any]) -> None:
            self._schedule_stop_events.put_nowait((route, payload))

        return _notify

    def _is_authorized(self, user_id: int) -> bool:
        # SECURITY: empty allowed list = NO ONE can use the bot (deny by default)
        allowed = self._config.telegram_allowed_user_ids
        return bool(allowed) and user_id in allowed

    def _route_has_active_fork_tasks(self, state: TelegramSessionState) -> bool:
        return bool(state.active_fork_task_ids)

    def _should_emit_completion_summary(self, state: TelegramSessionState) -> bool:
        # Keep completion markers consistent across user topics, forks, and
        # delegated workers. Per-route suppression caused missing terminal
        # markers in child topics while scheduled runs still emitted summaries.
        _ = state
        return True

    def _current_topic_base(self, state: TelegramSessionState) -> str:
        return (state.topic_title or self._default_topic_title(state.route)).strip()

    def _current_topic_title(self, route: TelegramRoute) -> str:
        state = self._get_state(route, create=False)
        if state is None:
            return self._default_topic_title(route)
        return self._current_topic_base(state)

    def _next_auto_child_title(self, state: TelegramSessionState) -> str:
        base = self._current_topic_base(state)
        if state.child_fork_base_title != base:
            state.child_fork_base_title = base
            state.child_fork_count = 0
        state.child_fork_count += 1
        self._persist_state_for_route(state.route)
        return f"{base} - F{state.child_fork_count}".strip()[:128]

    @staticmethod
    def _cron_error_result(text: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": text}],
            "tool_use_result": {"error": text},
            "is_error": True,
        }

    @staticmethod
    def _cron_ok_result(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}],
            "tool_use_result": payload,
        }

    @staticmethod
    def _parse_supported_cron_interval_seconds(expr: str) -> int | None:
        normalized = expr.strip().lower()
        if normalized == "@hourly":
            return 60 * 60
        if normalized == "@daily":
            return 24 * 60 * 60
        parts = normalized.split()
        if len(parts) != 5:
            return None
        minute, hour, day_of_month, month, day_of_week = parts

        def _step_value(token: str) -> int | None:
            if not token.startswith("*/"):
                return None
            raw = token[2:]
            if not raw.isdigit():
                return None
            value = int(raw)
            if value <= 0:
                return None
            return value

        if (
            (minutes := _step_value(minute)) is not None
            and hour == "*"
            and day_of_month == "*"
            and month == "*"
            and day_of_week == "*"
        ):
            return minutes * 60
        if (
            minute == "0"
            and (hours := _step_value(hour)) is not None
            and day_of_month == "*"
            and month == "*"
            and day_of_week == "*"
        ):
            return hours * 60 * 60
        if (
            minute == "0"
            and hour == "0"
            and (days := _step_value(day_of_month)) is not None
            and month == "*"
            and day_of_week == "*"
        ):
            return days * 24 * 60 * 60
        return None

    @staticmethod
    def _parse_rfc3339_timestamp(raw_value: str | None) -> float | None:
        if raw_value is None:
            return None
        normalized = raw_value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        return parsed.timestamp()

    @staticmethod
    def _window_overlap(
        *,
        start_a: float | None,
        end_a: float | None,
        start_b: float | None,
        end_b: float | None,
    ) -> bool:
        left_a = float("-inf") if start_a is None else float(start_a)
        right_a = float("inf") if end_a is None else float(end_a)
        left_b = float("-inf") if start_b is None else float(start_b)
        right_b = float("inf") if end_b is None else float(end_b)
        return max(left_a, left_b) < min(right_a, right_b)

    def _validate_schedule_overlap(
        self,
        *,
        route: TelegramRoute,
        start_ts: float | None,
        end_ts: float | None,
    ) -> str | None:
        now = time.time()
        active_schedule_id = self._active_schedule_execution_by_route.get(route)
        for existing in self._active_schedules_for_route(route):
            if self._schedule_is_exhausted(existing, now):
                existing.enabled = False
                existing.next_run_at = None
                self._register_topic_schedule(existing)
                continue
            if (
                active_schedule_id == existing.schedule_id
                and existing.max_runs is not None
                and (existing.run_count + 1) >= existing.max_runs
            ):
                continue
            if self._window_overlap(
                start_a=start_ts,
                end_a=end_ts,
                start_b=existing.from_ts,
                end_b=existing.until_ts,
            ):
                return (
                    "CronCreate failed: overlapping schedule window for this topic. "
                    "Only non-overlapping [from, until) windows are allowed."
                )
        return None

    def _next_cron_fire_ts(
        self,
        *,
        cron_expr: str,
        base_ts: float,
        not_before_ts: float | None = None,
    ) -> float:
        if croniter is None:
            raise ValueError("croniter is not installed")
        anchor = datetime.fromtimestamp(base_ts, timezone.utc)
        try:
            itr = croniter(cron_expr, anchor)
            candidate = float(itr.get_next(float))
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if not_before_ts is not None and candidate < not_before_ts:
            anchor_nb = datetime.fromtimestamp(not_before_ts - 1.0, timezone.utc)
            try:
                itr = croniter(cron_expr, anchor_nb)
                candidate = float(itr.get_next(float))
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        return candidate

    def _persist_topic_schedule(self, record: _TopicScheduleRecord) -> None:
        self._state_store.upsert_topic_schedule(
            schedule_id=record.schedule_id,
            chat_id=record.route.chat_id,
            thread_id=record.route.thread_id,
            description=record.description,
            schedule_mode=record.schedule_mode,
            cron_expr=record.cron_expr,
            trigger_kind=record.trigger_kind,
            interval_seconds=record.interval_seconds,
            prompt=record.prompt,
            run_mode="reset_session" if record.reset_session else "continue",
            recurring=record.recurring,
            enabled=record.enabled,
            run_count=record.run_count,
            max_runs=record.max_runs,
            from_ts=record.from_ts,
            until_ts=record.until_ts,
            inherit_mode=record.inherit_mode,
            next_run_at=record.next_run_at,
            last_run_at=record.last_run_at,
            last_success_at=record.last_success_at,
            last_error=record.last_error,
            max_retry_attempts=record.max_retry_attempts,
            retry_delay_seconds=record.retry_delay_seconds,
            retry_attempt_count=record.retry_attempt_count,
        )

    def _register_topic_schedule(self, record: _TopicScheduleRecord) -> None:
        previous = self._topic_schedules_by_id.get(record.schedule_id)
        if previous is not None and previous.route != record.route:
            schedule_ids = self._schedule_ids_by_route.get(previous.route)
            if schedule_ids is not None:
                schedule_ids.discard(record.schedule_id)
                if not schedule_ids:
                    self._schedule_ids_by_route.pop(previous.route, None)
        self._topic_schedules_by_id[record.schedule_id] = record
        self._schedule_ids_by_route.setdefault(record.route, set()).add(record.schedule_id)
        self._persist_topic_schedule(record)

    def _delete_topic_schedule(self, schedule_id: str) -> None:
        record = self._topic_schedules_by_id.pop(schedule_id, None)
        if record is not None:
            schedule_ids = self._schedule_ids_by_route.get(record.route)
            if schedule_ids is not None:
                schedule_ids.discard(schedule_id)
                if not schedule_ids:
                    self._schedule_ids_by_route.pop(record.route, None)
        self._state_store.delete_topic_schedule(schedule_id=schedule_id)

    def _delete_topic_schedules_for_route(self, route: TelegramRoute) -> None:
        for schedule_id in list(self._schedule_ids_by_route.get(route, set())):
            self._topic_schedules_by_id.pop(schedule_id, None)
            self._state_store.delete_topic_schedule(schedule_id=schedule_id)
        self._schedule_ids_by_route.pop(route, None)
        self._state_store.delete_topic_schedules_for_route(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
        )

    def _schedule_should_inherit(self, *, record: _TopicScheduleRecord, is_fork: bool) -> bool:
        mode = (record.inherit_mode or "none").strip().lower()
        if mode == "all":
            return True
        if mode == "fork":
            return is_fork
        return False

    def _inherit_topic_schedules(
        self,
        *,
        parent_route: TelegramRoute,
        child_route: TelegramRoute,
        is_fork: bool,
    ) -> None:
        now = time.time()
        parent_records = [
            self._topic_schedules_by_id[schedule_id]
            for schedule_id in sorted(self._schedule_ids_by_route.get(parent_route, set()))
            if schedule_id in self._topic_schedules_by_id
        ]
        for parent_record in parent_records:
            if not parent_record.enabled:
                continue
            if self._schedule_is_exhausted(parent_record, now):
                continue
            if not self._schedule_should_inherit(record=parent_record, is_fork=is_fork):
                continue
            overlap_error = self._validate_schedule_overlap(
                route=child_route,
                start_ts=parent_record.from_ts,
                end_ts=parent_record.until_ts,
            )
            if overlap_error:
                logger.info(
                    "Skipping inherited schedule due to overlap parent_route=%s child_route=%s schedule_id=%s",
                    parent_route,
                    child_route,
                    parent_record.schedule_id,
                )
                continue
            child_record = _TopicScheduleRecord(
                schedule_id=uuid.uuid4().hex[:8],
                route=child_route,
                description=parent_record.description,
                schedule_mode=parent_record.schedule_mode,
                cron_expr=parent_record.cron_expr,
                trigger_kind=parent_record.trigger_kind,
                interval_seconds=parent_record.interval_seconds,
                prompt=parent_record.prompt,
                reset_session=parent_record.reset_session,
                recurring=parent_record.recurring,
                enabled=True,
                run_count=0,
                max_runs=parent_record.max_runs,
                from_ts=parent_record.from_ts,
                until_ts=parent_record.until_ts,
                inherit_mode=parent_record.inherit_mode,
                next_run_at=None,
                last_run_at=None,
                last_success_at=None,
                last_error=None,
                max_retry_attempts=parent_record.max_retry_attempts,
                retry_delay_seconds=parent_record.retry_delay_seconds,
                retry_attempt_count=0,
            )
            if child_record.trigger_kind in {"interval", "cron"}:
                try:
                    child_record.next_run_at = self._next_timed_run_at(child_record, base_ts=now)
                except ValueError:
                    logger.debug(
                        "Skipping inherited schedule with invalid timed config parent=%s child=%s",
                        parent_record.schedule_id,
                        child_route,
                        exc_info=True,
                    )
                    continue
            self._register_topic_schedule(child_record)

    def _schedule_summary_payload(self, record: _TopicScheduleRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": record.schedule_id,
            "description": record.description,
            "schedule_mode": record.schedule_mode,
            "trigger_kind": record.trigger_kind,
            "interval_seconds": record.interval_seconds,
            "cron": record.cron_expr,
            "trigger": self._schedule_trigger_label(record),
            "reset_session": record.reset_session,
            "enabled": record.enabled,
            "run_count": record.run_count,
            "max_runs": record.max_runs,
            "from": (
                datetime.fromtimestamp(record.from_ts, timezone.utc).isoformat().replace("+00:00", "Z")
                if record.from_ts is not None
                else None
            ),
            "until": (
                datetime.fromtimestamp(record.until_ts, timezone.utc).isoformat().replace("+00:00", "Z")
                if record.until_ts is not None
                else None
            ),
            "next_run_at": (
                datetime.fromtimestamp(record.next_run_at, timezone.utc).isoformat().replace("+00:00", "Z")
                if record.next_run_at is not None
                else None
            ),
            "inherit": record.inherit_mode,
        }
        if record.last_error:
            payload["last_error"] = record.last_error
        return payload

    async def _cron_create(
        self,
        *,
        route: TelegramRoute,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._get_state(route, create=False)
        if state is None:
            return self._cron_error_result("CronCreate is only available inside an active Telegram topic")

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return self._cron_error_result("CronCreate failed: prompt is required")
        schedule_mode = str(args.get("schedule_mode") or "").strip().lower()
        cron_expr = str(args.get("cron") or "").strip()
        if not schedule_mode:
            if args.get("interval_seconds") is not None:
                schedule_mode = "interval"
            elif cron_expr:
                schedule_mode = "cron"
        if schedule_mode not in {"interval", "cron"}:
            return self._cron_error_result(
                "CronCreate failed: schedule_mode must be interval or cron"
            )
        if schedule_mode == "cron" and not cron_expr:
            return self._cron_error_result(
                "CronCreate failed: cron is required for schedule_mode=cron"
            )

        interval_override = args.get("interval_seconds")
        interval_seconds: int | None
        trigger_kind: str
        if schedule_mode == "interval":
            if interval_override is None:
                return self._cron_error_result(
                    "CronCreate failed: interval_seconds is required for schedule_mode=interval"
                )
            try:
                parsed_override = int(interval_override)
            except (TypeError, ValueError):
                return self._cron_error_result("CronCreate failed: interval_seconds must be an integer")
            if parsed_override < 0:
                return self._cron_error_result("CronCreate failed: interval_seconds must be non-negative")
            if parsed_override == 0:
                trigger_kind = "on_topic_stop"
                interval_seconds = None
            else:
                trigger_kind = "interval"
                interval_seconds = parsed_override
        else:
            if croniter is None:
                return self._cron_error_result(
                    "CronCreate failed: cron mode unavailable because croniter is not installed"
                )
            trigger_kind = "cron"
            interval_seconds = None

        max_runs = args.get("max_runs")
        if max_runs is not None:
            try:
                max_runs = int(max_runs)
            except (TypeError, ValueError):
                return self._cron_error_result("CronCreate failed: max_runs must be an integer")
            if max_runs <= 0:
                return self._cron_error_result("CronCreate failed: max_runs must be positive")
        else:
            max_runs = 1

        reset_session = args.get("reset_session")
        if reset_session is None:
            run_mode = str(args.get("run_mode") or "").strip().lower()
            if run_mode and run_mode not in {"continue", "reset_session"}:
                return self._cron_error_result(
                    "CronCreate failed: run_mode must be continue or reset_session"
                )
            reset_session = run_mode == "reset_session"
        elif not isinstance(reset_session, bool):
            return self._cron_error_result("CronCreate failed: reset_session must be boolean")

        from_raw = str(args.get("from") or "").strip() or None
        until_raw = str(args.get("until") or "").strip() or None
        try:
            from_ts = self._parse_rfc3339_timestamp(from_raw)
            until_ts = self._parse_rfc3339_timestamp(until_raw)
        except ValueError:
            return self._cron_error_result(
                "CronCreate failed: from/until must be RFC3339 timestamp"
            )
        if from_ts is not None and until_ts is not None and from_ts > until_ts:
            return self._cron_error_result(
                "CronCreate failed: from must be <= until"
            )

        inherit_mode = str(args.get("inherit") or "none").strip().lower() or "none"
        if inherit_mode not in {"none", "fork", "all"}:
            return self._cron_error_result(
                "CronCreate failed: inherit must be none, fork, or all"
            )

        overlap_error = self._validate_schedule_overlap(
            route=route,
            start_ts=from_ts,
            end_ts=until_ts,
        )
        if overlap_error:
            return self._cron_error_result(overlap_error)

        now = time.time()
        schedule_id = uuid.uuid4().hex[:8]
        record = _TopicScheduleRecord(
            schedule_id=schedule_id,
            route=route,
            description=str(args.get("description") or "").strip() or None,
            schedule_mode=schedule_mode,
            cron_expr=cron_expr,
            trigger_kind=trigger_kind,
            interval_seconds=interval_seconds,
            prompt=prompt,
            reset_session=bool(reset_session),
            recurring=max_runs != 1,
            enabled=True,
            run_count=0,
            max_runs=max_runs,
            from_ts=from_ts,
            until_ts=until_ts,
            inherit_mode=inherit_mode,
            next_run_at=None,
            max_retry_attempts=self._schedule_retry_max_attempts,
            retry_delay_seconds=self._schedule_retry_delay_seconds,
            retry_attempt_count=0,
        )
        if record.trigger_kind in {"interval", "cron"}:
            try:
                record.next_run_at = self._next_timed_run_at(record, base_ts=now)
            except ValueError as exc:
                return self._cron_error_result(f"CronCreate failed: invalid cron expression: {exc}")
        self._register_topic_schedule(record)

        bot = self._bot_for_state(state)
        if bot is not None:
            try:
                await self._send_system_message(
                    route=route,
                    bot=bot,
                    text=(
                        "schedule created: "
                        f"{self._schedule_display_name(record)} "
                        f"({self._schedule_trigger_label(record)})"
                    ),
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Failed sending schedule create marker route=%s", route, exc_info=True)

        payload = {
            "schedule": self._schedule_summary_payload(record),
        }
        return self._cron_ok_result(payload)

    async def _cron_list(self, *, route: TelegramRoute) -> dict[str, Any]:
        schedules = [
            self._topic_schedules_by_id[schedule_id]
            for schedule_id in sorted(self._schedule_ids_by_route.get(route, set()))
            if schedule_id in self._topic_schedules_by_id
        ]
        payload = {"schedules": [self._schedule_summary_payload(record) for record in schedules]}
        return self._cron_ok_result(payload)

    async def _cron_delete(
        self,
        *,
        route: TelegramRoute,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        schedule_id = str(args.get("id") or "").strip()
        if not schedule_id:
            return self._cron_error_result("CronDelete failed: id is required")
        record = self._topic_schedules_by_id.get(schedule_id)
        if record is None or record.route != route:
            return self._cron_ok_result({"deleted": False, "id": schedule_id})
        self._delete_topic_schedule(schedule_id)
        state = self._get_state(route, create=False)
        bot = self._bot_for_state(state) if state is not None else self._primary_bot
        if bot is not None:
            try:
                await self._send_system_message(
                    route=route,
                    bot=bot,
                    text=f"schedule deleted: {schedule_id}",
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Failed sending schedule delete marker route=%s", route, exc_info=True)
        return self._cron_ok_result({"deleted": True, "id": schedule_id})

    def _schedule_is_exhausted(self, record: _TopicScheduleRecord, now: float) -> bool:
        if record.max_runs is not None and record.run_count >= record.max_runs:
            return True
        if record.until_ts is not None and now >= record.until_ts:
            return True
        return False

    def _schedule_window_not_started(self, record: _TopicScheduleRecord, now: float) -> bool:
        return record.from_ts is not None and now < record.from_ts

    def _next_timed_run_at(self, record: _TopicScheduleRecord, *, base_ts: float) -> float | None:
        if record.trigger_kind == "interval":
            if record.interval_seconds is None:
                return None
            anchor = max(base_ts, record.from_ts or base_ts)
            return anchor + record.interval_seconds
        if record.trigger_kind == "cron":
            cron_expr = (record.cron_expr or "").strip()
            if not cron_expr:
                raise ValueError("missing cron expression")
            return self._next_cron_fire_ts(
                cron_expr=cron_expr,
                base_ts=base_ts,
                not_before_ts=record.from_ts,
            )
        return None

    def _reanchor_interval_schedules_for_route(
        self,
        *,
        route: TelegramRoute,
        base_ts: float | None = None,
    ) -> None:
        now = base_ts if base_ts is not None else time.time()
        schedule_ids = sorted(self._schedule_ids_by_route.get(route, set()))
        for schedule_id in schedule_ids:
            record = self._topic_schedules_by_id.get(schedule_id)
            if record is None or not record.enabled:
                continue
            if record.trigger_kind != "interval" or record.interval_seconds is None:
                continue
            if self._schedule_is_exhausted(record, now):
                record.enabled = False
                record.next_run_at = None
                self._register_topic_schedule(record)
                continue
            if self._schedule_window_not_started(record, now):
                continue
            try:
                record.next_run_at = self._next_timed_run_at(record, base_ts=now)
            except ValueError as exc:
                record.enabled = False
                record.last_error = f"schedule config error: {exc}"
                record.next_run_at = None
            self._register_topic_schedule(record)

    async def _execute_topic_schedule(
        self,
        *,
        record: _TopicScheduleRecord,
        trigger_kind: str,
    ) -> bool:
        if not record.enabled:
            return False
        now = time.time()
        if self._schedule_is_exhausted(record, now):
            record.enabled = False
            record.next_run_at = None
            self._register_topic_schedule(record)
            return False
        if self._schedule_window_not_started(record, now):
            if record.trigger_kind in {"interval", "cron"}:
                try:
                    record.next_run_at = self._next_timed_run_at(record, base_ts=now)
                except ValueError as exc:
                    record.enabled = False
                    record.last_error = f"schedule config error: {exc}"
                    record.next_run_at = None
                self._register_topic_schedule(record)
            return False

        state = self._get_state(record.route, create=False)
        if state is None:
            return False
        bot = self._bot_for_state(state)
        if bot is None:
            return False
        lock = self._get_route_lock(record.route)
        if lock.locked() or state.busy or record.route in self._schedule_running_by_route:
            return False

        now_after = now
        run_succeeded = False
        run_failed = False
        failure_text: str | None = None

        async def _emit_post_schedule_summary() -> None:
            try:
                await self._send_system_message(
                    route=record.route,
                    bot=bot,
                    text=self._build_completion_summary(
                        state,
                        triggered_schedule_id=record.schedule_id,
                    ),
                    disable_notification=False,
                )
            except Exception:
                logger.debug(
                    "Failed sending post-schedule summary route=%s schedule_id=%s",
                    record.route,
                    record.schedule_id,
                    exc_info=True,
                )

        self._schedule_running_by_route.add(record.route)
        self._active_schedule_execution_by_route[record.route] = record.schedule_id
        try:
            async with lock:
                if state.busy:
                    return False
                if record.reset_session:
                    await self._reset_route_state(state)
                    state.last_bot = bot
                try:
                    await self._send_system_message(
                        route=record.route,
                        bot=bot,
                        text=self._schedule_trigger_line(record, now_ts=time.time()),
                        disable_notification=True,
                    )
                except Exception:
                    logger.debug(
                        "Failed sending schedule trigger marker route=%s schedule_id=%s",
                        record.route,
                        record.schedule_id,
                        exc_info=True,
                    )
                scheduled_prompt = (
                    "(System: scheduled execution.)\n\n"
                    f"{record.prompt}"
                )
                record.last_run_at = time.time()
                self._schedule_stop_suppress_until[record.route] = (
                    time.time() + _SCHEDULE_STOP_SUPPRESS_SECONDS
                )
                state.hook_state.schedule_run_active = True
                try:
                    outcome = await self._run_and_send(
                        state=state,
                        user_text=scheduled_prompt,
                        bot=bot,
                        trigger_message=None,
                        triggered_schedule_id=record.schedule_id,
                        suppress_completion_summary=True,
                    )
                    now_after = time.time()
                    if outcome.failed:
                        run_failed = True
                        failure_text = outcome.error or "scheduled run failed"
                    else:
                        run_succeeded = True
                finally:
                    state.hook_state.schedule_run_active = False
        except Exception as exc:
            now_after = time.time()
            run_failed = True
            failure_text = f"{type(exc).__name__}: {exc}"
            logger.debug(
                "Scheduled run failed schedule_id=%s route=%s",
                record.schedule_id,
                record.route,
                exc_info=True,
            )
        finally:
            self._schedule_running_by_route.discard(record.route)
            self._active_schedule_execution_by_route.pop(record.route, None)

        if run_succeeded:
            record.run_count += 1
            record.retry_attempt_count = 0
            record.last_error = None
            record.last_success_at = now_after
        elif run_failed:
            record.last_error = failure_text or "scheduled run failed"
            if (
                record.max_retry_attempts > 0
                and record.retry_attempt_count < record.max_retry_attempts
            ):
                record.retry_attempt_count += 1
                if record.trigger_kind in {"interval", "cron"}:
                    retry_delay = max(int(record.retry_delay_seconds), 1)
                    record.next_run_at = now_after + retry_delay
                self._register_topic_schedule(record)
                await _emit_post_schedule_summary()
                return False
            record.retry_attempt_count = 0
            record.run_count += 1
            try:
                await self._send_system_message(
                    route=record.route,
                    bot=bot,
                    text=(
                        "schedule failed: "
                        f"{self._schedule_display_name(record)}: "
                        f"{record.last_error}"
                    ),
                    disable_notification=False,
                )
            except Exception:
                logger.debug(
                    "Failed sending schedule failure marker route=%s schedule_id=%s",
                    record.route,
                    record.schedule_id,
                    exc_info=True,
                )

        if self._schedule_is_exhausted(record, now_after):
            record.enabled = False
        if record.enabled and record.trigger_kind in {"interval", "cron"}:
            try:
                record.next_run_at = self._next_timed_run_at(record, base_ts=now_after)
            except ValueError as exc:
                record.enabled = False
                record.last_error = f"schedule config error: {exc}"
                record.next_run_at = None
        elif record.trigger_kind in {"interval", "cron"}:
            record.next_run_at = None
        self._register_topic_schedule(record)
        await _emit_post_schedule_summary()
        return run_succeeded

    async def _run_due_interval_schedules(self) -> None:
        now = time.time()
        due_records = [
            record
            for record in self._topic_schedules_by_id.values()
            if record.enabled
            and record.trigger_kind in {"interval", "cron"}
            and record.next_run_at is not None
            and record.next_run_at <= now
        ]
        due_records.sort(key=lambda record: float(record.next_run_at or now))
        for record in due_records:
            await self._execute_topic_schedule(record=record, trigger_kind=record.trigger_kind)

    async def _process_stop_schedule_events(self) -> None:
        events: list[tuple[TelegramRoute, dict[str, Any]]] = []
        while not self._schedule_stop_events.empty():
            try:
                events.append(self._schedule_stop_events.get_nowait())
            except asyncio.QueueEmpty:
                break

        now = time.time()
        expired_routes = [
            route
            for route, until in self._schedule_stop_suppress_until.items()
            if now >= until
        ]
        for route in expired_routes:
            self._schedule_stop_suppress_until.pop(route, None)

        deferred_by_route: dict[TelegramRoute, dict[str, Any]] = {}

        def _defer_stop_event(route: TelegramRoute, payload: dict[str, Any]) -> None:
            raw_count = payload.get("_defer_count")
            if isinstance(raw_count, (int, float)):
                defer_count = int(raw_count)
            else:
                defer_count = 0
            if defer_count >= _SCHEDULE_STOP_MAX_DEFERS:
                logger.debug(
                    "Dropping deferred stop event after max defers route=%s payload=%s",
                    route,
                    payload,
                )
                return
            deferred_payload = dict(payload)
            # Replayed events should run after the active turn drains.
            deferred_payload["execution_active"] = False
            deferred_payload["schedule_run_active"] = False
            deferred_payload["_defer_count"] = defer_count + 1
            deferred_by_route[route] = deferred_payload

        for route, payload in events:
            if bool(payload.get("schedule_run_active")):
                continue
            if bool(payload.get("execution_active")):
                _defer_stop_event(route, payload)
                continue
            suppress_until = self._schedule_stop_suppress_until.get(route, 0.0)
            if now < suppress_until:
                continue
            state = self._get_state(route, create=False)
            if state is None:
                continue
            lock = self._get_route_lock(route)
            if state.busy or lock.locked() or route in self._schedule_running_by_route:
                _defer_stop_event(route, payload)
                continue
            session_id = payload.get("session_id")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id in self._route_by_session_id
                and self._route_by_session_id.get(session_id) != route
            ):
                continue
            records = [
                self._topic_schedules_by_id[schedule_id]
                for schedule_id in sorted(self._schedule_ids_by_route.get(route, set()))
                if schedule_id in self._topic_schedules_by_id
            ]
            for record in records:
                if not record.enabled:
                    continue
                if self._schedule_is_exhausted(record, now):
                    record.enabled = False
                    record.next_run_at = None
                    self._register_topic_schedule(record)
                    continue
                if record.trigger_kind == "interval" and record.interval_seconds is not None:
                    if self._schedule_window_not_started(record, now):
                        continue
                    try:
                        record.next_run_at = self._next_timed_run_at(record, base_ts=now)
                    except ValueError as exc:
                        record.enabled = False
                        record.last_error = f"schedule config error: {exc}"
                        record.next_run_at = None
                    self._register_topic_schedule(record)
                    continue
                if record.trigger_kind != "on_topic_stop":
                    continue
                if self._schedule_window_not_started(record, now):
                    continue
                ran = await self._execute_topic_schedule(record=record, trigger_kind="on_topic_stop")
                if not ran and (state.busy or lock.locked() or route in self._schedule_running_by_route):
                    _defer_stop_event(route, payload)
                    break

        for route, payload in deferred_by_route.items():
            self._schedule_stop_events.put_nowait((route, payload))

    def _build_fork_task_topic_name(
        self,
        *,
        state: TelegramSessionState,
        description: str | None,
    ) -> str:
        base = self._current_topic_base(state)
        if description:
            return f"{base} - {description}".strip()[:128]
        return self._next_auto_child_title(state)

    def _build_team_worker_env(
        self,
        *,
        team_name: str | None,
        agent_name: str | None = None,
    ) -> dict[str, str]:
        normalized_team = (team_name or "").strip()
        if not normalized_team:
            return {}
        env = {
            "CLAUDE_CODE_ENABLE_TASKS": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_TASK_LIST_ID": normalized_team,
            "CLAUDE_CODE_TEAM_NAME": normalized_team,
        }
        normalized_agent = (agent_name or "").strip()
        if normalized_agent:
            env["CLAUDE_CODE_AGENT_NAME"] = normalized_agent
        return env

    def _upsert_native_team_member_config(
        self,
        *,
        team_name: str | None,
        agent_name: str | None,
        child_session_id: str | None,
    ) -> None:
        normalized_team = (team_name or "").strip()
        normalized_agent = (agent_name or "").strip()
        if not normalized_team or not normalized_agent:
            return

        team_dir = Path.home() / ".claude" / "teams" / normalized_team
        config_path = team_dir / "config.json"
        team_dir.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                logger.debug("Failed to parse team config %s; recreating", config_path, exc_info=True)

        now_ms = int(time.time() * 1000)
        if not isinstance(payload.get("name"), str) or not str(payload.get("name")).strip():
            payload["name"] = normalized_team
        if not isinstance(payload.get("createdAt"), int):
            payload["createdAt"] = now_ms
        lead_agent_id = str(payload.get("leadAgentId") or f"team-lead@{normalized_team}")
        payload["leadAgentId"] = lead_agent_id
        if child_session_id and (not isinstance(payload.get("leadSessionId"), str) or not payload.get("leadSessionId")):
            payload["leadSessionId"] = child_session_id

        members_raw = payload.get("members")
        members: list[dict[str, Any]] = [
            member for member in members_raw
            if isinstance(member, dict)
        ] if isinstance(members_raw, list) else []

        def _upsert_member(member: dict[str, Any]) -> None:
            member_id = str(member.get("agentId") or "").strip()
            if not member_id:
                return
            for idx, existing in enumerate(members):
                if str(existing.get("agentId") or "").strip() == member_id:
                    merged = dict(existing)
                    merged.update(member)
                    members[idx] = merged
                    return
            members.append(member)

        _upsert_member(
            {
                "agentId": lead_agent_id,
                "name": "team-lead",
                "agentType": "team-lead",
                "model": getattr(self._config, "model", "claude-opus-4-6"),
                "joinedAt": now_ms,
                "tmuxPaneId": "",
                "cwd": str(self._config.vault_path),
                "subscriptions": [],
            }
        )

        worker_agent_id = f"{normalized_agent}@{normalized_team}"
        _upsert_member(
            {
                "agentId": worker_agent_id,
                "name": normalized_agent,
                "agentType": "general-purpose",
                "model": getattr(self._config, "model", "claude-opus-4-6"),
                "joinedAt": now_ms,
                "tmuxPaneId": "in-process",
                "cwd": str(self._config.vault_path),
                "subscriptions": [],
                "backendType": "in-process",
            }
        )

        payload["members"] = members
        config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

        inbox_dir = team_dir / "inboxes"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = inbox_dir / f"{normalized_agent}.json"
        if not inbox_path.exists():
            inbox_path.write_text("[]", encoding="utf-8")

    def _child_session_path(self, session_id: str) -> str | None:
        path = find_session_jsonl(
            session_id=session_id,
            cwd=self._config.vault_path,
        )
        return str(path) if path is not None else None

    def _persisted_session_uuids(self, session_id: str) -> list[str]:
        path = find_session_jsonl(
            session_id=session_id,
            cwd=self._config.vault_path,
        )
        if path is None:
            return []
        uuids: list[str] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    entry_uuid = entry.get("uuid")
                    if isinstance(entry_uuid, str) and entry_uuid:
                        uuids.append(entry_uuid)
        except OSError:
            return []
        return uuids

    def _resolve_persisted_fork_source(
        self,
        *,
        session_id: str,
        preferred_uuid: str,
        preferred_route: TelegramRoute,
    ) -> tuple[str, TelegramRoute, int | None]:
        persisted_uuids = self._persisted_session_uuids(session_id)
        if not persisted_uuids:
            return preferred_uuid, preferred_route, None
        resolved_uuid = (
            preferred_uuid if preferred_uuid in persisted_uuids else persisted_uuids[-1]
        )
        located = self._find_bound_message_id(
            session_id=session_id,
            jsonl_uuid=resolved_uuid,
            preferred_route=preferred_route,
        )
        if located is None:
            return resolved_uuid, preferred_route, None
        resolved_route, message_id = located
        return resolved_uuid, resolved_route, message_id

    def _find_task_by_child_route(self, route: TelegramRoute) -> _ForkTaskRecord | None:
        task_id = self._fork_task_by_child_route.get(route)
        if not task_id:
            return None
        return self._fork_tasks_by_id.get(task_id)

    @staticmethod
    def _team_worker_key(team_name: str | None, agent_name: str | None) -> tuple[str, str] | None:
        team = (team_name or "").strip().lower()
        agent = (agent_name or "").strip().lower()
        if not team or not agent:
            return None
        return team, agent

    def _remove_team_worker_mappings_for_task(self, task_id: str) -> None:
        stale = [key for key, mapped in self._team_worker_records.items() if mapped == task_id]
        for key in stale:
            self._team_worker_records.pop(key, None)
        self._state_store.delete_team_worker_state_by_task_id(task_id=task_id)

    def _persist_team_worker_record(self, record: _ForkTaskRecord) -> None:
        if record.is_fork:
            self._state_store.delete_team_worker_state_by_task_id(task_id=record.task_id)
            return
        team = (record.team_name or "").strip()
        agent = (record.agent_name or "").strip()
        if not team or not agent:
            self._state_store.delete_team_worker_state_by_task_id(task_id=record.task_id)
            return
        self._state_store.upsert_team_worker_state(
            team_name=team,
            agent_name=agent,
            task_id=record.task_id,
            child_chat_id=record.child_route.chat_id,
            child_thread_id=record.child_route.thread_id,
            child_session_id=record.child_session_id,
            description=record.description,
            status=record.status,
            idle_ready=record.idle_ready,
        )

    def _register_team_worker_record(self, record: _ForkTaskRecord) -> None:
        self._remove_team_worker_mappings_for_task(record.task_id)
        if record.is_fork:
            return
        key = self._team_worker_key(record.team_name, record.agent_name)
        if key is not None:
            self._team_worker_records[key] = record.task_id
            self._upsert_native_team_member_config(
                team_name=record.team_name,
                agent_name=record.agent_name,
                child_session_id=record.child_session_id,
            )
            self._persist_team_worker_record(record)

    def _resolve_team_worker_record(
        self,
        *,
        team_name: str | None,
        agent_name: str | None,
    ) -> _ForkTaskRecord | None:
        key = self._team_worker_key(team_name, agent_name)
        if key is None:
            return None
        task_id = self._team_worker_records.get(key)
        if not task_id:
            return None
        record = self._fork_tasks_by_id.get(task_id)
        if record is None:
            self._team_worker_records.pop(key, None)
            return None
        return record

    def _mark_task_terminal_request(self, route: TelegramRoute, status: str) -> None:
        for record in self._fork_tasks_by_id.values():
            if record.child_route != route and record.parent_route != route:
                continue
            if record.terminal_request is None:
                record.terminal_request = status

    async def _cancel_route_fork_tasks(self, route: TelegramRoute, status: str) -> None:
        for task_id, record in list(self._fork_tasks_by_id.items()):
            if record.child_route != route and record.parent_route != route:
                continue
            if record.terminal_request is None:
                record.terminal_request = status
            if record.status not in {"completed", "failed", "stopped"}:
                record.status = status
            record.idle_ready = False
            record.wake_requested = False
            if record.completed_at is None:
                record.completed_at = time.time()

            parent_state = self._get_state(record.parent_route, create=False)
            if parent_state is not None:
                parent_state.active_fork_task_ids.discard(task_id)

            child_state = self._get_state(record.child_route, create=False)
            if child_state is not None:
                child_state.hook_state.interrupt_flag = True
                try:
                    client = await child_state.session_manager.get_client()
                    await client.interrupt()
                except Exception:
                    logger.debug("Failed interrupting child while clearing task_id=%s", task_id, exc_info=True)

            task = self._fork_task_tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()

            self._fork_task_by_child_route.pop(record.child_route, None)
            self._remove_team_worker_mappings_for_task(task_id)

    def _task_not_found_result(self, task_id: str) -> dict[str, Any]:
        text = f"No task found with ID: {task_id}"
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"<tool_use_error>{text}</tool_use_error>",
                }
            ],
            "tool_use_result": f"Error: {text}",
            "is_error": True,
        }

    def _task_not_running_result(self, task_id: str, *, status: str) -> dict[str, Any]:
        text = f"Task {task_id} is not running (status: {status})"
        return {
            "content": [{"type": "text", "text": f"<tool_use_error>{text}</tool_use_error>"}],
            "tool_use_result": f"Error: {text}",
            "is_error": True,
        }

    def _record_output_file(self, record: _ForkTaskRecord) -> str | None:
        return self._child_session_path(record.child_session_id)

    def _record_output_snapshot(self, record: _ForkTaskRecord) -> str:
        output_file = self._record_output_file(record)
        if output_file is None:
            return ""
        try:
            return Path(output_file).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _record_status_for_notification(self, record: _ForkTaskRecord) -> str:
        if record.status == "completed":
            return "completed"
        if record.status == "stopped":
            return "stopped"
        return "failed"

    def _record_status_label(self, record: _ForkTaskRecord) -> str:
        if record.status == "completed":
            return "completed"
        if record.status == "stopped":
            return "stopped"
        if record.status == "failed" and record.error == "timed out":
            return "timed out"
        return record.status

    def _record_summary(self, record: _ForkTaskRecord) -> str:
        label = record.description or self._current_topic_title(record.child_route)
        status = self._record_status_label(record)
        task_noun = "Fork" if record.is_fork else "Agent task"
        if label:
            return f'{task_noun} "{label}" {status}'
        return f"{task_noun} {status}"

    def _build_fork_task_notification_xml(self, record: _ForkTaskRecord) -> str:
        lines = [
            "<task-notification>",
            f"  <task-id>{html.escape(record.task_id)}</task-id>",
        ]
        if record.tool_use_id:
            lines.append(f"  <tool-use-id>{html.escape(record.tool_use_id)}</tool-use-id>")
        lines.extend(
            [
                f"  <status>{html.escape(self._record_status_for_notification(record))}</status>",
                f"  <summary>{html.escape(self._record_summary(record))}</summary>",
                "  <result>",
                html.escape(record.result_text or record.error or ""),
                "  </result>",
                "  <usage>",
                f"    <total_tokens>{record.usage_total_tokens or 0}</total_tokens>",
                f"    <tool_uses>{record.usage_tool_uses or 0}</tool_uses>",
                f"    <duration_ms>{record.usage_duration_ms or 0}</duration_ms>",
                "  </usage>",
                "</task-notification>",
            ]
        )
        output_file = self._record_output_file(record)
        if output_file:
            lines.append(f"Full transcript available at: {output_file}")
        child_link = None
        if record.child_completion_message_id is not None:
            child_link = self._build_message_link(
                record.child_route, record.child_completion_message_id
            )
        if child_link:
            lines.append(f"Telegram topic: {child_link}")
        return "\n".join(lines)

    def _build_fork_task_output_text(
        self,
        *,
        record: _ForkTaskRecord,
        retrieval_status: str,
        status: str,
        output: str | None,
    ) -> str:
        parts = [f"<retrieval_status>{retrieval_status}</retrieval_status>"]
        parts.append("")
        parts.append(f"<task_id>{record.task_id}</task_id>")
        parts.append("")
        parts.append("<task_type>local_agent</task_type>")
        parts.append("")
        parts.append(f"<status>{status}</status>")
        if output:
            parts.append("")
            parts.append("<output>")
            parts.append(output)
            parts.append("</output>")
        return "\n".join(parts)

    def _build_fork_task_output_result(
        self,
        *,
        record: _ForkTaskRecord,
        retrieval_status: str,
        status: str,
        output: str | None,
    ) -> dict[str, Any]:
        task = {
            "task_id": record.task_id,
            "task_type": "local_agent",
            "status": status,
            "result": output or "",
        }
        if record.description:
            task["description"] = record.description
        if output:
            task["output"] = output
        return {
            "content": [
                {
                    "type": "text",
                    "text": self._build_fork_task_output_text(
                        record=record,
                        retrieval_status=retrieval_status,
                        status=status,
                        output=output,
                    ),
                }
            ],
            "tool_use_result": {"retrieval_status": retrieval_status, "task": task},
        }

    def _build_fork_task_callback_payload(self, record: _ForkTaskRecord) -> str:
        return self._build_fork_task_notification_xml(record)

    def _build_fork_task_terminal_html(
        self,
        *,
        record: _ForkTaskRecord,
        parent_callback_link: str | None,
    ) -> str:
        task_prefix = "fork task" if record.is_fork else "agent task"
        lines = [f"{task_prefix} {html.escape(self._record_status_label(record))}"]
        if parent_callback_link:
            lines.append(
                f'returned to <a href="{html.escape(parent_callback_link)}">parent topic</a>'
            )
        return "\n".join(lines)

    def _coerce_timeout_ms(self, value: Any) -> int:
        if value is None:
            return int(self._config.bg_fork_timeout * 1000)
        return max(int(value), 1)

    def _coerce_max_turns(self, value: Any) -> int | None:
        if value is None:
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None

    def _resolve_fork_source(
        self,
        *,
        state: TelegramSessionState,
        reply_message_id: int | None = None,
    ) -> tuple[str | None, str | None, TelegramRoute, int | None]:
        source_binding = (
            self._message_map.get((state.route.chat_id, reply_message_id))
            if isinstance(reply_message_id, int)
            else None
        )
        if source_binding is not None:
            return (
                source_binding.session_id,
                source_binding.jsonl_uuid,
                source_binding.route,
                reply_message_id,
            )

        source_session_id = state.session_id
        source_uuid = self._session_heads.get(source_session_id or "")
        source_route = state.route
        if source_session_id and not source_uuid:
            # Tool callbacks can race slightly ahead of session-head bookkeeping.
            # Fall back to the latest persisted UUID for this session.
            persisted = self._persisted_session_uuids(source_session_id)
            if persisted:
                source_uuid = persisted[-1]
        located = (
            self._find_bound_message_id(
                session_id=source_session_id,
                jsonl_uuid=source_uuid,
                preferred_route=state.route,
            )
            if source_session_id and source_uuid
            else None
        )
        source_message_id = located[1] if located else None
        if source_session_id and source_uuid:
            resolved_uuid, resolved_route, resolved_message_id = self._resolve_persisted_fork_source(
                session_id=source_session_id,
                preferred_uuid=source_uuid,
                preferred_route=source_route,
            )
            if resolved_uuid != source_uuid:
                logger.info(
                    "[fork_source] falling back to persisted uuid route=%s session_id=%s head_uuid=%s persisted_uuid=%s",
                    state.route,
                    source_session_id,
                    source_uuid,
                    resolved_uuid,
                )
                source_uuid = resolved_uuid
                source_route = resolved_route
                source_message_id = resolved_message_id
        return source_session_id, source_uuid, source_route, source_message_id

    async def _ensure_background_poller(self, bot: Any | None) -> None:
        if bot is not None:
            self._primary_bot = bot
        if not self._enable_background_poller:
            return
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = asyncio.create_task(self._background_poller_loop())

    async def shutdown(self) -> None:
        """Stop background tasks owned by this adapter."""
        if self._background_task is not None and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        if self._transport_worker_task is not None and not self._transport_worker_task.done():
            self._transport_worker_task.cancel()
            try:
                await self._transport_worker_task
            except asyncio.CancelledError:
                pass
        for task in list(self._typing_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._typing_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in list(self._observability_flush_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._observability_flush_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in list(self._fork_task_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._fork_task_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._state_store.close()

    async def _send_plain_with_retry(
        self,
        *,
        route: TelegramRoute,
        bot,
        text: str,
        disable_notification: bool,
        parse_mode: ParseMode | None = None,
        reply_to_message_id: int | None = None,
        priority: int = _PRIORITY_ASSISTANT,
        max_attempts: int | None = None,
    ):
        """Enqueue one Telegram message for paced prioritized transport delivery."""
        return await self._enqueue_send(
            route=route,
            text=text,
            disable_notification=disable_notification,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            fallback_bot=bot,
            priority=priority,
            max_attempts=max_attempts,
        )

    def _format_system_html(self, text: str) -> str:
        return f"<u><i>{html.escape(text)}</i></u>"

    def _wrap_system_html(self, html_text: str, *, underline: bool = True) -> str:
        if underline:
            return f"<u><i>{html_text}</i></u>"
        return f"<i>{html_text}</i>"

    def _underline_first_nonempty_line_html(self, html_text: str) -> str:
        lines = html_text.splitlines()
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("<u>") and stripped.endswith("</u>"):
                break
            lines[idx] = f"<u>{stripped}</u>"
            break
        return "\n".join(lines)

    def _format_status_html(self, text: str) -> str:
        return f"<i>{html.escape(text)}</i>"

    def _remember_system_message_id(self, *, route: TelegramRoute, message_id: int | None) -> None:
        if isinstance(message_id, int):
            key = (route.chat_id, message_id)
            self._system_message_ids.add(key)
            self._system_message_routes[key] = route
            self._state_store.upsert_system_message(
                chat_id=route.chat_id,
                message_id=message_id,
                route_chat_id=route.chat_id,
                route_thread_id=route.thread_id,
            )

    def _remember_system_message_ids(self, *, route: TelegramRoute, sent_messages: list) -> None:
        for sent in sent_messages:
            self._remember_system_message_id(
                route=route,
                message_id=self._sent_message_id(sent),
            )

    def _is_system_message_id(self, *, chat_id: int, message_id: int) -> bool:
        return (chat_id, message_id) in self._system_message_ids

    async def _send_system_message(
        self,
        *,
        route: TelegramRoute,
        bot,
        text: str,
        disable_notification: bool,
        reply_to_message_id: int | None = None,
        priority: int = _PRIORITY_SYSTEM,
        max_attempts: int | None = None,
    ):
        sent = await self._send_plain_with_retry(
            route=route,
            bot=bot,
            text=self._format_system_html(text),
            parse_mode=ParseMode.HTML,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
            priority=priority,
            max_attempts=max_attempts,
        )
        self._remember_system_message_id(
            route=route,
            message_id=self._sent_message_id(sent),
        )
        return sent

    async def _send_system_html_message(
        self,
        *,
        route: TelegramRoute,
        bot,
        html_text: str,
        disable_notification: bool,
        reply_to_message_id: int | None = None,
        priority: int = _PRIORITY_SYSTEM,
        underline: bool = True,
        max_attempts: int | None = None,
    ) -> list:
        sent_messages = await self._send_html(
            route=route,
            bot=bot,
            html_text=self._wrap_system_html(html_text, underline=underline),
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
            priority=priority,
            max_attempts=max_attempts,
        )
        self._remember_system_message_ids(route=route, sent_messages=sent_messages)
        return sent_messages

    def _active_schedules_for_route(self, route: TelegramRoute) -> list[_TopicScheduleRecord]:
        schedule_ids = self._schedule_ids_by_route.get(route, set())
        records = [
            self._topic_schedules_by_id[schedule_id]
            for schedule_id in schedule_ids
            if schedule_id in self._topic_schedules_by_id
        ]
        now = time.time()
        active: list[_TopicScheduleRecord] = []
        for record in records:
            if not record.enabled:
                continue
            if self._schedule_is_exhausted(record, now):
                record.enabled = False
                record.next_run_at = None
                self._register_topic_schedule(record)
                continue
            active.append(record)
        return active

    def _format_duration_human(self, seconds: float | int) -> str:
        total = max(int(seconds), 0)
        if total == 0:
            return "0s"
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs and not parts:
            parts.append(f"{secs}s")
        return " ".join(parts[:2])

    def _schedule_display_name(self, record: _TopicScheduleRecord) -> str:
        if record.description:
            return record.description
        return record.schedule_id

    def _schedule_trigger_label(self, record: _TopicScheduleRecord) -> str:
        if record.trigger_kind == "on_topic_stop":
            return "on stop"
        if record.schedule_mode == "cron" and record.cron_expr:
            return f"cron {record.cron_expr}"
        if record.interval_seconds is not None:
            return f"every {self._format_duration_human(record.interval_seconds)}"
        return "interval"

    def _schedule_uses_second_precision(self, record: _TopicScheduleRecord) -> bool:
        if record.trigger_kind == "interval" and record.interval_seconds is not None:
            return record.interval_seconds < 60
        return False

    def _format_schedule_timestamp(
        self,
        *,
        ts: float,
        record: _TopicScheduleRecord,
        now_ts: float | None = None,
    ) -> str:
        include_seconds = self._schedule_uses_second_precision(record)
        now_utc = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), timezone.utc)
        value_utc = datetime.fromtimestamp(ts, timezone.utc)
        now_dt = now_utc.astimezone()
        value_dt = value_utc.astimezone()
        tz_label = value_dt.tzname() or value_dt.strftime("%z") or "local"
        time_fmt = "%H:%M:%S" if include_seconds else "%H:%M"
        if value_dt.date() == now_dt.date():
            return f"today at {value_dt.strftime(time_fmt)} {tz_label}"
        if value_dt.date() == (now_dt + timedelta(days=1)).date():
            return f"tomorrow at {value_dt.strftime(time_fmt)} {tz_label}"
        full_fmt = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
        return f"{value_dt.strftime(full_fmt)} {tz_label}"

    def _schedule_details(
        self,
        record: _TopicScheduleRecord,
        *,
        now_ts: float,
        remaining_offset: int = 0,
    ) -> list[str]:
        details: list[str] = [self._schedule_trigger_label(record)]
        if record.max_retry_attempts > 0:
            details.append(
                f"retries={record.retry_attempt_count}/{record.max_retry_attempts}"
            )
        if record.max_runs is not None:
            remaining = max(record.max_runs - record.run_count - remaining_offset, 0)
            details.append(f"remaining={remaining}")
        if record.from_ts is not None:
            details.append(
                "from="
                + self._format_schedule_timestamp(
                    ts=record.from_ts,
                    record=record,
                    now_ts=now_ts,
                )
            )
        if record.until_ts is not None:
            details.append(
                "until="
                + self._format_schedule_timestamp(
                    ts=record.until_ts,
                    record=record,
                    now_ts=now_ts,
                )
            )
        return details

    def _schedule_trigger_line(
        self,
        record: _TopicScheduleRecord,
        *,
        now_ts: float,
    ) -> str:
        details = self._schedule_details(
            record,
            now_ts=now_ts,
            remaining_offset=1,
        )
        if details:
            return (
                f"schedule_triggered: {self._schedule_display_name(record)} "
                f"({' ; '.join(details)})"
            )
        return f"schedule_triggered: {self._schedule_display_name(record)}"

    def _next_schedule_line(self, route: TelegramRoute) -> str | None:
        now = time.time()
        timed_records = [
            record
            for record in self._active_schedules_for_route(route)
            if record.next_run_at is not None
        ]
        record: _TopicScheduleRecord | None = None
        if timed_records:
            record = min(timed_records, key=lambda item: float(item.next_run_at or now))
        else:
            stop_records = [
                candidate
                for candidate in self._active_schedules_for_route(route)
                if candidate.trigger_kind == "on_topic_stop"
            ]
            if stop_records:
                record = stop_records[0]
        if record is None:
            return None

        if record.next_run_at is not None:
            run_at_text = self._format_schedule_timestamp(
                ts=record.next_run_at,
                record=record,
                now_ts=now,
            )
            pieces = [f"next_schedule: {self._schedule_display_name(record)} at {run_at_text}"]
        else:
            pieces = [f"next_schedule: {self._schedule_display_name(record)} on next stop"]
        details = self._schedule_details(record, now_ts=now)
        if details:
            pieces.append(f"({' ; '.join(details)})")
        return " ".join(pieces)

    def _build_completion_summary(
        self,
        state: TelegramSessionState,
        *,
        subtask_status: str | None = None,
        return_to_parent_link: str | None = None,
        triggered_schedule_id: str | None = None,
    ) -> str:
        snapshot = build_context_snapshot(
            session_id=state.session_id,
            data=state.hook_state.last_result_data,
            context_window_estimate_tokens=self._config.context_window_estimate_tokens,
            cwd=self._config.vault_path,
        )
        lines = [format_context_snapshot_compact(snapshot)]
        if subtask_status:
            lines.append(f"subtask: {subtask_status}")
        if return_to_parent_link:
            lines.append(f"return_to_parent: {return_to_parent_link}")
        next_schedule = self._next_schedule_line(state.route)
        if next_schedule:
            lines.append(next_schedule)
        return "\n".join(lines)

    def _has_queue_idle_state(self, state: TelegramSessionState) -> bool:
        return not state.pending_messages and state.hook_state.message_queue.empty()

    def _bot_for_state(self, state: TelegramSessionState) -> Any | None:
        return state.last_bot or self._primary_bot

    async def _activate_route_session(
        self,
        state: TelegramSessionState,
        session_id: str | None,
    ) -> None:
        current_id = state.session_id
        if current_id == session_id:
            return

        old_manager = state.session_manager
        state.session_manager = SessionManager(config=self._config, hook_state=state.hook_state)
        if session_id:
            state.session_manager.set_session_id(session_id)
            self._route_by_session_id[session_id] = state.route

        if current_id:
            self._route_by_session_id.pop(current_id, None)
        await old_manager.disconnect()
        self._persist_state_for_route(state.route)

    async def _reset_route_state(self, state: TelegramSessionState) -> None:
        self._last_inbound_message_id_by_route.pop(state.route, None)
        self._unbind_route_sessions(state.route)
        await state.session_manager.async_reset()
        state.pending_messages.clear()
        state.hook_state.reset()
        state.warning_sent = False
        self._prune_bindings_for_route(state.route)
        self._persist_state_for_route(state.route)

    async def _maybe_send_route_warning(self, state: TelegramSessionState) -> None:
        _ = state

    async def _send_received_marker(
        self,
        *,
        route: TelegramRoute,
        bot,
        reply_to_message_id: int | None,
    ) -> list[int]:
        receipt_message = await self._send_system_message(
            route=route,
            bot=bot,
            text="received",
            disable_notification=True,
            reply_to_message_id=reply_to_message_id,
            max_attempts=1,
        )
        receipt_message_id = self._sent_message_id(receipt_message)
        if receipt_message_id is None:
            return []
        return [receipt_message_id]

    def _record_message_binding(
        self,
        *,
        route: TelegramRoute,
        message_id: int,
        jsonl_uuid: str,
        session_id: str,
        role: str,
    ) -> None:
        self._message_map[(route.chat_id, message_id)] = _TelegramMessageBinding(
            jsonl_uuid=jsonl_uuid,
            session_id=session_id,
            role=role,
            route=route,
        )
        self._state_store.upsert_message_binding(
            chat_id=route.chat_id,
            message_id=message_id,
            session_id=session_id,
            jsonl_uuid=jsonl_uuid,
            role=role,
            route_chat_id=route.chat_id,
            route_thread_id=route.thread_id,
        )
        logger.info(
            "[message_map] route=%s telegram_msg_id=%d session_id=%s role=%s uuid=%s",
            route,
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
        state: TelegramSessionState,
        message_ids: list[int],
        jsonl_uuid: str,
        role: str,
        deferred_bindings: list[tuple[int, str, str]] | None = None,
    ) -> None:
        session_id = state.session_id
        for message_id in message_ids:
            if session_id:
                self._record_message_binding(
                    route=state.route,
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
        route: TelegramRoute,
        deferred_bindings: list[tuple[int, str, str]],
        session_id: str | None,
    ) -> None:
        if not session_id or not deferred_bindings:
            return
        while deferred_bindings:
            message_id, jsonl_uuid, role = deferred_bindings.pop(0)
            self._record_message_binding(
                route=route,
                message_id=message_id,
                jsonl_uuid=jsonl_uuid,
                session_id=session_id,
                role=role,
            )

    def _prune_bindings_for_route(self, route: TelegramRoute) -> None:
        stale = [
            key for key, binding in self._message_map.items()
            if binding.route == route
        ]
        for key in stale:
            self._message_map.pop(key, None)
            self._system_message_ids.discard(key)
            self._system_message_routes.pop(key, None)
        stale_system = [
            key
            for key, mapped_route in self._system_message_routes.items()
            if mapped_route == route
        ]
        for key in stale_system:
            self._system_message_ids.discard(key)
            self._system_message_routes.pop(key, None)
        self._state_store.delete_message_bindings_for_route(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
        )
        self._state_store.delete_system_messages_for_route(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
        )
        self._state_store.delete_team_worker_states_for_route(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
        )
        self._last_inbound_message_id_by_route.pop(route, None)
        self._persist_state_for_route(route)

    async def _await_persisted_session_uuid(
        self,
        *,
        session_id: str,
        timeout_seconds: float = 8.0,
    ) -> str | None:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            persisted = self._persisted_session_uuids(session_id)
            if persisted:
                return persisted[-1]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.1)

    def _nearest_binding_for_route_message(
        self,
        *,
        route: TelegramRoute,
        target_message_id: int,
        max_distance: int = 3,
    ) -> _TelegramMessageBinding | None:
        candidates: list[tuple[int, int, _TelegramMessageBinding]] = []
        for (chat_id, message_id), binding in self._message_map.items():
            if chat_id != route.chat_id or binding.route != route:
                continue
            if binding.role == "system":
                continue
            if self._is_system_message_id(chat_id=chat_id, message_id=message_id):
                continue
            distance = abs(message_id - target_message_id)
            candidates.append((distance, message_id, binding))
        if not candidates:
            return None
        distance, _message_id, binding = min(candidates, key=lambda item: (item[0], item[1]))
        if distance > max_distance:
            return None
        return binding

    def _refresh_session_head(
        self,
        *,
        state: TelegramSessionState,
        latest_turn_uuid: str | None,
        source: str,
    ) -> None:
        session_id = state.session_id
        if not session_id or not latest_turn_uuid:
            return
        if self._session_heads.get(session_id) == latest_turn_uuid:
            return
        self._set_session_head(
            session_id=session_id,
            jsonl_uuid=latest_turn_uuid,
        )
        logger.info(
            "[session_head] route=%s session_id=%s uuid=%s source=%s",
            state.route,
            session_id,
            latest_turn_uuid,
            source,
        )

    def _set_session_head(self, *, session_id: str, jsonl_uuid: str) -> None:
        if not session_id or not jsonl_uuid:
            return
        self._session_heads[session_id] = jsonl_uuid
        self._state_store.upsert_session_head(
            session_id=session_id,
            jsonl_uuid=jsonl_uuid,
        )

    async def _resolve_session_for_trigger(
        self,
        *,
        state: TelegramSessionState,
        trigger_message: QueuedMessage | None,
        bot,
    ) -> tuple[bool, int | None]:
        reply_to_user_message_id = trigger_message.telegram_message_id if trigger_message else None
        target_message_id = trigger_message.reply_to_message_id if trigger_message else None
        if target_message_id is None:
            return True, reply_to_user_message_id

        target_is_system = self._is_system_message_id(
            chat_id=state.route.chat_id,
            message_id=target_message_id,
        )
        binding = self._message_map.get((state.route.chat_id, target_message_id))
        if binding is None or binding.role == "system" or target_is_system:
            nearest = self._nearest_binding_for_route_message(
                route=state.route,
                target_message_id=target_message_id,
            )
            if nearest is not None:
                binding = nearest
        logger.info(
            "[reply_lookup] route=%s target_message_id=%s found=%s",
            state.route,
            target_message_id,
            bool(binding),
        )
        if binding is None:
            await self._send_system_message(
                route=state.route,
                bot=bot,
                text="can't fork from this message",
                disable_notification=True,
                reply_to_message_id=reply_to_user_message_id,
            )
            return False, reply_to_user_message_id

        latest_uuid = self._session_heads.get(binding.session_id)
        if latest_uuid == binding.jsonl_uuid:
            await self._activate_route_session(state, binding.session_id)
            return True, reply_to_user_message_id

        fork_session_id = fork_session_jsonl(
            session_id=binding.session_id,
            target_uuid=binding.jsonl_uuid,
            cwd=self._config.vault_path,
            new_session_id=str(uuid.uuid4()),
        )
        self._set_session_head(
            session_id=fork_session_id,
            jsonl_uuid=binding.jsonl_uuid,
        )
        await self._activate_route_session(state, fork_session_id)
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
        route = self._route_for_message(message)
        state = self._get_state(route)
        if state is not None:
            state.last_bot = context.bot
        if message.media_group_id is not None:
            media_group_key = (
                message.chat_id,
                user_id,
                route.thread_id,
                str(message.media_group_id),
            )
            if media_group_key not in self._media_group_receipt_ids:
                try:
                    self._media_group_receipt_ids[media_group_key] = await self._send_received_marker(
                        route=route,
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
                route=route,
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

    def _routes_in_chat(self, chat_id: int) -> list[TelegramRoute]:
        return [route for route in self._states_by_route if route.chat_id == chat_id]

    def _command_targets(
        self,
        *,
        route: TelegramRoute,
        args: list[str],
    ) -> tuple[list[TelegramSessionState], bool]:
        apply_all = len(args) == 1 and args[0].strip().lower() == "all"
        if apply_all:
            routes = self._routes_in_chat(route.chat_id)
            if route not in routes:
                routes.append(route)
            states = [self._get_state(candidate) for candidate in routes]
            return [state for state in states if state is not None], True
        state = self._get_state(route)
        return ([state] if state is not None else []), False

    async def _build_context_lines(self, state: TelegramSessionState) -> list[str]:
        snapshot = build_context_snapshot(
            session_id=state.session_id,
            data=state.hook_state.last_result_data,
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
        return format_context_snapshot_lines(snapshot)

    async def handle_clear(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /clear - reset current topic or all topics in the current chat."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        await self._ensure_background_poller(context.bot)
        route = self._route_for_message(update.effective_message)
        states, apply_all = self._command_targets(route=route, args=context.args)
        has_schedules = (
            any(bool(self._schedule_ids_by_route.get(state.route, set())) for state in states)
            if apply_all
            else bool(self._schedule_ids_by_route.get(route, set()))
        )
        for state in states:
            state.last_bot = context.bot
            state.hook_state.interrupt_flag = True
            state.hook_state.pause_queue_delivery = False
            self._mark_task_terminal_request(state.route, "failed")
            await self._cancel_route_fork_tasks(state.route, status="failed")

        for state in states:
            lock = self._get_route_lock(state.route)
            async with lock:
                await self._reset_route_state(state)

        await self._send_system_message(
            route=route,
            bot=context.bot,
            text=(
                (
                    "all topic sessions cleared; schedules were kept. "
                    "Use /unschedule all to remove schedules across this chat."
                )
                if apply_all and has_schedules
                else (
                    "session cleared; schedule was kept. Use /unschedule to remove this topic schedule."
                    if has_schedules
                    else ("all topic sessions cleared" if apply_all else "session cleared")
                )
            ),
            disable_notification=True,
        )

    async def handle_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Backward-compatible alias for /clear."""
        await self.handle_clear(update, context)

    async def handle_unschedule(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /unschedule - remove schedule(s) from this topic or chat (/unschedule all)."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        route = self._route_for_message(update.effective_message)
        states, apply_all = self._command_targets(route=route, args=context.args)
        if apply_all:
            deleted = 0
            for state in states:
                schedule_ids = sorted(self._schedule_ids_by_route.get(state.route, set()))
                for schedule_id in schedule_ids:
                    self._delete_topic_schedule(schedule_id)
                    deleted += 1
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text=(
                    f"unscheduled {deleted} schedule(s) across this chat"
                    if deleted > 0
                    else "no schedules attached to this chat"
                ),
                disable_notification=True,
            )
            return

        schedule_ids = sorted(self._schedule_ids_by_route.get(route, set()))
        if not schedule_ids:
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text="no schedules attached to this topic",
                disable_notification=True,
            )
            return

        deleted = 0
        if context.args:
            target_id = context.args[0].strip()
            record = self._topic_schedules_by_id.get(target_id)
            if record is None or record.route != route:
                await self._send_system_message(
                    route=route,
                    bot=context.bot,
                    text=f"no schedule found in this topic: {target_id}",
                    disable_notification=True,
                )
                return
            self._delete_topic_schedule(target_id)
            deleted = 1
        else:
            for schedule_id in schedule_ids:
                self._delete_topic_schedule(schedule_id)
                deleted += 1

        await self._send_system_message(
            route=route,
            bot=context.bot,
            text=f"unscheduled {deleted} schedule(s) for this topic",
            disable_notification=True,
        )

    async def handle_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /context - show session and context window info for this route."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        route = self._route_for_message(update.effective_message)
        state = self._get_state(route)
        if state is None:
            return
        lines = await self._build_context_lines(state)
        await update.effective_message.reply_text(
            "\n".join(lines),
            disable_web_page_preview=True,
        )

    async def handle_report(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /report [comment] - persist a debug case file for later analysis."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        route = self._route_for_message(update.effective_message)
        comment = " ".join(context.args).strip()
        try:
            report_path = self._write_case_report(
                route=route,
                trigger_message_id=update.effective_message.message_id,
                trigger_user_id=update.effective_user.id,
                comment=comment,
            )
        except Exception as exc:
            logger.exception("Failed writing /report case file")
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text=f"report failed: {exc}",
                disable_notification=True,
                reply_to_message_id=update.effective_message.message_id,
            )
            return

        await self._send_system_message(
            route=route,
            bot=context.bot,
            text=f"report saved: {report_path}",
            disable_notification=True,
            reply_to_message_id=update.effective_message.message_id,
        )

    async def handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop - interrupt current response and pause queued auto-resume."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        await self._ensure_background_poller(context.bot)
        route = self._route_for_message(update.effective_message)
        states, apply_all = self._command_targets(route=route, args=context.args)
        for state in states:
            state.last_bot = context.bot
            state.hook_state.interrupt_flag = True
            state.hook_state.pause_queue_delivery = True
            self._mark_task_terminal_request(state.route, "stopped")
        logger.info("Interrupt via /stop from user %d all=%s", update.effective_user.id, apply_all)
        await self._send_system_message(
            route=route,
            bot=context.bot,
            text="interrupt sent to all topics" if apply_all else "interrupt sent",
            disable_notification=True,
        )

    def _status_to_text(self, event: StatusEvent) -> str:
        if event.type == "queue_delivered":
            return ""
        return event.summary

    def _render_notification_status_html(self, event: StatusEvent) -> str:
        heading = event.summary.strip() or "notification"
        parts = [self._format_system_html(heading)]
        for line in event.messages or []:
            normalized = line.strip()
            if normalized:
                parts.append(self._format_status_html(normalized))
        return "\n".join(parts).strip()

    def _is_interrupt_marker_text(self, text: str) -> bool:
        normalized = text.strip()
        return (
            normalized.startswith("[Request interrupted by user")
            and normalized.endswith("]")
        )

    def _render_turn_html(self, turn_items: list[TextEvent | StatusEvent]) -> str:
        parts: list[str] = []
        for item in turn_items:
            if isinstance(item, TextEvent):
                if self._is_interrupt_marker_text(item.text):
                    parts.append(self._format_system_html(item.text.strip()))
                    continue
                rendered = md_to_telegram_html(item.text)
                if rendered:
                    parts.append(rendered)
            elif isinstance(item, StatusEvent):
                if item.type == "notification":
                    rendered = self._render_notification_status_html(item)
                    if rendered:
                        parts.append(rendered)
                    continue
                status_text = self._status_to_text(item)
                if status_text.strip():
                    parts.append(self._format_status_html(status_text))
        return "\n".join(parts).strip()

    def _turn_is_observability_only(self, turn_items: list[TextEvent | StatusEvent]) -> bool:
        if not turn_items:
            return False
        if any(isinstance(item, TextEvent) for item in turn_items):
            return False
        # Keep notification pushes visible immediately instead of coalescing them
        # into the low-priority observability buffer.
        if any(
            isinstance(item, StatusEvent) and item.type == "notification"
            for item in turn_items
        ):
            return False
        return True

    async def _flush_observability_buffer(self, *, route: TelegramRoute, bot) -> None:
        task = self._observability_flush_tasks.pop(route, None)
        if task is not None and not task.done():
            task.cancel()
        buffered = self._observability_buffer.pop(route, None)
        if not buffered:
            return
        html_text = "\n".join(
            chunk.html_text for chunk in buffered if chunk.html_text.strip()
        ).strip()
        if not html_text:
            return
        sent_messages = await self._send_html(
            route=route,
            bot=bot,
            html_text=html_text,
            disable_notification=True,
            priority=_PRIORITY_OBSERVABILITY,
        )
        latest_uuid: str | None = None
        latest_session_id: str | None = None
        for chunk in reversed(buffered):
            if chunk.jsonl_uuid and chunk.session_id:
                latest_uuid = chunk.jsonl_uuid
                latest_session_id = chunk.session_id
                break
        if latest_uuid and latest_session_id:
            for sent in sent_messages:
                message_id = self._sent_message_id(sent)
                if not isinstance(message_id, int):
                    continue
                self._record_message_binding(
                    route=route,
                    message_id=message_id,
                    jsonl_uuid=latest_uuid,
                    session_id=latest_session_id,
                    role="assistant",
                )

    def _schedule_observability_flush(self, *, route: TelegramRoute) -> None:
        existing = self._observability_flush_tasks.get(route)
        if existing is not None and not existing.done():
            return

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(_OBSERVABILITY_COALESCE_SECONDS)
                state = self._get_state(route, create=False)
                if state is None or state.last_bot is None:
                    return
                await self._flush_observability_buffer(route=route, bot=state.last_bot)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("observability flush failed route=%s", route, exc_info=True)
            finally:
                self._observability_flush_tasks.pop(route, None)

        self._observability_flush_tasks[route] = asyncio.create_task(_delayed_flush())

    async def _send_html(
        self,
        *,
        route: TelegramRoute,
        bot,
        html_text: str,
        disable_notification: bool,
        reply_to_message_id: int | None = None,
        priority: int = _PRIORITY_ASSISTANT,
        max_attempts: int | None = None,
    ) -> list:
        text = html_text.strip() if html_text.strip() else "(no response)"
        chunks = split_message(text)
        sent_messages: list = []

        for index, chunk in enumerate(chunks):
            if index > 0:
                await asyncio.sleep(_CHUNK_DELAY_SECONDS)
            try:
                sent = await self._send_plain_with_retry(
                    route=route,
                    bot=bot,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_notification=disable_notification,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                    priority=priority,
                    max_attempts=max_attempts,
                )
                sent_messages.append(sent)
            except BadRequest as exc:
                msg_lower = str(exc).lower()
                if "can't parse entities" in msg_lower:
                    logger.warning("HTML parse failed, sending plain text: %s", exc)
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    sent = await self._send_plain_with_retry(
                        route=route,
                        bot=bot,
                        text=plain,
                        disable_notification=disable_notification,
                        reply_to_message_id=reply_to_message_id if index == 0 else None,
                        priority=priority,
                        max_attempts=max_attempts,
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
                            route=route,
                            bot=bot,
                            text=sub,
                            disable_notification=disable_notification,
                            reply_to_message_id=(
                                reply_to_message_id
                                if index == 0 and sub_index == 0
                                else None
                            ),
                            priority=priority,
                            max_attempts=max_attempts,
                        )
                        sent_messages.append(sent)
                    continue
                raise
        return sent_messages

    async def _flush_turn(
        self,
        *,
        state: TelegramSessionState,
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
        if self._turn_is_observability_only(turn_items):
            self._observability_buffer.setdefault(state.route, []).append(
                _ObservabilityChunk(
                    html_text=html_text,
                    jsonl_uuid=jsonl_uuid,
                    session_id=state.session_id,
                )
            )
            self._schedule_observability_flush(route=state.route)
            return
        await self._flush_observability_buffer(route=state.route, bot=bot)
        logger.info(
            "[flush_turn] sending %d chars (%d items) route=%s",
            len(html_text), len(turn_items), state.route,
        )
        sent_messages = await self._send_html(
            route=state.route,
            bot=bot,
            html_text=html_text,
            disable_notification=True,
            priority=_PRIORITY_ASSISTANT,
        )
        session_id = state.session_id
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
                    route=state.route,
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
        state: TelegramSessionState,
        user_text: str,
        bot,
        trigger_message: QueuedMessage | None = None,
        extra_pending: list[QueuedMessage] | None = None,
        trigger_status_message_ids: list[int] | None = None,
        triggered_schedule_id: str | None = None,
        suppress_completion_summary: bool = False,
    ) -> _RunOutcome:
        pending_messages = list(state.pending_messages)
        if extra_pending:
            pending_messages.extend(extra_pending)
        state.pending_messages = []

        proceed, reply_to_message_id = await self._resolve_session_for_trigger(
            state=state,
            trigger_message=trigger_message,
            bot=bot,
        )
        if not proceed:
            return _RunOutcome(assistant_text="")

        runner = ConversationRunner(
            state.session_manager,
            state.hook_state,
            self._config,
            pending_messages=pending_messages,
        )

        turn_items: list[TextEvent | StatusEvent] = []
        deferred_bindings: list[tuple[int, str, str]] = []
        completion_sent = False
        event_count = 0
        turn_count = 0
        latest_turn_uuid: str | None = None
        captured_text_parts: list[str] = []
        failed = False
        error_text: str | None = None
        state.busy = True
        state.hook_state.execution_active = True
        state.last_bot = bot
        state.hook_state.pause_queue_delivery = False
        trigger_status_ids = list(trigger_status_message_ids or [])
        trigger_user_mapped = False
        logger.info(
            "[run_and_send] START route=%s msg=%s",
            state.route, user_text[:80],
        )

        try:
            working_message = None
            try:
                working_message = await self._send_system_message(
                    route=state.route,
                    bot=bot,
                    text="working",
                    disable_notification=True,
                    reply_to_message_id=reply_to_message_id,
                    max_attempts=3,
                )
            except Exception:
                logger.debug("Failed to send working marker", exc_info=True)
            working_message_id = self._sent_message_id(working_message) if working_message is not None else None
            if isinstance(working_message_id, int):
                trigger_status_ids.append(working_message_id)
            async for event in runner.run(user_text):
                event_count += 1
                self._flush_deferred_bindings(
                    route=state.route,
                    deferred_bindings=deferred_bindings,
                    session_id=state.session_id,
                )
                self._bind_state_session(state)
                self._refresh_session_head(
                    state=state,
                    latest_turn_uuid=latest_turn_uuid,
                    source="event_loop",
                )
                if isinstance(event, TextEvent | StatusEvent):
                    if isinstance(event, TextEvent) and event.text:
                        captured_text_parts.append(event.text)
                    turn_items.append(event)
                    continue

                if isinstance(event, TurnEndEvent):
                    if (
                        event.jsonl_uuid
                        and event.message_role == "user"
                        and not trigger_user_mapped
                        and trigger_message is not None
                        and isinstance(trigger_message.telegram_message_id, int)
                    ):
                        self._record_or_defer_message_bindings(
                            state=state,
                            message_ids=[trigger_message.telegram_message_id],
                            jsonl_uuid=event.jsonl_uuid,
                            role="user",
                            deferred_bindings=deferred_bindings,
                        )
                        if trigger_status_ids:
                            self._record_or_defer_message_bindings(
                                state=state,
                                message_ids=list(trigger_status_ids),
                                jsonl_uuid=event.jsonl_uuid,
                                role="system",
                                deferred_bindings=deferred_bindings,
                            )
                        if state.session_id:
                            self._set_session_head(
                                session_id=state.session_id,
                                jsonl_uuid=event.jsonl_uuid,
                            )
                        latest_turn_uuid = event.jsonl_uuid
                        self._refresh_session_head(
                            state=state,
                            latest_turn_uuid=latest_turn_uuid,
                            source="user_turn",
                        )
                        trigger_user_mapped = True
                        trigger_status_ids.clear()

                    turn_count += 1
                    logger.info(
                        "[run_and_send] TurnEnd #%d items=%d route=%s",
                        turn_count, len(turn_items), state.route,
                    )
                    mapped_uuid = (
                        event.jsonl_uuid
                        if event.jsonl_uuid and turn_items
                        else None
                    )
                    if mapped_uuid and trigger_status_ids:
                        self._record_or_defer_message_bindings(
                            state=state,
                            message_ids=trigger_status_ids,
                            jsonl_uuid=mapped_uuid,
                            role="assistant",
                            deferred_bindings=deferred_bindings,
                        )
                        trigger_status_ids.clear()
                    if mapped_uuid:
                        latest_turn_uuid = mapped_uuid
                        self._refresh_session_head(
                            state=state,
                            latest_turn_uuid=latest_turn_uuid,
                            source="assistant_turn",
                        )
                    await self._flush_turn(
                        state=state,
                        bot=bot,
                        turn_items=turn_items,
                        jsonl_uuid=mapped_uuid,
                        deferred_bindings=deferred_bindings,
                    )
                    turn_items.clear()
                    continue

                if isinstance(event, DoneEvent):
                    logger.info(
                        "[run_and_send] DoneEvent events=%d turns=%d route=%s",
                        event_count, turn_count, state.route,
                    )
                    # Safety flush in case a producer emitted text without TurnEndEvent.
                    if turn_items:
                        await self._flush_turn(
                            state=state,
                            bot=bot,
                            turn_items=turn_items,
                            deferred_bindings=deferred_bindings,
                        )
                        turn_items.clear()

                    self._flush_deferred_bindings(
                        route=state.route,
                        deferred_bindings=deferred_bindings,
                        session_id=state.session_id,
                    )
                    self._bind_state_session(state)
                    self._refresh_session_head(
                        state=state,
                        latest_turn_uuid=latest_turn_uuid,
                        source="done_event",
                    )
                    state.pending_messages = runner.remaining_pending
                    await self._flush_observability_buffer(route=state.route, bot=bot)
                    if not suppress_completion_summary and self._should_emit_completion_summary(state):
                        if triggered_schedule_id is None:
                            self._reanchor_interval_schedules_for_route(
                                route=state.route,
                                base_ts=time.time(),
                            )
                        summary_message = await self._send_system_message(
                            route=state.route,
                            bot=bot,
                            text=self._build_completion_summary(
                                state,
                                triggered_schedule_id=triggered_schedule_id,
                            ),
                            disable_notification=False,
                            reply_to_message_id=reply_to_message_id,
                        )
                        summary_message_id = self._sent_message_id(summary_message)
                        latest_uuid = latest_turn_uuid or self._session_heads.get(
                            state.session_id or ""
                        )
                        if (
                            summary_message_id is not None
                            and latest_uuid
                        ):
                            self._record_or_defer_message_bindings(
                                state=state,
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
                                state.session_id,
                            )
                        completion_sent = True
                    break

            state.pending_messages = runner.remaining_pending
            self._flush_deferred_bindings(
                route=state.route,
                deferred_bindings=deferred_bindings,
                session_id=state.session_id,
            )
            self._bind_state_session(state)
            self._refresh_session_head(
                state=state,
                latest_turn_uuid=latest_turn_uuid,
                source="post_run",
            )
            await self._flush_observability_buffer(route=state.route, bot=bot)

            if (
                not completion_sent
                and not suppress_completion_summary
                and self._should_emit_completion_summary(state)
            ):
                if triggered_schedule_id is None:
                    self._reanchor_interval_schedules_for_route(
                        route=state.route,
                        base_ts=time.time(),
                    )
                summary_message = await self._send_system_message(
                    route=state.route,
                    bot=bot,
                    text=self._build_completion_summary(
                        state,
                        triggered_schedule_id=triggered_schedule_id,
                    ),
                    disable_notification=False,
                    reply_to_message_id=reply_to_message_id,
                )
                summary_message_id = self._sent_message_id(summary_message)
                latest_uuid = latest_turn_uuid or self._session_heads.get(
                    state.session_id or ""
                )
                if (
                    summary_message_id is not None
                    and latest_uuid
                ):
                    self._record_or_defer_message_bindings(
                        state=state,
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
                        state.session_id,
                    )
                completion_sent = True

        except Exception as exc:
            logger.exception("Error in ConversationRunner")

            error_detail = f"{type(exc).__name__}: {exc}"
            if len(error_detail) > 200:
                error_detail = error_detail[:200] + "..."
            failed = True
            error_text = error_detail

            try:
                await self._send_system_message(
                    route=state.route,
                    bot=bot,
                    text=f"error: {error_detail}",
                    disable_notification=False,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception:
                logger.exception("Failed to send error reply")

            try:
                await state.session_manager.soft_reset()
                logger.info("Soft reset after error (session_id preserved)")
            except Exception:
                logger.warning("Soft reset failed", exc_info=True)

        finally:
            state.busy = False
            state.hook_state.execution_active = False
            try:
                await self._flush_observability_buffer(route=state.route, bot=bot)
            except Exception:
                logger.debug("Failed flushing observability buffer in finally", exc_info=True)
            # Always send the final completion summary so Telegram collectors
            # have a stable end-of-turn marker even on error paths.
            if (
                not completion_sent
                and not suppress_completion_summary
                and self._should_emit_completion_summary(state)
            ):
                try:
                    if triggered_schedule_id is None:
                        self._reanchor_interval_schedules_for_route(
                            route=state.route,
                            base_ts=time.time(),
                        )
                    summary_message = await self._send_system_message(
                        route=state.route,
                        bot=bot,
                        text=self._build_completion_summary(
                            state,
                            triggered_schedule_id=triggered_schedule_id,
                        ),
                        disable_notification=False,
                        reply_to_message_id=reply_to_message_id,
                    )
                    summary_message_id = self._sent_message_id(summary_message)
                    latest_uuid = latest_turn_uuid or self._session_heads.get(
                        state.session_id or ""
                    )
                    if (
                        summary_message_id is not None
                        and latest_uuid
                    ):
                        self._record_or_defer_message_bindings(
                            state=state,
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
                            state.session_id,
                        )
                except Exception:
                    logger.debug("Failed to send completion summary in finally", exc_info=True)

        if (
            not trigger_user_mapped
            and trigger_message is not None
            and isinstance(trigger_message.telegram_message_id, int)
            and latest_turn_uuid
        ):
            mapped_key = (state.route.chat_id, trigger_message.telegram_message_id)
            if mapped_key not in self._message_map:
                self._record_or_defer_message_bindings(
                    state=state,
                    message_ids=[trigger_message.telegram_message_id],
                    jsonl_uuid=latest_turn_uuid,
                    role="user",
                    deferred_bindings=deferred_bindings,
                )
            trigger_user_mapped = True
        if trigger_status_ids and latest_turn_uuid:
            self._record_or_defer_message_bindings(
                state=state,
                message_ids=list(trigger_status_ids),
                jsonl_uuid=latest_turn_uuid,
                role="system",
                deferred_bindings=deferred_bindings,
            )
            trigger_status_ids.clear()
        self._flush_deferred_bindings(
            route=state.route,
            deferred_bindings=deferred_bindings,
            session_id=state.session_id,
        )
        return _RunOutcome(
            assistant_text="\n".join(part for part in captured_text_parts if part).strip(),
            failed=failed,
            error=error_text,
        )

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

        route = self._route_for_message(update.effective_message)
        message_id = update.effective_message.message_id
        last_seen = self._last_inbound_message_id_by_route.get(route)
        if isinstance(last_seen, int) and message_id <= last_seen:
            logger.info(
                "Skipping duplicate/out-of-order inbound route=%s message_id=%s last_seen=%s",
                route,
                message_id,
                last_seen,
            )
            return
        self._last_inbound_message_id_by_route[route] = message_id
        state = self._get_state(route)
        if state is None:
            return
        self._persist_state_for_route(route)
        state.last_bot = context.bot
        state.warning_sent = False

        logger.info("Processing message from route %s: %s", route, user_text[:100])

        lock = self._get_route_lock(route)
        logger.info(
            "[process_message] route=%s lock.locked=%s busy=%s",
            route, lock.locked(), state.busy,
        )
        reply_to_message_id = None
        reply_to_message = update.effective_message.reply_to_message
        candidate_reply_id = getattr(reply_to_message, "message_id", None)
        if isinstance(candidate_reply_id, int):
            # Forum-topic sends often arrive as an implicit reply to the topic root.
            # That is routing metadata, not an explicit user fork target.
            if not (route.thread_id is not None and candidate_reply_id == route.thread_id):
                reply_to_message_id = candidate_reply_id
        incoming = QueuedMessage(
            text=user_text,
            telegram_message_id=update.effective_message.message_id,
            reply_to_message_id=reply_to_message_id,
        )
        if lock.locked() or state.busy:
            state.hook_state.message_queue.put_nowait(incoming)
            logger.info("[process_message] queued while busy route=%s", route)
            try:
                await self._send_received_marker(
                    route=route,
                    bot=context.bot,
                    reply_to_message_id=incoming.telegram_message_id,
                )
            except Exception:
                logger.debug("Failed to send receipt marker for queued message", exc_info=True)
            return

        # Pre-claim busy before status marker delivery to avoid a race where
        # a second inbound message slips between marker send and lock acquire.
        state.busy = True
        trigger_status_message_ids: list[int] = list(pre_sent_status_message_ids or [])
        if not trigger_status_message_ids:
            try:
                trigger_status_message_ids.extend(
                    await self._send_received_marker(
                        route=route,
                        bot=context.bot,
                        reply_to_message_id=incoming.telegram_message_id,
                    )
                )
            except Exception:
                logger.debug("Failed to send receipt marker", exc_info=True)
        for warning in user_warnings or []:
            try:
                warning_message = await self._send_system_message(
                    route=route,
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
        try:
            async with lock:
                logger.info("[process_message] lock acquired route=%s", route)
                queued_before = list(state.pending_messages)
                if state.hook_state.pause_queue_delivery:
                    queued_before.extend(_drain_queue(state.hook_state.message_queue))
                    state.hook_state.pause_queue_delivery = False
                await self._run_and_send(
                    state=state,
                    user_text=user_text,
                    bot=context.bot,
                    trigger_message=incoming,
                    extra_pending=queued_before if queued_before else None,
                    trigger_status_message_ids=trigger_status_message_ids,
                )
                logger.info("[process_message] _run_and_send returned route=%s", route)
        finally:
            if state.busy and not lock.locked():
                # _run_and_send normally clears this; this is fallback for
                # pre-lock failures.
                state.busy = False

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
                getattr(first_message, "message_thread_id", None),
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
                await self._process_stop_schedule_events()
                await self._run_due_interval_schedules()
                for state in list(self._states_by_route.values()):
                    bot = self._bot_for_state(state)
                    if bot is None:
                        continue
                    if state.busy or state.hook_state.pause_queue_delivery:
                        continue

                    queued = _drain_queue(state.hook_state.message_queue)
                    has_pending = bool(state.pending_messages)
                    if not queued and not has_pending:
                        continue

                    lock = self._get_route_lock(state.route)
                    if lock.locked():
                        for message in queued:
                            state.hook_state.message_queue.put_nowait(message)
                        continue

                    logger.info(
                        "Auto-delivering queued updates route=%s queued=%d pending=%d",
                        state.route, len(queued), len(state.pending_messages),
                    )
                    async with lock:
                        if state.busy or state.hook_state.pause_queue_delivery:
                            for message in queued:
                                state.hook_state.message_queue.put_nowait(message)
                            continue

                        await self._run_and_send(
                            state=state,
                            user_text=_AUTO_DELIVERY_PROMPT,
                            bot=bot,
                            trigger_message=(
                                (queued or state.pending_messages)[-1]
                                if (queued or state.pending_messages)
                                else None
                            ),
                            extra_pending=queued if queued else None,
                        )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Background poller iteration failed")

    def _build_message_link(self, route: TelegramRoute, message_id: int) -> str | None:
        chat_str = str(route.chat_id)
        if chat_str.startswith("-100"):
            chat_token = chat_str[4:]
        elif chat_str.startswith("-"):
            chat_token = chat_str[1:]
        else:
            return None
        if route.thread_id is None:
            return f"https://t.me/c/{chat_token}/{message_id}"
        return f"https://t.me/c/{chat_token}/{route.thread_id}/{message_id}"

    def _resolve_runtime_log_file(self) -> str | None:
        for key in ("OBS_RUNTIME_LOG_FILE", "OBS_TELEGRAM_LOG_FILE"):
            value = (os.environ.get(key) or "").strip()
            if value:
                return value
        return None

    def _write_case_report(
        self,
        *,
        route: TelegramRoute,
        trigger_message_id: int,
        trigger_user_id: int,
        comment: str,
    ) -> Path:
        now_utc = datetime.now(timezone.utc)
        timestamp_slug = now_utc.strftime("%Y%m%d-%H%M%S")
        thread_part = (
            f"topic{route.thread_id}"
            if route.thread_id is not None
            else "general"
        )
        filename = (
            f"case-{timestamp_slug}-chat{route.chat_id}-{thread_part}-{uuid.uuid4().hex[:8]}.md"
        )
        report_dir = self._config.claude_path / "reports" / "cases"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / filename

        state = self._get_state(route, create=False)
        session_id = state.session_id if state is not None else None
        session_head = self._session_heads.get(session_id or "")
        session_jsonl = (
            find_session_jsonl(session_id=session_id, cwd=self._config.vault_path)
            if session_id
            else None
        )

        bound = self._message_map.get((route.chat_id, trigger_message_id))
        bound_uuid = bound.jsonl_uuid if bound is not None else None
        bound_session = bound.session_id if bound is not None else None
        trigger_link = self._build_message_link(route, trigger_message_id)
        log_file = self._resolve_runtime_log_file()
        normalized_comment = comment.strip() or "(no comment provided)"

        lines = [
            "# Telegram Case Report",
            "",
            f"- Created (UTC): {now_utc.isoformat(timespec='seconds')}",
            f"- Trigger comment: {normalized_comment}",
            f"- Chat ID: {route.chat_id}",
            f"- Topic thread ID: {route.thread_id if route.thread_id is not None else '(General)'}",
            f"- Topic title: {self._current_topic_title(route)}",
            f"- Trigger message ID: {trigger_message_id}",
            f"- Trigger message link: {trigger_link or '(unavailable for this chat id)'}",
            f"- Trigger user ID: {trigger_user_id}",
            f"- Active route session ID: {session_id or '(none)'}",
            f"- Active route head UUID: {session_head or '(none)'}",
            f"- Trigger binding UUID: {bound_uuid or '(none)'}",
            f"- Trigger binding session ID: {bound_session or '(none)'}",
            f"- Session JSONL: {session_jsonl or '(none)'}",
            f"- State DB: {self._config.telegram_state_db_path}",
            f"- Runtime PID: {os.getpid()}",
            f"- Runtime log file: {log_file or '(OBS_RUNTIME_LOG_FILE not set)'}",
            "",
            "## Notes",
            "- This file was created by `/report` for later debugging.",
            "- Correlate with logs using timestamp + chat/topic/message identifiers above.",
            "",
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    def _find_bound_message_id(
        self,
        *,
        session_id: str,
        jsonl_uuid: str,
        preferred_route: TelegramRoute | None = None,
    ) -> tuple[TelegramRoute, int] | None:
        matches = [
            (binding.route, message_id, binding.role)
            for (chat_id, message_id), binding in self._message_map.items()
            if chat_id == (preferred_route.chat_id if preferred_route else chat_id)
            and binding.session_id == session_id
            and binding.jsonl_uuid == jsonl_uuid
            and (preferred_route is None or binding.route == preferred_route)
        ]
        if not matches:
            matches = [
                (binding.route, message_id, binding.role)
                for (_chat_id, message_id), binding in self._message_map.items()
                if binding.session_id == session_id and binding.jsonl_uuid == jsonl_uuid
            ]
        if not matches:
            return None
        preferred = [
            item
            for item in matches
            if item[2] != "system" and not self._is_system_message_id(
                chat_id=item[0].chat_id,
                message_id=item[1],
            )
        ]
        chosen = preferred if preferred else matches
        route, message_id, _role = max(chosen, key=lambda item: item[1])
        return route, message_id

    def _next_topic_title(self, state: TelegramSessionState, explicit: str | None) -> str:
        if explicit:
            return explicit[:128]
        return self._next_auto_child_title(state)

    def _update_topic_metadata_from_update(self, update: Update) -> None:
        if update.effective_message is None:
            return
        self._update_topic_metadata_from_message(update.effective_message)

    async def handle_forum_topic_created(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        _ = context
        self._update_topic_metadata_from_update(update)

    async def handle_forum_topic_edited(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        _ = context
        self._update_topic_metadata_from_update(update)

    async def _drop_route_state(
        self,
        route: TelegramRoute,
        *,
        terminal_status: str | None = None,
    ) -> None:
        flush_task = self._observability_flush_tasks.pop(route, None)
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
        self._observability_buffer.pop(route, None)
        if terminal_status:
            self._mark_task_terminal_request(route, terminal_status)
            await self._cancel_route_fork_tasks(route, status=terminal_status)
        stale_session_ids = [
            session_id
            for session_id, mapped_route in self._route_by_session_id.items()
            if mapped_route == route
        ]
        state = self._states_by_route.pop(route, None)
        self._route_locks.pop(route, None)
        self._prune_bindings_for_route(route)
        self._topic_metadata_by_route.pop(route, None)
        self._delete_topic_schedules_for_route(route)
        self._state_store.delete_route_state(
            chat_id=route.chat_id,
            thread_id=route.thread_id,
        )
        if state is None:
            return
        if state.session_id:
            stale_session_ids.append(state.session_id)
        self._unbind_route_sessions(route)
        for session_id in stale_session_ids:
            self._session_heads.pop(session_id, None)
            self._state_store.delete_session_head(session_id=session_id)
        try:
            await state.session_manager.disconnect()
        finally:
            state.hook_state.reset()

    async def _create_child_fork_topic(
        self,
        *,
        parent_state: TelegramSessionState,
        bot,
        source_session_id: str | None,
        source_uuid: str | None,
        source_route: TelegramRoute,
        source_message_id: int | None,
        topic_name: str,
        child_service_html: str,
        notify_on_completion: bool,
        is_fork: bool = True,
        team_name: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        if is_fork:
            if not source_session_id or not source_uuid:
                raise RuntimeError("Cannot create fork child topic: source session head is unavailable")
            child_session_id = fork_session_jsonl(
                session_id=source_session_id,
                target_uuid=source_uuid,
                cwd=self._config.vault_path,
                new_session_id=str(uuid.uuid4()),
            )
            self._set_session_head(
                session_id=child_session_id,
                jsonl_uuid=source_uuid,
            )
        else:
            child_session_id = str(uuid.uuid4())
        parent_icon = (
            parent_state.topic_icon_custom_emoji_id
            or self._topic_metadata_for_route(parent_state.route).icon_custom_emoji_id
        )

        forum_topic = await self._enqueue_create_topic(
            route=TelegramRoute(chat_id=parent_state.route.chat_id, thread_id=None),
            topic_name=topic_name,
            icon_custom_emoji_id=parent_icon,
            fallback_bot=bot,
            priority=_PRIORITY_SYSTEM,
        )
        thread_id = getattr(forum_topic, "message_thread_id", None)
        if not isinstance(thread_id, int):
            raise RuntimeError("create_forum_topic returned no message_thread_id")
        raw_child_icon = getattr(forum_topic, "icon_custom_emoji_id", None)
        child_icon = (
            raw_child_icon.strip()
            if isinstance(raw_child_icon, str) and raw_child_icon.strip()
            else parent_icon
        )

        child_route = TelegramRoute(chat_id=parent_state.route.chat_id, thread_id=thread_id)
        child_state = self._get_state(child_route, topic_title=topic_name)
        assert child_state is not None
        child_state.topic_title = topic_name
        child_state.topic_icon_custom_emoji_id = child_icon
        child_state.last_bot = bot
        child_state.warning_sent = False
        child_state.notify_on_completion = notify_on_completion
        self._set_topic_metadata(
            route=child_route,
            title=topic_name,
            icon_custom_emoji_id=child_icon,
        )
        self._inherit_topic_schedules(
            parent_route=parent_state.route,
            child_route=child_route,
            is_fork=is_fork,
        )
        await self._activate_route_session(child_state, child_session_id)
        child_state.session_manager.set_sdk_env_overrides(
            self._build_team_worker_env(
                team_name=team_name,
                agent_name=agent_name,
            )
        )

        source_link = (
            self._build_message_link(source_route, source_message_id)
            if source_message_id is not None
            else None
        )
        styled_service_html = self._underline_first_nonempty_line_html(child_service_html)
        service_messages = await self._send_system_html_message(
            route=child_route,
            bot=bot,
            html_text=styled_service_html,
            disable_notification=True,
            underline=False,
            max_attempts=_TASK_LAUNCH_SEND_MAX_ATTEMPTS,
        )
        service_message_id = self._sent_message_id(service_messages[0]) if service_messages else None
        if service_message_id is not None:
            if source_uuid is not None:
                self._record_message_binding(
                    route=child_route,
                    message_id=service_message_id,
                    jsonl_uuid=source_uuid,
                    session_id=child_session_id,
                    role="assistant",
                )
        session_marker_id: int | None = None
        session_marker_text = (
            f"session forked, your new session id is {child_session_id}"
            if is_fork
            else f"session launched, your new session id is {child_session_id}"
        )
        try:
            session_marker = await self._send_system_message(
                route=child_route,
                bot=bot,
                text=session_marker_text,
                disable_notification=True,
            )
            session_marker_id = self._sent_message_id(session_marker)
            if session_marker_id is not None:
                if source_uuid is not None:
                    self._record_message_binding(
                        route=child_route,
                        message_id=session_marker_id,
                        jsonl_uuid=source_uuid,
                        session_id=child_session_id,
                        role="assistant",
                    )
        except Exception:
            logger.debug("Failed sending fork session marker route=%s", child_route, exc_info=True)
        child_link = (
            self._build_message_link(child_route, service_message_id)
            if service_message_id is not None
            else None
        )
        return {
            "child_route": child_route,
            "child_state": child_state,
            "child_session_id": child_session_id,
            "child_link": child_link,
            "child_service_message_id": service_message_id,
            "child_session_marker_message_id": session_marker_id,
            "source_link": source_link,
        }

    def _build_fork_task_child_service_html(
        self,
        *,
        agent_id: str,
        description: str | None,
        prompt: str,
        source_link: str | None,
        max_turns: int | None = None,
        is_fork: bool = True,
        team_name: str | None = None,
        agent_name: str | None = None,
    ) -> str:
        lines = ["fork task launched by agent" if is_fork else "agent task launched by agent"]
        lines.append(
            html.escape(
                f"<fork_context><is_fork>{str(is_fork).lower()}</is_fork><agent_id>{agent_id}</agent_id></fork_context>"
            )
        )
        if source_link:
            lines.append(
                f'forked from <a href="{html.escape(source_link)}">source message</a>'
            )
        lines.append(f"agentId: {html.escape(agent_id)}")
        if description:
            lines.append(f"description: {html.escape(description)}")
        if team_name:
            lines.append(f"team_name: {html.escape(team_name)}")
        if agent_name:
            lines.append(f"agent_name: {html.escape(agent_name)}")
        if max_turns is not None:
            lines.append(f"max_turns: {max_turns}")
        lines.append("prompt:")
        lines.append(html.escape(prompt))
        return "\n".join(lines)

    def _build_fork_task_launch_text(
        self,
        *,
        task_id: str,
        output_file: str | None,
        topic_link: str | None,
        task_label: str,
    ) -> str:
        lines = [
            f"{task_label} launched successfully.",
            f"agentId: {task_id}",
            "The agent is working in the background. You will be notified automatically when it completes.",
        ]
        if output_file:
            lines.append(f"output_file: {output_file}")
        if topic_link:
            lines.append(f"telegram_topic: {topic_link}")
        return "\n".join(lines)

    def _build_super_task_lifecycle_html(
        self,
        *,
        record: _ForkTaskRecord,
        phase: str,
        elapsed_seconds: float | None = None,
        idle_seconds: float | None = None,
    ) -> str:
        heading = self._format_system_html(f"notification: agent task {phase}")
        body_lines = [f"agentId: {record.task_id}"]
        if record.description:
            body_lines.append(f"description: {record.description}")
        if elapsed_seconds is not None:
            body_lines.append(f"elapsed_s: {max(int(elapsed_seconds), 0)}")
        if idle_seconds is not None:
            body_lines.append(f"idle_for_s: {max(int(idle_seconds), 0)}")
        body_html = "\n".join(self._format_status_html(line) for line in body_lines)
        return "\n".join([heading, body_html]).strip()

    async def _send_super_task_lifecycle_notification(
        self,
        *,
        record: _ForkTaskRecord,
        parent_state: TelegramSessionState | None,
        phase: str,
        elapsed_seconds: float | None = None,
        idle_seconds: float | None = None,
    ) -> None:
        if record.is_fork:
            return
        if parent_state is None or parent_state.last_bot is None:
            return
        html_text = self._build_super_task_lifecycle_html(
            record=record,
            phase=phase,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
        )
        try:
            sent_messages = await self._send_html(
                route=record.parent_route,
                bot=parent_state.last_bot,
                html_text=html_text,
                disable_notification=True,
                reply_to_message_id=record.launch_parent_message_id,
                priority=_PRIORITY_SYSTEM,
            )
            self._remember_system_message_ids(route=record.parent_route, sent_messages=sent_messages)
        except Exception:
            logger.debug(
                "Failed sending AgentTask lifecycle notification task_id=%s phase=%s",
                record.task_id,
                phase,
                exc_info=True,
            )

    async def _monitor_super_task_lifecycle(
        self,
        *,
        record: _ForkTaskRecord,
        child_state: TelegramSessionState,
        parent_state: TelegramSessionState | None,
    ) -> None:
        if record.is_fork:
            return
        if parent_state is None or parent_state.last_bot is None:
            return
        now = time.time()
        last_activity = child_state.session_manager.last_activity or now
        await self._send_super_task_lifecycle_notification(
            record=record,
            parent_state=parent_state,
            phase="running",
            elapsed_seconds=0.0,
        )
        idle_reported = False
        while True:
            await asyncio.sleep(self._super_task_monitor_tick_seconds)
            task = self._fork_task_tasks.get(record.task_id)
            if task is None or task.done():
                return
            now = time.time()
            current_activity = child_state.session_manager.last_activity or last_activity
            if current_activity > last_activity:
                last_activity = current_activity
                if idle_reported:
                    await self._send_super_task_lifecycle_notification(
                        record=record,
                        parent_state=parent_state,
                        phase="running",
                        elapsed_seconds=now - record.created_at,
                    )
                    idle_reported = False
                    continue
            if (
                not idle_reported
                and (now - last_activity) >= self._super_task_idle_seconds
            ):
                await self._send_super_task_lifecycle_notification(
                    record=record,
                    parent_state=parent_state,
                    phase="idle",
                    elapsed_seconds=now - record.created_at,
                    idle_seconds=now - last_activity,
                )
                idle_reported = True
                continue

    def _build_idle_team_worker_prompt(
        self,
        *,
        team_name: str,
        agent_name: str,
        sender: str | None,
        summary: str | None,
        content: str | None,
    ) -> str:
        lines = [
            "(System: New teammate messages arrived while you were idle.)",
            f"Use ReadInbox with team_name={team_name}, agent={agent_name}, include_read=false, mark_read=true, limit=50.",
            "Process all unread teammate messages and continue team work.",
            "If needed, reply to teammates with SendInboxMessage.",
        ]
        sender_norm = (sender or "").strip()
        summary_norm = (summary or "").strip()
        content_norm = (content or "").strip()
        if sender_norm:
            lines.append(f"Latest sender: {sender_norm}.")
        if summary_norm:
            lines.append(f"Latest summary: {summary_norm}.")
        if content_norm:
            preview = content_norm if len(content_norm) <= 240 else f"{content_norm[:240]}..."
            lines.append(f"Latest content preview: {preview}")
        return "\n".join(lines)

    async def _start_idle_team_worker_wake(
        self,
        *,
        record: _ForkTaskRecord,
        sender: str | None,
        summary: str | None,
        content: str | None,
    ) -> None:
        if record.is_fork:
            return
        team_name = (record.team_name or "").strip()
        agent_name = (record.agent_name or "").strip()
        if not team_name or not agent_name:
            return
        child_state = self._get_state(record.child_route, create=False)
        if child_state is None:
            return
        bot = child_state.last_bot
        if bot is None:
            return

        sender_norm = (sender or "").strip() or None
        summary_norm = (summary or "").strip() or None
        content_norm = (content or "").strip() or None
        wake_html_lines = ["agent task wake: teammate message received"]
        wake_html_lines.append(f"team_name: {html.escape(team_name)}")
        wake_html_lines.append(f"agent_name: {html.escape(agent_name)}")
        if sender_norm:
            wake_html_lines.append(f"from: {html.escape(sender_norm)}")
        if summary_norm:
            wake_html_lines.append(f"summary: {html.escape(summary_norm)}")
        wake_messages = await self._send_system_html_message(
            route=record.child_route,
            bot=bot,
            html_text="\n".join(wake_html_lines),
            disable_notification=True,
            underline=False,
        )
        wake_message_id = self._sent_message_id(wake_messages[0]) if wake_messages else None
        wake_prompt = self._build_idle_team_worker_prompt(
            team_name=team_name,
            agent_name=agent_name,
            sender=sender_norm,
            summary=summary_norm,
            content=content_norm,
        )

        current_child_session_id = (child_state.session_id or "").strip() or record.child_session_id
        if current_child_session_id:
            record.child_session_id = current_child_session_id
        record.parent_route = record.child_route
        record.parent_session_id_at_launch = (
            current_child_session_id
            or record.parent_session_id_at_launch
        )
        record.parent_source_uuid = (
            self._session_heads.get(current_child_session_id or "")
            or record.parent_source_uuid
        )
        record.prompt = wake_prompt
        record.status = "launched"
        record.error = None
        record.terminal_request = None
        record.completed_at = None
        record.result_text = None
        record.parent_callback_message_id = None
        record.child_completion_message_id = None
        record.tool_use_id = None
        record.launch_tool_name = "AgentTask"
        record.launch_parent_message_id = wake_message_id
        record.launch_child_message_id = wake_message_id
        record.idle_ready = False
        record.wake_requested = False
        record.wake_source_sender = None
        record.wake_source_summary = None
        record.wake_source_content = None
        record.emit_parent_callback = False
        self._register_team_worker_record(record)
        await self._schedule_fork_task(
            task_id=record.task_id,
            parent_state=child_state,
        )

    async def _handle_inbox_message_notification(
        self,
        *,
        sender_route: TelegramRoute,
        payload: dict[str, Any],
    ) -> None:
        team_name = str(payload.get("team_name") or "").strip()
        recipient = str(payload.get("recipient") or "").strip()
        if not team_name or not recipient:
            return
        record = self._resolve_team_worker_record(
            team_name=team_name,
            agent_name=recipient,
        )
        if record is None:
            return
        if record.status in {"failed", "stopped"} or record.terminal_request in {"failed", "stopped"}:
            self._remove_team_worker_mappings_for_task(record.task_id)
            return

        running = self._fork_task_tasks.get(record.task_id)
        sender = str(payload.get("sender") or "").strip() or None
        summary = str(payload.get("summary") or "").strip() or None
        content = str(payload.get("content") or "").strip() or None
        if running is not None and not running.done():
            record.wake_requested = True
            record.wake_source_sender = sender
            record.wake_source_summary = summary
            record.wake_source_content = content
            return
        if record.status == "completed" and record.terminal_request is None:
            record.idle_ready = True
        if not record.idle_ready:
            return

        try:
            await self._start_idle_team_worker_wake(
                record=record,
                sender=sender,
                summary=summary,
                content=content,
            )
        except Exception:
            logger.warning(
                "Failed waking idle team worker from inbox notification route=%s task_id=%s sender_route=%s",
                record.child_route,
                record.task_id,
                sender_route,
                exc_info=True,
            )

    async def _schedule_fork_task(
        self,
        *,
        task_id: str,
        parent_state: TelegramSessionState,
    ) -> None:
        parent_state.active_fork_task_ids.add(task_id)
        task = asyncio.create_task(self._execute_fork_task(task_id))
        self._fork_task_tasks[task_id] = task
        task.add_done_callback(
            lambda done: self._fork_task_tasks.pop(task_id, None)
            if self._fork_task_tasks.get(task_id) is done
            else None
        )

    async def _resume_fork_task(
        self,
        *,
        route: TelegramRoute,
        state: TelegramSessionState,
        args: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        record = self._fork_tasks_by_id.get(task_id)
        if record is None:
            return self._task_not_found_result(task_id)
        task_label = "ForkTask" if record.is_fork else "AgentTask"
        existing_task = self._fork_task_tasks.get(task_id)
        if existing_task is not None and not existing_task.done():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{task_label} failed: task {task_id} is already running",
                    }
                ],
                "is_error": True,
            }

        prompt = str(args["prompt"])
        description = str(args.get("description") or "").strip() or record.description
        team_name = str(args.get("team_name") or "").strip() or record.team_name
        agent_name = (
            str(args.get("agent_name") or args.get("name") or "").strip()
            or record.agent_name
        )
        timeout_ms = self._coerce_timeout_ms(args.get("timeout_ms"))
        max_turns = self._coerce_max_turns(args.get("max_turns"))
        child_state = self._get_state(record.child_route, create=False)
        if child_state is None or state.last_bot is None:
            return self._task_not_found_result(task_id)
        record.parent_route = route
        record.parent_session_id_at_launch = state.session_id or record.parent_session_id_at_launch
        record.parent_source_uuid = self._session_heads.get(state.session_id or "") or record.parent_source_uuid
        record.prompt = prompt
        record.description = description
        record.timeout_ms = timeout_ms
        record.max_turns = max_turns
        record.team_name = team_name
        record.agent_name = agent_name
        record.tool_use_id = str(args.get("tool_use_id") or "").strip() or None
        launch_tool_name = str(args.get("task_tool_name") or "").strip()
        if launch_tool_name:
            record.launch_tool_name = launch_tool_name
        record.status = "launched"
        record.error = None
        record.terminal_request = None
        record.result_text = None
        record.completed_at = None
        record.parent_callback_message_id = None
        record.child_completion_message_id = None
        child_state.last_bot = state.last_bot
        record.idle_ready = False
        record.wake_requested = False
        record.wake_source_sender = None
        record.wake_source_summary = None
        record.wake_source_content = None
        record.emit_parent_callback = True
        self._register_team_worker_record(record)
        child_state.session_manager.set_sdk_env_overrides(
            self._build_team_worker_env(
                team_name=record.team_name,
                agent_name=record.agent_name,
            )
        )

        resume_service_html = self._build_fork_task_child_service_html(
            agent_id=task_id,
            description=description,
            prompt=prompt,
            source_link=None,
            max_turns=max_turns,
            is_fork=record.is_fork,
            team_name=record.team_name,
            agent_name=record.agent_name,
        )
        resume_service_messages = await self._send_system_html_message(
            route=record.child_route,
            bot=state.last_bot,
            html_text=self._underline_first_nonempty_line_html(resume_service_html),
            disable_notification=True,
            underline=False,
            max_attempts=_TASK_LAUNCH_SEND_MAX_ATTEMPTS,
        )
        record.launch_child_message_id = (
            self._sent_message_id(resume_service_messages[0]) if resume_service_messages else None
        )
        if record.launch_child_message_id is not None:
            current_child_session_id = child_state.session_id or record.child_session_id
            if current_child_session_id:
                record.child_session_id = current_child_session_id
            latest_child_uuid = self._session_heads.get(current_child_session_id or "") or record.parent_source_uuid
            self._record_message_binding(
                route=record.child_route,
                message_id=record.launch_child_message_id,
                jsonl_uuid=latest_child_uuid,
                session_id=current_child_session_id or record.child_session_id,
                role="assistant",
            )
        child_link = (
            self._build_message_link(record.child_route, record.launch_child_message_id)
            if record.launch_child_message_id is not None
            else None
        )
        parent_html = "fork task resumed" if record.is_fork else "agent task resumed"
        if child_link:
            parent_html = (
                f'{"fork task resumed" if record.is_fork else "agent task resumed"}: '
                f'<a href="{html.escape(child_link)}">'
                f"{html.escape(self._current_topic_title(record.child_route))}</a>"
            )
        parent_messages = await self._send_system_html_message(
            route=route,
            bot=state.last_bot,
            html_text=parent_html,
            disable_notification=True,
            max_attempts=_TASK_LAUNCH_SEND_MAX_ATTEMPTS,
        )
        record.launch_parent_message_id = (
            self._sent_message_id(parent_messages[0]) if parent_messages else None
        )
        await self._schedule_fork_task(task_id=task_id, parent_state=state)
        return {
            "content": [
                {
                    "type": "text",
                    "text": self._build_fork_task_launch_text(
                        task_id=task_id,
                        output_file=self._record_output_file(record),
                        topic_link=child_link,
                        task_label=record.launch_tool_name,
                    ),
                }
            ]
        }

    async def _launch_fork_task(
        self,
        *,
        route: TelegramRoute,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._get_state(route, create=False)
        if state is None or state.last_bot is None:
            raise RuntimeError("ForkTask is only available inside an active Telegram topic")

        resume_task_id = self._normalize_resume_task_id(args.get("resume"))
        if resume_task_id:
            return await self._resume_fork_task(
                route=route,
                state=state,
                args=args,
                task_id=resume_task_id,
            )

        is_fork = self._coerce_fork_flag(args.get("fork"))
        launch_tool_name = str(args.get("task_tool_name") or "").strip() or (
            "ForkTask" if is_fork else "AgentTask"
        )
        team_name = str(args.get("team_name") or "").strip() or None
        agent_name = str(args.get("agent_name") or args.get("name") or "").strip() or None
        source_session_id: str | None = None
        source_uuid: str | None = None
        source_route = route
        source_message_id: int | None = None
        if is_fork:
            source_session_id, source_uuid, source_route, source_message_id = self._resolve_fork_source(
                state=state
            )
            fallback_session_id = str(args.get("session_id") or "").strip() or None
            if not source_session_id and fallback_session_id:
                source_session_id = fallback_session_id
            if source_session_id and not source_uuid:
                persisted_uuid = await self._await_persisted_session_uuid(
                    session_id=source_session_id,
                    timeout_seconds=8.0,
                )
                if persisted_uuid:
                    source_uuid = persisted_uuid
                    self._set_session_head(
                        session_id=source_session_id,
                        jsonl_uuid=persisted_uuid,
                    )
                    resolved_uuid, resolved_route, resolved_message_id = self._resolve_persisted_fork_source(
                        session_id=source_session_id,
                        preferred_uuid=persisted_uuid,
                        preferred_route=source_route,
                    )
                    source_uuid = resolved_uuid
                    source_route = resolved_route
                    source_message_id = resolved_message_id
            if not source_session_id or not source_uuid:
                raise RuntimeError("Cannot launch ForkTask yet: no mapped head in this topic")

        task_id = str(uuid.uuid4())
        description = str(args.get("description") or "").strip() or None
        prompt = str(args["prompt"])
        timeout_ms = self._coerce_timeout_ms(args.get("timeout_ms"))
        max_turns = self._coerce_max_turns(args.get("max_turns"))
        topic_name = self._build_fork_task_topic_name(
            state=state,
            description=description,
        )
        source_link = (
            self._build_message_link(source_route, source_message_id)
            if is_fork and source_message_id is not None
            else None
        )
        child_service_html = self._build_fork_task_child_service_html(
            agent_id=task_id,
            description=description,
            prompt=prompt,
            source_link=source_link,
            max_turns=max_turns,
            is_fork=is_fork,
            team_name=team_name,
            agent_name=agent_name,
        )
        created = await self._create_child_fork_topic(
            parent_state=state,
            bot=state.last_bot,
            source_session_id=source_session_id,
            source_uuid=source_uuid,
            source_route=source_route,
            source_message_id=source_message_id,
            topic_name=topic_name,
            child_service_html=child_service_html,
            notify_on_completion=True,
            is_fork=is_fork,
            team_name=team_name,
            agent_name=agent_name,
        )
        child_route: TelegramRoute = created["child_route"]
        child_service_message_id: int | None = created["child_service_message_id"]
        child_link: str | None = created["child_link"]

        confirmation = "fork task launched" if is_fork else "agent task launched"
        if child_link:
            confirmation = (
                f'{"fork task launched" if is_fork else "agent task launched"}: '
                f'<a href="{html.escape(child_link)}">'
                f"{html.escape(topic_name)}</a>"
            )
        confirmation_messages = await self._send_system_html_message(
            route=route,
            bot=state.last_bot,
            html_text=confirmation,
            disable_notification=True,
            max_attempts=_TASK_LAUNCH_SEND_MAX_ATTEMPTS,
        )
        confirmation_id = (
            self._sent_message_id(confirmation_messages[0]) if confirmation_messages else None
        )
        if confirmation_id is not None and source_uuid and source_session_id:
            self._record_message_binding(
                route=route,
                message_id=confirmation_id,
                jsonl_uuid=source_uuid,
                session_id=source_session_id,
                role="assistant",
            )

        record = _ForkTaskRecord(
            task_id=task_id,
            tool_use_id=str(args.get("tool_use_id") or "").strip() or None,
            parent_route=route,
            parent_session_id_at_launch=source_session_id or (state.session_id or ""),
            parent_source_uuid=source_uuid or "",
            child_route=child_route,
            child_session_id=created["child_session_id"],
            prompt=prompt,
            description=description,
            timeout_ms=timeout_ms,
            max_turns=max_turns,
            launch_parent_message_id=confirmation_id,
            launch_child_message_id=child_service_message_id,
            is_fork=is_fork,
            launch_tool_name=launch_tool_name,
            team_name=team_name,
            agent_name=agent_name,
        )
        self._fork_tasks_by_id[task_id] = record
        self._fork_task_by_child_route[child_route] = task_id
        self._register_team_worker_record(record)
        await self._schedule_fork_task(task_id=task_id, parent_state=state)
        return {
            "content": [
                {
                    "type": "text",
                    "text": self._build_fork_task_launch_text(
                        task_id=task_id,
                        output_file=self._record_output_file(record),
                        topic_link=child_link,
                        task_label=launch_tool_name,
                    ),
                }
            ]
        }

    @staticmethod
    def _normalize_resume_task_id(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        if normalized.lower() in {"false", "none", "null", "nil", "0", "no"}:
            return None
        return normalized

    @staticmethod
    def _coerce_fork_flag(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return True

    async def _execute_fork_task(self, task_id: str) -> None:
        record = self._fork_tasks_by_id.get(task_id)
        if record is None:
            return

        child_state = self._get_state(record.child_route, create=False)
        parent_state = self._get_state(record.parent_route, create=False)
        bot = (
            (child_state.last_bot if child_state is not None else None)
            or (parent_state.last_bot if parent_state is not None else None)
        )
        if child_state is None or bot is None:
            record.status = record.terminal_request or "failed"
            record.error = record.error or "child route unavailable before launch"
            record.completed_at = time.time()
            if parent_state is not None:
                parent_state.active_fork_task_ids.discard(task_id)
            return

        current_child_session_id = (child_state.session_id or "").strip()
        if current_child_session_id and current_child_session_id != record.child_session_id:
            record.child_session_id = current_child_session_id

        child_lock = self._get_route_lock(record.child_route)
        lifecycle_monitor_task: asyncio.Task | None = None
        if not record.is_fork and parent_state is not None and parent_state.last_bot is not None:
            lifecycle_monitor_task = asyncio.create_task(
                self._monitor_super_task_lifecycle(
                    record=record,
                    child_state=child_state,
                    parent_state=parent_state,
                )
            )
        try:
            async with child_lock:
                is_fork_text = "true" if record.is_fork else "false"
                fork_context_prefix = (
                    "(System: "
                    "<fork_context>"
                    f"<is_fork>{is_fork_text}</is_fork>"
                    f"<agent_id>{record.task_id}</agent_id>"
                    f"<session_id>{record.child_session_id}</session_id>"
                    "</fork_context>"
                    " You are running inside a delegated child topic.)\n\n"
                )
                team_context_prefix = ""
                if record.team_name:
                    team_name = record.team_name.strip()
                    agent_name = (record.agent_name or "").strip() or "unassigned"
                    team_context_prefix = (
                        "(System: "
                        "<team_context>"
                        f"<team_name>{team_name}</team_name>"
                        f"<agent_name>{agent_name}</agent_name>"
                        "</team_context>"
                        " Team task tools are enabled for this worker. For teammate "
                        "messaging, prefer SendInboxMessage and ReadInbox over native "
                        "SendMessage.)\n\n"
                    )
                child_prompt = f"{fork_context_prefix}{team_context_prefix}{record.prompt}"
                if record.max_turns is not None:
                    child_prompt = (
                        f"(System: complete this task in at most {record.max_turns} turns.)\n\n"
                        f"{child_prompt}"
                    )
                outcome_coro = self._run_and_send(
                    state=child_state,
                    user_text=child_prompt,
                    bot=bot,
                    trigger_message=QueuedMessage(
                        text=child_prompt,
                        telegram_message_id=record.launch_child_message_id,
                    ),
                )
                if record.timeout_ms is not None:
                    outcome = await asyncio.wait_for(
                        outcome_coro,
                        timeout=record.timeout_ms / 1000.0,
                    )
                else:
                    outcome = await outcome_coro
            record.result_text = outcome.assistant_text or None
            if record.terminal_request:
                record.status = record.terminal_request
            elif outcome.failed:
                record.status = "failed"
                record.error = outcome.error
            else:
                record.status = "completed"
        except asyncio.TimeoutError:
            record.status = record.terminal_request or "failed"
            record.error = "timed out"
        except Exception as exc:
            record.status = record.terminal_request or "failed"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            record.completed_at = time.time()
            result_data = child_state.hook_state.last_result_data if child_state is not None else None
            usage = result_data.get("usage") if isinstance(result_data, dict) else None
            if isinstance(usage, dict):
                record.usage_total_tokens = sum(
                    int(usage.get(key) or 0)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
            if isinstance(result_data, dict):
                duration = result_data.get("duration_ms")
                if duration is not None:
                    record.usage_duration_ms = int(duration)
            output_snapshot = self._record_output_snapshot(record)
            record.usage_tool_uses = output_snapshot.count('"type":"tool_use"')
            if lifecycle_monitor_task is not None and not lifecycle_monitor_task.done():
                lifecycle_monitor_task.cancel()
                try:
                    await lifecycle_monitor_task
                except asyncio.CancelledError:
                    pass

        is_team_worker = (
            not record.is_fork
            and bool((record.team_name or "").strip())
            and bool((record.agent_name or "").strip())
        )
        if (
            record.status == "completed"
            and record.terminal_request is None
            and is_team_worker
        ):
            record.idle_ready = True
        elif record.status in {"failed", "stopped"}:
            record.idle_ready = False

        if parent_state is not None:
            parent_state.active_fork_task_ids.discard(task_id)

        if record.terminal_request == "failed":
            self._fork_task_by_child_route.pop(record.child_route, None)
            return

        parent_callback_link: str | None = None
        child_terminal_link: str | None = None
        child_state = self._get_state(record.child_route, create=False)
        if child_state is not None and child_state.last_bot is not None:
            terminal_text = self._build_completion_summary(
                child_state,
                subtask_status=f"{'fork' if record.is_fork else 'agent task'} {self._record_status_label(record)}",
            )
            terminal_message = await self._send_system_message(
                route=record.child_route,
                bot=child_state.last_bot,
                text=terminal_text,
                disable_notification=True,
            )
            record.child_completion_message_id = (
                self._sent_message_id(terminal_message) if terminal_message is not None else None
            )
            if record.child_completion_message_id is not None:
                child_terminal_link = self._build_message_link(
                    record.child_route,
                    record.child_completion_message_id,
                )
            latest_uuid = self._session_heads.get(record.child_session_id) or record.parent_source_uuid
            if record.child_completion_message_id and latest_uuid:
                self._record_message_binding(
                    route=record.child_route,
                    message_id=record.child_completion_message_id,
                    jsonl_uuid=latest_uuid,
                    session_id=record.child_session_id,
                    role="assistant",
                )

        if (
            record.emit_parent_callback
            and parent_state is not None
            and parent_state.last_bot is not None
        ):
            child_anchor_id = record.child_completion_message_id or record.launch_child_message_id
            child_anchor_link = (
                self._build_message_link(record.child_route, child_anchor_id)
                if child_anchor_id is not None
                else None
            )
            task_prefix = "fork task" if record.is_fork else "agent task"
            parent_marker = f"{task_prefix} {html.escape(self._record_status_label(record))}"
            if child_anchor_link:
                parent_marker = (
                    f'{task_prefix} {html.escape(self._record_status_label(record))}: '
                    f'<a href="{html.escape(child_anchor_link)}">open child completion</a>'
                )
            try:
                marker_messages = await self._send_system_html_message(
                    route=record.parent_route,
                    bot=parent_state.last_bot,
                    html_text=parent_marker,
                    disable_notification=True,
                    reply_to_message_id=record.launch_parent_message_id,
                )
                record.parent_callback_message_id = (
                    self._sent_message_id(marker_messages[0]) if marker_messages else None
                )
                if record.parent_callback_message_id is not None:
                    parent_callback_link = self._build_message_link(
                        record.parent_route,
                        record.parent_callback_message_id,
                    )
                    latest_parent_uuid = self._session_heads.get(parent_state.session_id or "")
                    self._record_message_binding(
                        route=record.parent_route,
                        message_id=record.parent_callback_message_id,
                        jsonl_uuid=latest_parent_uuid or record.parent_source_uuid,
                        session_id=parent_state.session_id or record.parent_session_id_at_launch,
                        role="assistant",
                    )
            except Exception:
                logger.exception("Failed to send parent completion marker for ForkTask task_id=%s", task_id)
            finally:
                parent_state.hook_state.message_queue.put_nowait(
                    self._build_fork_task_callback_payload(record)
                )

        if (
            child_state is not None
            and child_state.last_bot is not None
            and parent_callback_link
            and child_terminal_link is not None
        ):
            backlink_text = self._build_completion_summary(
                child_state,
                subtask_status=f"{'fork' if record.is_fork else 'agent task'} {self._record_status_label(record)}",
                return_to_parent_link=parent_callback_link,
            )
            try:
                await child_state.last_bot.edit_message_text(
                    chat_id=record.child_route.chat_id,
                    message_id=record.child_completion_message_id,
                    text=self._format_system_html(backlink_text),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.debug("Failed to add parent backlink to child terminal message", exc_info=True)

        if (
            record.idle_ready
            and record.terminal_request is None
            and record.status == "completed"
        ):
            self._fork_task_by_child_route[record.child_route] = record.task_id
            self._register_team_worker_record(record)
            if record.wake_requested:
                await self._start_idle_team_worker_wake(
                    record=record,
                    sender=record.wake_source_sender,
                    summary=record.wake_source_summary,
                    content=record.wake_source_content,
                )
            return

        self._fork_task_by_child_route.pop(record.child_route, None)
        self._remove_team_worker_mappings_for_task(task_id)

    async def _fork_task_output(
        self,
        *,
        route: TelegramRoute,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        _ = route
        task_id = str(args["task_id"])
        block = bool(args["block"])
        timeout = max(int(args["timeout"]), 0)
        record = self._fork_tasks_by_id.get(task_id)
        if record is None:
            return self._task_not_found_result(task_id)
        if record.terminal_request in {"stopped", "killed"}:
            return self._task_not_found_result(task_id)

        task = self._fork_task_tasks.get(task_id)
        if task is not None and not task.done():
            if block:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout / 1000.0)
                except asyncio.TimeoutError:
                    return {
                        "content": [{"type": "text", "text": "<retrieval_status>timeout</retrieval_status>"}],
                        "tool_use_result": {"retrieval_status": "timeout", "task": None},
                    }
            else:
                running_output = (record.result_text or "").strip() or None
                return self._build_fork_task_output_result(
                    record=record,
                    retrieval_status="not_ready",
                    status="running",
                    output=running_output,
                )

        if record.status in {"completed", "stopped", "failed"}:
            return self._task_not_found_result(task_id)
        return self._task_not_found_result(task_id)

    async def _fork_task_stop(
        self,
        *,
        route: TelegramRoute,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        _ = route
        task_id = str(args["task_id"])
        record = self._fork_tasks_by_id.get(task_id)
        task = self._fork_task_tasks.get(task_id)
        if record is not None and record.idle_ready and (task is None or task.done()):
            record.idle_ready = False
            record.wake_requested = False
            record.wake_source_sender = None
            record.wake_source_summary = None
            record.wake_source_content = None
            record.terminal_request = "stopped"
            record.status = "stopped"
            record.completed_at = time.time()
            self._fork_task_by_child_route.pop(record.child_route, None)
            self._remove_team_worker_mappings_for_task(task_id)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "message": f"Successfully stopped task: {task_id} ({record.description or 'fork task'})",
                                "task_id": task_id,
                                "task_type": "local_agent",
                                "command": record.description or "fork task",
                            },
                            ensure_ascii=True,
                        ),
                    }
                ],
                "tool_use_result": {
                    "message": f"Successfully stopped task: {task_id} ({record.description or 'fork task'})",
                    "task_id": task_id,
                    "task_type": "local_agent",
                    "command": record.description or "fork task",
                },
            }
        if record is None or task is None or task.done():
            return self._task_not_found_result(task_id)
        if record.terminal_request == "stopped":
            return self._task_not_running_result(task_id, status="killed")

        record.terminal_request = "stopped"
        child_state = self._get_state(record.child_route, create=False)
        if child_state is not None:
            child_state.hook_state.interrupt_flag = True
            try:
                client = await child_state.session_manager.get_client()
                await client.interrupt()
            except Exception:
                logger.debug("ForkTaskStop interrupt failed task_id=%s", task_id, exc_info=True)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "message": f"Successfully stopped task: {task_id} ({record.description or 'fork task'})",
                            "task_id": task_id,
                            "task_type": "local_agent",
                            "command": record.description or "fork task",
                        },
                        ensure_ascii=True,
                    ),
                }
            ],
            "tool_use_result": {
                "message": f"Successfully stopped task: {task_id} ({record.description or 'fork task'})",
                "task_id": task_id,
                "task_type": "local_agent",
                "command": record.description or "fork task",
            },
        }

    async def handle_fork(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /fork - create a new topic from current head or replied message."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        await self._ensure_background_poller(context.bot)
        message = update.effective_message
        route = self._route_for_message(message)
        state = self._get_state(route)
        if state is None:
            return
        state.last_bot = context.bot

        reply_message = message.reply_to_message
        reply_message_id = getattr(reply_message, "message_id", None)
        if (
            reply_message_id is not None
            and self._message_map.get((route.chat_id, reply_message_id)) is None
        ):
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text="can't fork from this message",
                disable_notification=True,
                reply_to_message_id=message.message_id,
            )
            return

        source_session_id, source_uuid, source_route, source_message_id = self._resolve_fork_source(
            state=state,
            reply_message_id=reply_message_id,
        )

        logger.info(
            "[fork_command] route=%s state_session_id=%s source_session_id=%s source_uuid=%s "
            "reply_message_id=%s source_head=%s",
            route,
            state.session_id,
            source_session_id,
            source_uuid,
            reply_message_id,
            self._session_heads.get(source_session_id or ""),
        )

        if not source_session_id or not source_uuid:
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text="can't fork yet: no mapped head in this topic",
                disable_notification=True,
                reply_to_message_id=message.message_id,
            )
            return

        topic_name = self._next_topic_title(state, " ".join(context.args).strip() or None)
        source_link = (
            self._build_message_link(source_route, source_message_id)
            if source_message_id is not None
            else None
        )
        service_lines = ["fork created"]
        if source_link:
            service_lines.append(f'forked from <a href="{html.escape(source_link)}">source message</a>')
        service_html = "\n".join(service_lines)
        created = await self._create_child_fork_topic(
            parent_state=state,
            bot=context.bot,
            source_session_id=source_session_id,
            source_uuid=source_uuid,
            source_route=source_route,
            source_message_id=source_message_id,
            topic_name=topic_name,
            child_service_html=service_html,
            notify_on_completion=True,
        )
        child_link = created["child_link"]
        confirmation = "fork topic created"
        if child_link:
            confirmation = f'fork topic created: <a href="{html.escape(child_link)}">{html.escape(topic_name)}</a>'
        confirmation_message = await self._send_system_html_message(
            route=route,
            bot=context.bot,
            html_text=confirmation,
            disable_notification=True,
            reply_to_message_id=message.message_id,
        )
        confirmation_id = self._sent_message_id(confirmation_message[0]) if confirmation_message else None
        if confirmation_id is not None:
            self._record_message_binding(
                route=route,
                message_id=confirmation_id,
                jsonl_uuid=source_uuid,
                session_id=source_session_id,
                role="assistant",
            )

    async def handle_delete(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /delete - delete current topic or all non-General topics."""
        if update.effective_user is None or update.effective_message is None:
            return
        if not self._is_authorized(update.effective_user.id):
            return

        route = self._route_for_message(update.effective_message)
        if len(context.args) == 1 and context.args[0].strip().lower() == "all":
            targets = [candidate for candidate in self._routes_in_chat(route.chat_id) if candidate.thread_id is not None]
            for target in targets:
                try:
                    await self._enqueue_delete_topic(
                        route=target,
                        fallback_bot=context.bot,
                        priority=_PRIORITY_SYSTEM,
                    )
                except Exception:
                    logger.debug("Failed deleting topic route=%s", target, exc_info=True)
                await self._drop_route_state(target, terminal_status="failed")
            reply_route = route if route.thread_id is None else TelegramRoute(chat_id=route.chat_id, thread_id=None)
            await self._send_system_message(
                route=reply_route,
                bot=context.bot,
                text="all non-General topics deleted",
                disable_notification=False,
            )
            return

        if route.thread_id is None:
            await self._send_system_message(
                route=route,
                bot=context.bot,
                text="can't delete General",
                disable_notification=True,
            )
            return
        try:
            await self._enqueue_delete_topic(
                route=route,
                fallback_bot=context.bot,
                priority=_PRIORITY_SYSTEM,
            )
        finally:
            await self._drop_route_state(route, terminal_status="failed")


def create_telegram_app(config: OBSConfig) -> Application:
    """Create and configure a python-telegram-bot Application."""
    primary_token = config.telegram_primary_bot_token
    if not primary_token:
        raise ValueError("OBS_TELEGRAM_BOT_TOKEN or OBS_TELEGRAM_BOT_TOKENS is required")
    if not config.telegram_allowed_user_ids:
        raise ValueError(
            "OBS_TELEGRAM_ALLOWED_USERS is required (comma-separated Telegram user IDs). "
            "The bot will not start without an explicit allowlist."
        )

    bot = TelegramBot(config)
    app = (
        Application.builder()
        .token(primary_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["obs_telegram_bot"] = bot
    app.add_handler(CommandHandler("clear", bot.handle_clear))
    app.add_handler(CommandHandler("new", bot.handle_new))
    app.add_handler(CommandHandler("unschedule", bot.handle_unschedule))
    app.add_handler(CommandHandler("stop", bot.handle_stop))
    app.add_handler(CommandHandler("context", bot.handle_context))
    app.add_handler(CommandHandler("report", bot.handle_report))
    app.add_handler(CommandHandler("fork", bot.handle_fork))
    app.add_handler(CommandHandler("delete", bot.handle_delete))
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, bot.handle_forum_topic_created))
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_EDITED, bot.handle_forum_topic_edited))
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
        BotCommand("clear", "Clear this topic; use '/clear all' for the whole group"),
        BotCommand("unschedule", "Remove schedule(s) from this topic; use /unschedule all"),
        BotCommand("stop", "Interrupt this topic; use '/stop all' for the whole group"),
        BotCommand("context", "Show session and context window info"),
        BotCommand("report", "Save a debug case file for this message/topic"),
        BotCommand("fork", "Create a new topic from this head or replied message"),
        BotCommand("delete", "Delete this topic; use '/delete all' to remove all non-General topics"),
    ])


async def run_telegram_bot(config: OBSConfig) -> None:
    """Start the Telegram bot (blocking)."""
    app = create_telegram_app(config)
    tg_bot: TelegramBot = app.bot_data["obs_telegram_bot"]

    logger.info("Starting Telegram bot...")
    await tg_bot.initialize_runtime()
    await app.initialize()
    await tg_bot._ensure_background_poller(app.bot)
    await _set_bot_commands(app)
    await app.start()
    drop_pending_updates = (
        (os.environ.get("OBS_TELEGRAM_DROP_PENDING_UPDATES") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    await app.updater.start_polling(drop_pending_updates=drop_pending_updates)

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
