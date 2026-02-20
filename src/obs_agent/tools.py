"""MCP tools for OBS Agent.

Provides in-process MCP tools that the Claude Agent SDK exposes to the model.
Currently: self_fork - agent-controlled session forking for subtasks.

See decisions D018 (forking as core primitive).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query, tool, create_sdk_mcp_server

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig
    from obs_agent.hooks import HookState

logger = logging.getLogger("obs_agent.tools")


def create_obs_tools(
    config: OBSConfig,
    get_session_id: Callable[[], str | None],
    hook_state: HookState | None = None,
):
    """Create the OBS Agent MCP tool server.

    get_session_id is a callable that returns the current session_id
    (closure over daemon state).

    hook_state is optional — when provided, background forks enqueue
    their results to hook_state.message_queue for delivery at the next
    hook boundary.
    """

    # Use hook_state.background_tasks if available, else local set (for GC prevention)
    _background_tasks: set[asyncio.Task] = hook_state.background_tasks if hook_state is not None else set()

    async def _run_fork_background(task_text: str, max_turns: int, session_id: str) -> None:
        """Run a fork in the background and enqueue the result."""
        try:
            options = ClaudeAgentOptions(
                resume=session_id,
                fork_session=True,
                max_turns=max_turns,
                permission_mode="bypassPermissions",
                cwd=str(config.vault_path),
            )

            result_parts: list[str] = []
            async for message in query(prompt=task_text, options=options):
                if hasattr(message, "content") and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_parts.append(block.text)

            text = "\n".join(result_parts) or "(fork returned no output)"
            if hook_state is not None:
                hook_state.message_queue.put_nowait(
                    f'[Background fork completed: "{task_text}"]: {text}'
                )
            logger.info("Background fork completed: %s", task_text[:80])
        except Exception as exc:
            error_msg = f'[Background fork error: "{task_text}"]: {type(exc).__name__}: {exc}'
            if hook_state is not None:
                hook_state.message_queue.put_nowait(error_msg)
            logger.error("Background fork failed: %s — %s", task_text[:80], exc)

    @tool(
        "self_fork",
        "Fork this conversation to perform a subtask. The fork inherits full "
        "conversation history and prompt cache. Use for: research, file analysis, "
        "multi-step operations, or any task where a copy of yourself should work "
        "independently. Set background=true to run the fork without blocking — "
        "results are delivered via the message queue when the fork completes. "
        "Returns the fork's text response (or a launch confirmation if background).",
        {
            "task": {
                "type": "string",
                "description": "What the forked session should accomplish",
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum turns for the fork (default 3, max 10)",
            },
            "background": {
                "type": "boolean",
                "description": "Run the fork in the background (default false). "
                "Results are delivered via the message queue when the fork completes.",
            },
        },
    )
    async def self_fork(args: dict) -> dict:
        task_text = args["task"]
        max_turns = min(int(args.get("max_turns", 3)), 10)
        background = bool(args.get("background", False))
        session_id = get_session_id()

        if not session_id:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Cannot fork: no active session yet (need at least one completed turn)",
                    }
                ]
            }

        if background:
            if hook_state is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Cannot run background fork: no message queue available (hook_state not configured)",
                        }
                    ]
                }
            task = asyncio.create_task(_run_fork_background(task_text, max_turns, session_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f'Background fork launched for: "{task_text}". '
                        "Results will be delivered via the message queue when complete.",
                    }
                ]
            }

        # Foreground (blocking) fork — existing behavior
        options = ClaudeAgentOptions(
            resume=session_id,
            fork_session=True,
            max_turns=max_turns,
            permission_mode="bypassPermissions",
            cwd=str(config.vault_path),
        )

        result_parts: list[str] = []
        async for message in query(prompt=task_text, options=options):
            if hasattr(message, "content") and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)

        text = "\n".join(result_parts) or "(fork returned no output)"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "session_info",
        "Return current session metadata: session ID, turn count, total cost, "
        "and duration. Use when the user asks about the current session.",
        {},
    )
    async def session_info(args: dict) -> dict:
        data = hook_state.last_result_data if hook_state is not None else None
        if data is None:
            return {
                "content": [
                    {"type": "text", "text": "No session data available yet (need at least one completed turn)."}
                ]
            }
        lines = [
            f"session_id: {data.get('session_id')}",
            f"num_turns: {data.get('num_turns')}",
            f"total_cost_usd: {data.get('total_cost_usd')}",
            f"duration_ms: {data.get('duration_ms')}",
        ]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "context_info",
        "Return context window usage: input/output token counts, cache stats, "
        "and estimated percentage of context remaining (200k window). "
        "Use when the user asks about context usage or tokens.",
        {},
    )
    async def context_info(args: dict) -> dict:
        data = hook_state.last_result_data if hook_state is not None else None
        usage = data.get("usage") if data else None
        if usage is None:
            return {
                "content": [
                    {"type": "text", "text": "No usage data available yet (need at least one completed turn)."}
                ]
            }

        input_tokens = usage.get("input_tokens") or 0
        output_tokens = usage.get("output_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        # Total context usage includes cached tokens (they still occupy the window)
        total_used = input_tokens + output_tokens + cache_creation + cache_read
        context_window = 200_000
        pct_remaining = max(0.0, (1 - total_used / context_window) * 100)

        lines = [
            f"input_tokens: {input_tokens}",
            f"output_tokens: {output_tokens}",
            f"cache_creation_input_tokens: {cache_creation}",
            f"cache_read_input_tokens: {cache_read}",
            f"total_tokens_used: {total_used}",
            f"context_window: {context_window}",
            f"pct_remaining: {pct_remaining:.1f}%",
        ]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    server = create_sdk_mcp_server("obs-agent", tools=[self_fork, session_info, context_info])
    return server
