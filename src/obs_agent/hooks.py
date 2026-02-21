"""SDK hooks for OBS Agent.

- PreToolUse: guards immutable files and .env from writes
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

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


# Write-mutating tool names that the guard should check
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Read-only tools that are always allowed
_READ_TOOLS = {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"}

# File patterns that are always blocked from writes (beyond config immutable_patterns)
_BLOCKED_FILE_PATTERNS = [".env"]


def _deny(reason: str) -> dict:
    """Return a deny hook response with reason."""
    return {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "reason": reason,
        }
    }


def on_pre_tool_use(
    tool_name: str,
    tool_input: dict,
    *,
    config: OBSConfig,
) -> dict | None:
    """Guard hook: block writes to immutable files and .env.

    Returns None to allow, or a deny dict to block.
    """
    # Only guard write-mutating tools
    if tool_name not in _WRITE_TOOLS:
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
    session_id: str | None = None
    background_tasks: set[asyncio.Task] = field(default_factory=set)
    last_result_data: dict | None = None  # last ResultMessage metrics

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
        self.session_id = None
        self.last_result_data = None


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
                    merged["hookSpecificOutput"]["additionalContext"] = "\n\n".join(accumulated_context)
                merged["continue_"] = False
                if "stopReason" in result:
                    merged["stopReason"] = result["stopReason"]
                return merged

            # Check for short-circuit: permissionDecision is "deny"
            if hso and hso.get("permissionDecision") == "deny":
                if accumulated_context:
                    merged.setdefault("hookSpecificOutput", {})
                    merged["hookSpecificOutput"]["additionalContext"] = "\n\n".join(accumulated_context)
                merged.setdefault("hookSpecificOutput", {})
                merged["hookSpecificOutput"]["permissionDecision"] = "deny"
                if "reason" in hso:
                    merged["hookSpecificOutput"]["reason"] = hso["reason"]
                return merged

        # No short-circuit — return accumulated context if any
        if accumulated_context:
            merged.setdefault("hookSpecificOutput", {})
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
            return {
                "continue_": False,
                "stopReason": "Interrupted by user",
            }
        return None

    return _check


def _make_immutable_check(config: OBSConfig) -> CheckFn:
    """Create a check that wraps on_pre_tool_use() into SDK callback format.

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

        # Convert the existing deny dict to SyncHookJSONOutput format
        return {"hookSpecificOutput": result.get("hookSpecificOutput", {})}

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
        messages: list[str] = []
        while not state.message_queue.empty():
            try:
                msg = state.message_queue.get_nowait()
                messages.append(msg)
            except asyncio.QueueEmpty:
                break

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
                "additionalContext": formatted,
            }
        }

    return _check


# ---------------------------------------------------------------------------
# Factory: build hook matchers for ClaudeAgentOptions
# ---------------------------------------------------------------------------


def create_hook_matchers(
    config: OBSConfig,
    state: HookState,
) -> dict[str, list[HookMatcher]]:
    """Build hook matcher dict ready for ClaudeAgentOptions(hooks=...).

    PreToolUse pipeline: interrupt check -> immutable guard -> queue check
    PostToolUse pipeline: queue check
    """
    interrupt_check = _make_interrupt_check(state)
    immutable_check = _make_immutable_check(config)
    queue_check = _make_queue_check(state)

    pre_tool_pipeline = HookPipeline([interrupt_check, immutable_check, queue_check])
    post_tool_pipeline = HookPipeline([queue_check])

    return {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[pre_tool_pipeline]),
        ],
        "PostToolUse": [
            HookMatcher(matcher=None, hooks=[post_tool_pipeline]),
        ],
    }
