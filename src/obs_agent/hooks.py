"""SDK hooks for OBS Agent.

- PreToolUse: guards immutable files/.env writes and blocks native tools
- Stop: triggers memory extraction via fork
- PreCompact: triggers extraction then denies compaction (D022)
- HookPipeline: extensible middleware that chains check functions
- HookState: shared state for message queuing and interrupt

See decisions D018, D022.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from claude_agent_sdk.types import (
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    SyncHookJSONOutput,
)

from obs_agent.queueing import coerce_queued_message

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


# Write-mutating tool names that the guard should check
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Read-only tools that are always allowed
_READ_TOOLS = {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"}

# File patterns that are always blocked from writes (beyond config immutable_patterns)
_BLOCKED_FILE_PATTERNS = [".env"]

# Native delegation tools are blocked so orchestration is forced through
# OBS-managed AgentTask/ForkTask tooling.
_BLOCKED_NATIVE_TASK_TOOLS = {"Task", "TaskStop"}  # TaskOutput allowed (read-only, useful for background bash)

# Native team inbox tools are blocked so worker messaging is forced through
# OBS-managed SendInboxMessage/ReadInbox implementations.
_BLOCKED_NATIVE_INBOX_TOOLS = {
    "SendMessage",
    "ReadMessage",
    "ReadMessages",
    "ListMessages",
    "GetMessages",
    "ReceiveMessages",
}

# Native runtime mode toggles are blocked in Telegram runtime.
_BLOCKED_NATIVE_MODE_TOOLS = {
    "EnterPlanMode",
}


def _normalize_tool_name(tool_name: str) -> str:
    """Normalize tool names from hook payloads.

    SDK hook payloads may report MCP tools as ``mcp__server__ToolName``.
    We normalize those to ``ToolName`` so deny/allow checks remain stable.
    """
    normalized = tool_name.strip()
    if normalized.startswith("mcp__"):
        parts = normalized.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return normalized


def _deny(
    reason: str,
    *,
    additional_context: str | None = None,
    show_system_message: bool = False,
) -> dict:
    """Return a deny hook response with reason."""
    hook_output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if additional_context:
        hook_output["additionalContext"] = additional_context
    payload: dict[str, Any] = {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": hook_output,
    }
    if show_system_message:
        payload["systemMessage"] = reason
    return payload


def on_pre_tool_use(
    tool_name: str,
    tool_input: dict,
    *,
    config: OBSConfig,
) -> dict | None:
    """Guard hook: block disallowed native tools and protected writes.

    Returns None to allow, or a deny dict to block.
    """
    normalized_tool_name = _normalize_tool_name(tool_name)

    if normalized_tool_name in _BLOCKED_NATIVE_TASK_TOOLS:
        return _deny(
            "Blocked by platform policy: native Task tools are disabled in Telegram runtime. "
            "Use AgentTask, AgentTaskOutput, and AgentTaskStop instead. "
            "TaskOutput is allowed for reading background task output.",
            additional_context=(
                "System: Native Task/TaskStop tools are disabled by platform policy in Telegram runtime. "
                "Do not retry Task/TaskStop; switch to AgentTask tools. TaskOutput is allowed."
            ),
            show_system_message=True,
        )

    if normalized_tool_name in _BLOCKED_NATIVE_INBOX_TOOLS:
        return _deny(
            "Blocked by platform policy: flat team messaging tools are disabled in Telegram runtime. "
            "Use SendInboxMessage and ReadInbox instead.",
            additional_context=(
                "System: Flat team messaging tools are disabled by platform policy in Telegram runtime. "
                "Use SendInboxMessage and ReadInbox."
            ),
            show_system_message=True,
        )

    if normalized_tool_name in _BLOCKED_NATIVE_MODE_TOOLS:
        return _deny(
            "Blocked by platform policy: EnterPlanMode is disabled in Telegram runtime.",
            additional_context=(
                "System: EnterPlanMode is not available in Telegram runtime. "
                "Continue in normal execution mode."
            ),
            show_system_message=True,
        )

    # Only guard write-mutating tools
    if normalized_tool_name not in _WRITE_TOOLS:
        return None

    file_path_str = tool_input.get("file_path", "")
    if not file_path_str:
        return None

    file_path = Path(file_path_str)

    # Check .env files
    for pattern in _BLOCKED_FILE_PATTERNS:
        if pattern in file_path.name:
            return _deny(f"Blocked: cannot modify {pattern} files")

    # Check immutable patterns from config
    if config.is_immutable(file_path):
        return _deny(f"Blocked: {file_path} matches an immutable pattern. These files must not be edited.")

    return None


async def on_stop(
    *,
    config: OBSConfig,
    fork_runner: ForkRunner,
) -> None:
    """Stop hook: trigger memory extraction fork."""
    await fork_runner.extract_memory()


async def on_pre_compact(
    *,
    config: OBSConfig,
    fork_runner: ForkRunner,
) -> dict:
    """PreCompact hook: extract memories then deny compaction.

    Per D022: no lossy compaction. Flush memories to vault, then deny
    so the daemon can restart with a fresh session.
    """
    await fork_runner.extract_memory()
    return _deny("Compaction denied: memories flushed, restart with fresh session")


# ---------------------------------------------------------------------------
# Hook Pipeline: extensible middleware for SDK hook callbacks
# ---------------------------------------------------------------------------

# Type alias for individual check functions within a pipeline.
# A check receives the same args as HookCallback and returns
# SyncHookJSONOutput | None (None means "no opinion, continue").
CheckFn = Callable[
    [HookInput, str | None, HookContext],
    Awaitable[SyncHookJSONOutput | None],
]


@dataclass
class HookState:
    """Shared mutable state between daemon endpoints and hook callbacks.

    Daemon endpoints write (enqueue messages, set interrupt flag).
    Hook callbacks read (drain queue, check/clear interrupt flag).
    status_queue is drained by the daemon's event_generator to yield SSE status events.
    """

    message_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    status_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    interrupt_flag: bool = False
    interrupt_requested: bool = False
    interrupt_notice_pending: bool = False
    pause_queue_delivery: bool = False
    session_id: str | None = None
    background_tasks: set[asyncio.Task] = field(default_factory=set)
    last_result_data: dict | None = None  # last ResultMessage metrics
    fork_task_launcher: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    fork_task_outputter: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    fork_task_stopper: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    cron_creator: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    cron_lister: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    cron_deleter: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    inbox_recipient_validator: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    inbox_message_notifier: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
    stop_event_notifier: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    current_tool_use_id: str | None = None
    schedule_run_active: bool = False
    execution_active: bool = False
    pending_obs_bootstrap_xml: str | None = None  # set by telegram.py when bootstrap is primed

    def reset(self) -> None:
        """Clear all queued state for a fresh session.

        Called by /new to prevent cross-scenario contamination from
        stale background fork results or queued messages.
        """
        # Drain message queue
        while not self.message_queue.empty():
            try:
                self.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Drain status queue
        while not self.status_queue.empty():
            try:
                self.status_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Cancel running background tasks
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
        self.background_tasks.clear()
        self.interrupt_flag = False
        self.interrupt_requested = False
        self.interrupt_notice_pending = False
        self.pause_queue_delivery = False
        self.session_id = None
        self.last_result_data = None
        self.current_tool_use_id = None
        self.schedule_run_active = False
        self.execution_active = False


class HookPipeline:
    """Chains multiple check functions into a single SDK HookCallback.

    Runs checks sequentially. Short-circuits on:
    - continue_: False (interrupt)
    - permissionDecision: "deny"

    Accumulates additionalContext from all checks that run.
    """

    def __init__(self, checks: list[CheckFn]) -> None:
        self._checks = list(checks)

    async def __call__(
        self,
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput:
        """Run all checks, merging results."""
        merged: SyncHookJSONOutput = {}
        accumulated_context: list[str] = []
        event_name = hook_input.get("hook_event_name")

        for check in self._checks:
            result = await check(hook_input, tool_use_id, context)
            if result is None:
                continue

            # Extract additionalContext from hookSpecificOutput
            hso = result.get("hookSpecificOutput")
            if hso and "additionalContext" in hso:
                accumulated_context.append(hso["additionalContext"])

            # Check for short-circuit: continue_ is False
            if result.get("continue_") is False:
                # Merge accumulated context before returning
                if accumulated_context:
                    merged.setdefault("hookSpecificOutput", {})
                    merged["hookSpecificOutput"]["hookEventName"] = event_name
                    merged["hookSpecificOutput"]["additionalContext"] = "\n\n".join(accumulated_context)
                merged["continue_"] = False
                if "stopReason" in result:
                    merged["stopReason"] = result["stopReason"]
                return merged

            # Check for short-circuit: permissionDecision is "deny"
            if hso and hso.get("permissionDecision") == "deny":
                for key in (
                    "continue_",
                    "suppressOutput",
                    "stopReason",
                    "decision",
                    "systemMessage",
                    "reason",
                ):
                    if key in result:
                        merged[key] = result[key]
                merged_hso: dict[str, Any] = {
                    "hookEventName": event_name,
                    "permissionDecision": "deny",
                }
                if "permissionDecisionReason" in hso:
                    merged_hso["permissionDecisionReason"] = hso["permissionDecisionReason"]
                if "updatedInput" in hso:
                    merged_hso["updatedInput"] = hso["updatedInput"]
                if accumulated_context:
                    merged_hso["additionalContext"] = "\n\n".join(accumulated_context)
                merged["hookSpecificOutput"] = merged_hso
                return merged

        # No short-circuit — return accumulated context if any
        if accumulated_context:
            merged.setdefault("hookSpecificOutput", {})
            merged["hookSpecificOutput"]["hookEventName"] = event_name
            merged["hookSpecificOutput"]["additionalContext"] = "\n\n".join(accumulated_context)

        return merged


# ---------------------------------------------------------------------------
# Check factory functions
# ---------------------------------------------------------------------------


def _make_interrupt_check(state: HookState) -> CheckFn:
    """Create a check that stops the agent if interrupt_flag is set."""

    async def _check(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput | None:
        if state.interrupt_flag:
            state.interrupt_flag = False
            state.interrupt_notice_pending = True
            return {
                "continue_": False,
                "stopReason": "Interrupted by user",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        "System: The user interrupted your previous response via /stop. "
                        "Stop current work immediately and wait for the next user message."
                    ),
                },
            }
        return None

    return _check


def _make_immutable_check(config: OBSConfig) -> CheckFn:
    """Create a check that wraps on_pre_tool_use() into SDK callback format.

    Handles native-tool denylists and immutable-write guards.
    Only meaningful for PreToolUse events — returns None for other events.
    """

    async def _check(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput | None:
        # Only act on PreToolUse events
        if hook_input.get("hook_event_name") != "PreToolUse":
            return None

        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})

        result = on_pre_tool_use(tool_name, tool_input, config=config)
        if result is None:
            return None

        return result

    return _check


def _make_queue_check(state: HookState) -> CheckFn:
    """Create a check that drains the message queue into additionalContext.

    Also pushes a queue_delivered StatusEvent to state.status_queue so the
    daemon's event_generator can yield it to the SSE stream.
    """

    async def _check(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput | None:
        event_name = hook_input.get("hook_event_name")
        if event_name == "PreToolUse":
            state.current_tool_use_id = tool_use_id
            session_id = hook_input.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                state.session_id = session_id.strip()
        elif event_name == "PostToolUse" and state.current_tool_use_id == tool_use_id:
            state.current_tool_use_id = None

        messages: list[str] = []
        deferred_messages: list[QueuedMessage] = []
        while not state.message_queue.empty():
            try:
                msg = coerce_queued_message(state.message_queue.get_nowait())
                if msg.reply_to_message_id is not None:
                    deferred_messages.append(msg)
                else:
                    messages.append(msg.text)
            except asyncio.QueueEmpty:
                break

        for msg in deferred_messages:
            state.message_queue.put_nowait(msg)

        if not messages:
            return None

        # Notify the SSE stream that queued messages were delivered
        from obs_agent.events import StatusEvent

        state.status_queue.put_nowait(
            StatusEvent(
                type="queue_delivered",
                summary="queued message delivered",
                count=len(messages),
                messages=messages,
            )
        )

        formatted = "\n".join(
            f"[Queued message from user]: {msg}" for msg in messages
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": hook_input["hook_event_name"],
                "additionalContext": formatted,
            }
        }

    return _check


def _make_notification_check(state: HookState) -> CheckFn:
    """Create a check that surfaces notification/lifecycle hook events.

    We mirror native hook events into status_queue so transport adapters (e.g.
    Telegram) can show user-visible activity for teammate/task lifecycle.
    """

    async def _check(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput | None:
        _ = tool_use_id, context
        event_name = hook_input.get("hook_event_name")
        if event_name not in {"Notification", "SubagentStart", "SubagentStop"}:
            return None

        from obs_agent.events import StatusEvent

        if event_name == "Notification":
            notification_type = str(hook_input.get("notification_type") or "notification").strip()
            title = str(hook_input.get("title") or "").strip()
            message = str(hook_input.get("message") or "").strip()
            lines: list[str] = []
            if title:
                lines.append(f"title: {title}")
            if message:
                lines.append(message)
            state.status_queue.put_nowait(
                StatusEvent(
                    type="notification",
                    summary=f"notification: {notification_type}",
                    messages=lines or None,
                )
            )
            return None

        agent_id = str(hook_input.get("agent_id") or "").strip()
        agent_type = str(hook_input.get("agent_type") or "").strip()
        lines = []
        if agent_id:
            lines.append(f"agent_id: {agent_id}")
        if agent_type:
            lines.append(f"agent_type: {agent_type}")
        if event_name == "SubagentStop":
            transcript = str(hook_input.get("agent_transcript_path") or "").strip()
            if transcript:
                lines.append(f"transcript: {transcript}")
        state.status_queue.put_nowait(
            StatusEvent(
                type="notification",
                summary=f"notification: {event_name}",
                messages=lines or None,
            )
        )
        return None

    return _check


def _make_stop_check(state: HookState) -> CheckFn:
    """Create a check that emits route-local stop notifications."""

    async def _check(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> SyncHookJSONOutput | None:
        _ = tool_use_id, context
        if hook_input.get("hook_event_name") != "Stop":
            return None
        session_id = hook_input.get("session_id")
        normalized_session_id = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        )
        if normalized_session_id is not None:
            state.session_id = normalized_session_id
        notifier = state.stop_event_notifier
        if notifier is None:
            return None
        payload = {
            "session_id": normalized_session_id,
            "hook_input": dict(hook_input),
            "schedule_run_active": bool(state.schedule_run_active),
            "execution_active": bool(state.execution_active),
        }
        await notifier(payload)
        return None

    return _check


# ---------------------------------------------------------------------------
# Factory: build hook matchers for ClaudeAgentOptions
# ---------------------------------------------------------------------------


def create_hook_matchers(
    config: OBSConfig,
    state: HookState,
) -> dict[str, list[HookMatcher]]:
    """Build hook matcher dict ready for ClaudeAgentOptions(hooks=...).

    PreToolUse pipeline: interrupt check -> native/immutable guard -> queue check
    PostToolUse pipeline: queue check
    """
    interrupt_check = _make_interrupt_check(state)
    immutable_check = _make_immutable_check(config)
    queue_check = _make_queue_check(state)
    notification_check = _make_notification_check(state)
    stop_check = _make_stop_check(state)

    pre_tool_pipeline = HookPipeline([interrupt_check, immutable_check, queue_check])
    post_tool_pipeline = HookPipeline([queue_check])
    notification_pipeline = HookPipeline([notification_check])
    stop_pipeline = HookPipeline([stop_check])

    return {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[pre_tool_pipeline]),
        ],
        "PostToolUse": [
            HookMatcher(matcher=None, hooks=[post_tool_pipeline]),
        ],
        "Notification": [
            HookMatcher(matcher=None, hooks=[notification_pipeline]),
        ],
        "SubagentStart": [
            HookMatcher(matcher=None, hooks=[notification_pipeline]),
        ],
        "SubagentStop": [
            HookMatcher(matcher=None, hooks=[notification_pipeline]),
        ],
        "Stop": [
            HookMatcher(matcher=None, hooks=[stop_pipeline]),
        ],
    }
