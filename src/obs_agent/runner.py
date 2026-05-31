"""ConversationRunner: core orchestration loop extracted from daemon.py.

Manages the query -> receive -> continuation -> background-fork-wait cycle.
Yields RunnerEvent objects that platform adapters (HTTP/SSE, Telegram, etc.)
can consume and render in their own format.

See daemon.py for the original inline implementation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

from claude_agent_sdk import (
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from obs_agent.events import StatusEvent, summarize_tool_use
from obs_agent.hooks import HookState
from obs_agent.metrics import log_result
from obs_agent.queueing import QueuedMessage, coerce_queued_message, queued_texts
from obs_agent.session import SessionManager

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.runner")

_RECOVERY_PROMPT = (
    "Resume the interrupted response only. "
    "Do not treat this as a new user message or as part of the conversation history."
)


# ---------------------------------------------------------------------------
# Runner event types
# ---------------------------------------------------------------------------

@dataclass
class TextEvent:
    """A chunk of assistant text output."""
    text: str


@dataclass
class TurnEndEvent:
    """Signals that one SDK assistant message has fully streamed."""

    jsonl_uuid: str | None = None
    message_role: str | None = None
    has_text: bool = False


@dataclass
class DoneEvent:
    """Signals the end of the response stream."""
    pass


RunnerEvent = TextEvent | StatusEvent | TurnEndEvent | DoneEvent


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _is_recoverable(exc: BaseException) -> bool:
    """Classify whether an error is recoverable via session reconnect.

    Almost everything is recoverable: even when the CLI process dies
    (ProcessError), a new client with ``resume=session_id`` starts a fresh
    process that loads conversation history from disk.

    The only truly unrecoverable case is ``CLINotFoundError`` — the ``claude``
    binary isn't installed, so no reconnect can succeed.
    """
    if isinstance(exc, CLINotFoundError):
        return False
    return True


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


def _message_role(message) -> str | None:
    role = getattr(message, "role", None)
    if isinstance(role, str) and role:
        return role

    type_name = type(message).__name__
    if type_name == "AssistantMessage":
        return "assistant"
    if type_name == "UserMessage":
        return "user"
    if type_name == "SystemMessage":
        return "system"
    return None


def _system_message_to_status_event(message) -> StatusEvent | None:
    """Convert selected SystemMessage subtypes into visible status events."""
    subtype = getattr(message, "subtype", None)
    if not isinstance(subtype, str) or not subtype:
        return None
    recognized_exact = {"task_started", "task_progress", "task_notification"}
    if subtype not in recognized_exact and not subtype.startswith("task_"):
        return None
    data = getattr(message, "data", None)
    payload = data if isinstance(data, dict) else {}
    lines: list[str] = []
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        lines.append(f"task_id: {task_id}")
    description = str(payload.get("description") or "").strip()
    if description:
        lines.append(f"description: {description}")

    if subtype == "task_progress":
        last_tool_name = str(payload.get("last_tool_name") or "").strip()
        usage = payload.get("usage")
        if last_tool_name:
            lines.append(f"last_tool: {last_tool_name}")
        if isinstance(usage, dict):
            tool_uses = usage.get("tool_uses")
            total_tokens = usage.get("total_tokens")
            duration_ms = usage.get("duration_ms")
            usage_bits: list[str] = []
            if tool_uses is not None:
                usage_bits.append(f"tool_uses={tool_uses}")
            if total_tokens is not None:
                usage_bits.append(f"total_tokens={total_tokens}")
            if duration_ms is not None:
                usage_bits.append(f"duration_ms={duration_ms}")
            if usage_bits:
                lines.append("usage: " + " ".join(usage_bits))

    if subtype == "task_notification":
        status = str(payload.get("status") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if status:
            lines.append(f"status: {status}")
        if summary:
            lines.append(summary)
    elif subtype not in {"task_started", "task_progress"}:
        status = str(payload.get("status") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if status:
            lines.append(f"status: {status}")
        if summary:
            lines.append(summary)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            usage_bits: list[str] = []
            tool_uses = usage.get("tool_uses")
            total_tokens = usage.get("total_tokens")
            duration_ms = usage.get("duration_ms")
            if tool_uses is not None:
                usage_bits.append(f"tool_uses={tool_uses}")
            if total_tokens is not None:
                usage_bits.append(f"total_tokens={total_tokens}")
            if duration_ms is not None:
                usage_bits.append(f"duration_ms={duration_ms}")
            if usage_bits:
                lines.append("usage: " + " ".join(usage_bits))

    return StatusEvent(
        type="notification",
        summary=f"notification: {subtype}",
        messages=lines or None,
    )


# ---------------------------------------------------------------------------
# ConversationRunner
# ---------------------------------------------------------------------------

class ConversationRunner:
    """Drives a single conversation turn through the SDK.

    Usage::

        runner = ConversationRunner(session_mgr, hook_state, config)
        async for event in runner.run("Hello"):
            if isinstance(event, TextEvent):
                print(event.text, end="")
            elif isinstance(event, StatusEvent):
                show_status(event)
            elif isinstance(event, DoneEvent):
                break
    """

    def __init__(
        self,
        session_manager: SessionManager,
        hook_state: HookState,
        config: OBSConfig,
        *,
        pending_messages: list[QueuedMessage | str] | None = None,
    ) -> None:
        self._session_mgr = session_manager
        self._hook_state = hook_state
        self._config = config
        self._pending_messages = [
            coerce_queued_message(message)
            for message in pending_messages or []
        ]
        self._client = None  # set during run()
        self._last_message = None  # tracks last SDK message for metrics
        self._last_result_message = None  # latest ResultMessage-like payload
        self._last_assistant_usage: dict | None = None  # latest assistant step usage
        self._last_context_usage: dict | None = None  # latest SDK context usage payload

    def _refresh_last_result_data(self) -> None:
        """Refresh hook_state.last_result_data from the latest SDK result-like message."""
        result_msg = self._last_result_message or self._last_message
        if result_msg is None:
            return
        self._hook_state.last_result_data = {
            "session_id": getattr(result_msg, "session_id", None),
            "num_turns": getattr(result_msg, "num_turns", None),
            "total_cost_usd": getattr(result_msg, "total_cost_usd", None),
            "duration_ms": getattr(result_msg, "duration_ms", None),
            "usage": None,
        }
        usage = self._last_assistant_usage
        if not isinstance(usage, dict):
            usage = getattr(result_msg, "usage", None)
        if isinstance(usage, dict):
            self._hook_state.last_result_data["usage"] = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            }
        if isinstance(self._last_context_usage, dict):
            self._hook_state.last_result_data["context_usage"] = dict(self._last_context_usage)

    async def _refresh_context_usage(self) -> None:
        if self._client is None or not hasattr(self._client, "get_context_usage"):
            return
        try:
            context_usage = await self._client.get_context_usage()
        except Exception:
            logger.debug("Failed to refresh SDK context usage", exc_info=True)
            return
        if isinstance(context_usage, dict):
            self._last_context_usage = dict(context_usage)

    def _sync_session_id_from_client(self) -> None:
        if self._client is None:
            return
        session_id = getattr(self._client, "session_id", None)
        if isinstance(session_id, str) and session_id:
            self._session_mgr.set_session_id(session_id)

    @property
    def remaining_pending(self) -> list[QueuedMessage]:
        """Messages left in the queue after run() completes (for next turn)."""
        return self._pending_messages

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    async def _stream_response(self) -> AsyncIterator[RunnerEvent]:
        """Stream one SDK response from ``self._client``, yielding events.

        Updates ``self._last_message`` for each message received.
        """
        response_stream = self._client.receive_response()
        if inspect.isawaitable(response_stream):
            response_stream = await response_stream
        async for message in response_stream:
            self._last_message = message
            raw_uuid = getattr(message, "_raw_uuid", None)
            message_role = _message_role(message)
            has_text = False
            if (
                getattr(message, "num_turns", None) is not None
                and getattr(message, "total_cost_usd", None) is not None
            ):
                self._last_result_message = message
            if hasattr(message, "session_id") and message.session_id:
                self._session_mgr.set_session_id(message.session_id)
            if hasattr(message, "content") and isinstance(message.content, list):
                usage = getattr(message, "usage", None)
                if message.content and isinstance(usage, dict):
                    self._last_assistant_usage = usage
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_name = getattr(block, "name", "")
                        tool_input = getattr(block, "input", {}) or {}
                        yield StatusEvent(
                            type="tool_use",
                            summary=summarize_tool_use(tool_name, tool_input),
                        )
                    elif isinstance(block, ThinkingBlock):
                        thinking_text = getattr(block, "thinking", "") or ""
                        thinking_text = thinking_text.strip()
                        yield StatusEvent(
                            type="thinking",
                            summary=thinking_text or "thinking...",
                        )
                    elif isinstance(block, TextBlock):
                        has_text = True
                        yield TextEvent(text=block.text)

            system_status = _system_message_to_status_event(message)
            if system_status is not None:
                yield system_status

            # Drain status_queue after each message
            while not self._hook_state.status_queue.empty():
                try:
                    yield self._hook_state.status_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            yield TurnEndEvent(
                jsonl_uuid=raw_uuid if isinstance(raw_uuid, str) and raw_uuid else None,
                message_role=message_role,
                has_text=has_text,
            )

    async def _stream_or_reconnect(
        self, retry_prompt: str,
    ) -> AsyncIterator[RunnerEvent]:
        """Stream response; on recoverable error reconnect and re-stream.

        On unrecoverable errors (e.g. ``CLINotFoundError``) the exception
        propagates immediately.  On recoverable errors we reconnect via
        ``SessionManager.reconnect()`` (preserves session_id), send
        *retry_prompt* and stream again.  If reconnect fails (e.g. no
        session_id yet), falls back to a fresh session via disconnect +
        get_client.  If the retry also fails the exception propagates.
        """
        try:
            async for event in self._stream_response():
                yield event
        except Exception as exc:
            if not _is_recoverable(exc):
                raise
            logger.warning("Stream error, attempting reconnect: %s", exc)
            try:
                self._client = await self._session_mgr.reconnect()
            except Exception:
                logger.warning(
                    "Reconnect failed (no session_id?), falling back to fresh session",
                    exc_info=True,
                )
                await self._session_mgr.disconnect()
                try:
                    self._client = await self._session_mgr.get_client()
                except Exception:
                    logger.error("Fresh session also failed", exc_info=True)
                    raise exc from None
            await self._client.query(f"(System: {retry_prompt})")
            self._sync_session_id_from_client()
            async for event in self._stream_response():
                yield event

    async def _query_or_reconnect(self, prompt: str) -> None:
        """Send query; on recoverable error reconnect and retry."""
        try:
            await self._client.query(prompt)
            self._sync_session_id_from_client()
        except Exception as exc:
            if not _is_recoverable(exc):
                raise
            logger.warning("Error sending query, attempting reconnect: %s", exc)
            try:
                self._client = await self._session_mgr.reconnect()
            except Exception:
                logger.warning(
                    "Reconnect failed (no session_id?), falling back to fresh session",
                    exc_info=True,
                )
                await self._session_mgr.disconnect()
                try:
                    self._client = await self._session_mgr.get_client()
                except Exception:
                    logger.error("Fresh session also failed", exc_info=True)
                    raise exc from None
            await self._client.query(prompt)
            self._sync_session_id_from_client()

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> AsyncIterator[RunnerEvent]:
        """Execute one conversation turn, yielding events as they occur.

        Handles:
        - Pending message injection from previous turn
        - Initial query + response streaming
        - Continuation loop for queued messages
        - Background fork wait + wake-up
        - Final queue drain for next turn
        """
        # 1. Inject pending messages from previous turn
        pending = self._pending_messages
        had_pending = bool(pending)
        pending_count = len(pending)
        actual_message = user_message

        if pending:
            prefix = "\n".join(
                f"[Queued message from user]: {m.text}" for m in pending
            )
            actual_message = f"{prefix}\n\n{user_message}"
            self._pending_messages = []

        if had_pending:
            yield StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=pending_count,
                messages=queued_texts(pending),
            )

        # 2. Get client and send query (with reconnect on connection loss)
        try:
            self._client = await self._session_mgr.get_client()
            self._sync_session_id_from_client()
        except Exception as exc:
            if not _is_recoverable(exc):
                raise
            logger.warning("Connection failed on get_client, reconnecting: %s", exc)
            await self._session_mgr.disconnect()
            try:
                self._client = await self._session_mgr.get_client()
                self._sync_session_id_from_client()
            except Exception as retry_exc:
                if not _is_recoverable(retry_exc):
                    raise
                if self._session_mgr.session_id is not None:
                    logger.error(
                        "Reconnect on get_client also failed; preserving session instead of starting fresh: %s",
                        retry_exc,
                    )
                    await self._session_mgr.soft_reset()
                    raise retry_exc from None
                logger.warning(
                    "Reconnect on get_client also failed before session creation; starting fresh: %s",
                    retry_exc,
                )
                await self._session_mgr.async_reset()
                self._client = await self._session_mgr.get_client()
                self._sync_session_id_from_client()

        try:
            await self._client.query(actual_message)
            self._sync_session_id_from_client()
        except Exception as exc:
            if not _is_recoverable(exc):
                raise
            logger.warning("Connection lost on initial query, reconnecting: %s", exc)
            await self._session_mgr.disconnect()
            self._client = await self._session_mgr.get_client()
            self._sync_session_id_from_client()
            await self._client.query(actual_message)
            self._sync_session_id_from_client()

        # 3. Stream response (with reconnect on recoverable errors)
        self._last_message = None
        self._last_result_message = None
        self._last_assistant_usage = None
        self._last_context_usage = None
        async for event in self._stream_or_reconnect(
            _RECOVERY_PROMPT
        ):
            yield event

        if self._last_message is not None:
            log_result(self._last_message, label="conversation")
            await self._refresh_context_usage()
            self._refresh_last_result_data()

        # 4. Continuation loop: process queued messages inline
        continuation_count = 0
        deferred_pending: list[QueuedMessage] = []
        while (
            continuation_count < self._config.max_queue_continuations
            and not self._hook_state.pause_queue_delivery
            and not self._hook_state.interrupt_requested
        ):
            remaining = _drain_queue(self._hook_state.message_queue)
            if not remaining:
                break
            latest = remaining[-1]
            if latest.reply_to_message_id is not None:
                deferred_pending.extend(remaining)
                break
            continuation_count += 1

            continuation_prompt = (
                "\n".join(f"[Queued message from user]: {m.text}" for m in remaining)
                + "\n\n(User sent these while you were responding. Address them briefly.)"
            )

            yield StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=len(remaining),
                messages=queued_texts(remaining),
            )

            await self._query_or_reconnect(continuation_prompt)
            async for event in self._stream_or_reconnect(
                _RECOVERY_PROMPT
            ):
                yield event
            await self._refresh_context_usage()
            self._refresh_last_result_data()

        # 5. Background fork wait loop
        while (
            self._hook_state.background_tasks
            and not self._hook_state.pause_queue_delivery
            and not self._hook_state.interrupt_requested
        ):
            tasks = set(self._hook_state.background_tasks)
            if not tasks:
                break
            try:
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=self._config.bg_fork_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except ValueError:
                break
            if not done:
                break

            await asyncio.sleep(0.1)

            bg_remaining = _drain_queue(self._hook_state.message_queue)
            if not bg_remaining:
                continue

            bg_prompt = (
                "\n".join(f"[Queued message from user]: {m.text}" for m in bg_remaining)
                + "\n\n(Background fork results arrived. Process and summarize them.)"
            )

            yield StatusEvent(
                type="queue_delivered",
                summary="background fork result delivered",
                count=len(bg_remaining),
                messages=queued_texts(bg_remaining),
            )

            await self._query_or_reconnect(bg_prompt)
            async for event in self._stream_or_reconnect(
                _RECOVERY_PROMPT
            ):
                yield event
            await self._refresh_context_usage()
            self._refresh_last_result_data()

        # 6. Drain remaining queue for next turn
        self._pending_messages = deferred_pending + _drain_queue(self._hook_state.message_queue)

        self._session_mgr.touch()
        yield DoneEvent()
