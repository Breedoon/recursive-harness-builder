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

from obs_agent.context_probe import probe_context_via_claude_cli
from obs_agent.context_stats import (
    apply_context_probe,
    build_context_snapshot,
    format_context_snapshot_lines,
)

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

    async def _render_context_and_session() -> str:
        data = hook_state.last_result_data if hook_state is not None else None
        snapshot = build_context_snapshot(
            session_id=get_session_id(),
            data=data,
            context_window_estimate_tokens=config.context_window_estimate_tokens,
            cwd=config.vault_path,
        )
        probe = None
        if config.context_probe_claude_cli:
            probe = await probe_context_via_claude_cli(
                session_id=snapshot.get("session_id"),
                cwd=config.vault_path,
            )
        snapshot = apply_context_probe(snapshot, probe)
        return "\n".join(format_context_snapshot_lines(snapshot))

    @tool(
        "session_info",
        "Return current session + usage snapshot: session ID, turn count, latest usage, "
        "billing token totals, and estimated context remaining.",
        {},
    )
    async def session_info(args: dict) -> dict:
        return {"content": [{"type": "text", "text": await _render_context_and_session()}]}

    @tool(
        "context_info",
        "Return current session + context usage snapshot including latest input/output/cache "
        "token stats, billing token total, and estimated context remaining.",
        {},
    )
    async def context_info(args: dict) -> dict:
        return {"content": [{"type": "text", "text": await _render_context_and_session()}]}

    server = create_sdk_mcp_server("obs-agent", tools=[self_fork, session_info, context_info])
    return server
