"""ConversationRunner: core orchestration loop extracted from daemon.py.

Manages the query → receive → continuation → background-fork-wait cycle.
Yields RunnerEvent objects that platform adapters (HTTP/SSE, Telegram, etc.)
can consume and render in their own format.

See daemon.py for the original inline implementation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

from claude_agent_sdk import CLIConnectionError, TextBlock, ThinkingBlock, ToolUseBlock

from obs_agent.events import StatusEvent, summarize_tool_use
from obs_agent.hooks import HookState
from obs_agent.metrics import log_result
from obs_agent.session import SessionManager

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.runner")


# ---------------------------------------------------------------------------
# Runner event types
# ---------------------------------------------------------------------------

@dataclass
class TextEvent:
    """A chunk of assistant text output."""
    text: str


@dataclass
class DoneEvent:
    """Signals the end of the response stream."""
    pass


RunnerEvent = TextEvent | StatusEvent | DoneEvent


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
        pending_messages: list[str] | None = None,
    ) -> None:
        self._session_mgr = session_manager
        self._hook_state = hook_state
        self._config = config
        self._pending_messages = list(pending_messages) if pending_messages else []

    @property
    def remaining_pending(self) -> list[str]:
        """Messages left in the queue after run() completes (for next turn)."""
        return self._pending_messages

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
                f"[Queued message from user]: {m}" for m in pending
            )
            actual_message = f"{prefix}\n\n{user_message}"
            self._pending_messages = []

        if had_pending:
            yield StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=pending_count,
                messages=pending,
            )

        # 2. Get client and send query (with reconnect on connection loss)
        client = await self._session_mgr.get_client()
        try:
            await client.query(actual_message)
        except CLIConnectionError:
            logger.warning("Client connection lost, reconnecting")
            await self._session_mgr.disconnect()
            client = await self._session_mgr.get_client()
            await client.query(actual_message)

        # 3. Stream response
        last_message = None
        async for message in client.receive_response():
            last_message = message
            if hasattr(message, "session_id") and message.session_id:
                self._session_mgr.set_session_id(message.session_id)
            if hasattr(message, "content") and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_name = getattr(block, "name", "")
                        tool_input = getattr(block, "input", {}) or {}
                        yield StatusEvent(
                            type="tool_use",
                            summary=summarize_tool_use(tool_name, tool_input),
                        )
                    elif isinstance(block, ThinkingBlock):
                        yield StatusEvent(
                            type="thinking",
                            summary="thinking...",
                        )
                    elif isinstance(block, TextBlock):
                        yield TextEvent(text=block.text)

            # Drain status_queue after each message
            while not self._hook_state.status_queue.empty():
                try:
                    status_event = self._hook_state.status_queue.get_nowait()
                    yield status_event
                except asyncio.QueueEmpty:
                    break

        if last_message is not None:
            log_result(last_message, label="conversation")

        # 4. Continuation loop: process queued messages inline
        continuation_count = 0
        while continuation_count < self._config.max_queue_continuations:
            remaining = _drain_queue(self._hook_state.message_queue)
            if not remaining:
                break
            continuation_count += 1

            prefix = "\n".join(
                f"[Queued message from user]: {m}" for m in remaining
            )
            continuation_prompt = (
                f"{prefix}\n\n"
                "(User sent these while you were responding. Address them briefly.)"
            )

            yield StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=len(remaining),
                messages=remaining,
            )

            await client.query(continuation_prompt)
            async for cont_message in client.receive_response():
                if hasattr(cont_message, "session_id") and cont_message.session_id:
                    self._session_mgr.set_session_id(cont_message.session_id)
                if hasattr(cont_message, "content") and isinstance(
                    cont_message.content, list
                ):
                    for block in cont_message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_name = getattr(block, "name", "")
                            tool_input = getattr(block, "input", {}) or {}
                            yield StatusEvent(
                                type="tool_use",
                                summary=summarize_tool_use(tool_name, tool_input),
                            )
                        elif isinstance(block, ThinkingBlock):
                            yield StatusEvent(
                                type="thinking",
                                summary="thinking...",
                            )
                        elif isinstance(block, TextBlock):
                            yield TextEvent(text=block.text)

                while not self._hook_state.status_queue.empty():
                    try:
                        status_event = self._hook_state.status_queue.get_nowait()
                        yield status_event
                    except asyncio.QueueEmpty:
                        break

        # 5. Background fork wait loop
        while self._hook_state.background_tasks:
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

            bg_prefix = "\n".join(
                f"[Queued message from user]: {m}" for m in bg_remaining
            )
            bg_prompt = (
                f"{bg_prefix}\n\n"
                "(Background fork results arrived. Process and summarize them.)"
            )

            yield StatusEvent(
                type="queue_delivered",
                summary="background fork result delivered",
                count=len(bg_remaining),
                messages=bg_remaining,
            )

            await client.query(bg_prompt)
            async for bg_msg in client.receive_response():
                if hasattr(bg_msg, "session_id") and bg_msg.session_id:
                    self._session_mgr.set_session_id(bg_msg.session_id)
                if hasattr(bg_msg, "content") and isinstance(
                    bg_msg.content, list
                ):
                    for block in bg_msg.content:
                        if isinstance(block, ToolUseBlock):
                            tool_name = getattr(block, "name", "")
                            tool_input = getattr(block, "input", {}) or {}
                            yield StatusEvent(
                                type="tool_use",
                                summary=summarize_tool_use(tool_name, tool_input),
                            )
                        elif isinstance(block, ThinkingBlock):
                            yield StatusEvent(
                                type="thinking",
                                summary="thinking...",
                            )
                        elif isinstance(block, TextBlock):
                            yield TextEvent(text=block.text)

                while not self._hook_state.status_queue.empty():
                    try:
                        status_event = self._hook_state.status_queue.get_nowait()
                        yield status_event
                    except asyncio.QueueEmpty:
                        break

        # 6. Drain remaining queue for next turn
        self._pending_messages = _drain_queue(self._hook_state.message_queue)

        self._session_mgr.touch()
        yield DoneEvent()
