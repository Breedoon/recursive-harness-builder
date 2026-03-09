"""MCP tools for OBS Agent.

Provides in-process MCP tools that the Claude Agent SDK exposes to the model.
Current task orchestration primitives: ``AgentTask`` (with ``fork`` mode),
``AgentTaskOutput``, and ``AgentTaskStop``.

Compatibility aliases are preserved for ``ForkTask*`` names.

See decisions D018 (forking as core primitive).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from claude_agent_sdk import tool, create_sdk_mcp_server

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
_INBOX_FILE_LOCKS: dict[Path, asyncio.Lock] = {}


def _error_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _transport_unavailable(tool_name: str) -> dict:
    return _error_result(
        f"Cannot use {tool_name}: this transport does not provide task orchestration"
    )


def _coerce_bool_arg(value, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ValueError(f"{name} must be true or false")


def _normalize_resume_arg(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.lower() in {"false", "none", "null", "nil", "0", "no"}:
        return None
    return normalized


def _inbox_lock(path: Path) -> asyncio.Lock:
    lock = _INBOX_FILE_LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _INBOX_FILE_LOCKS[path] = lock
    return lock


def create_obs_tools(
    config: OBSConfig,
    get_session_id: Callable[[], str | None],
    hook_state: HookState | None = None,
):
    """Create the OBS Agent MCP tool server.

    get_session_id is a callable that returns the current session_id
    (closure over daemon state).

    hook_state is optional. When provided, transport-owned task tools such as
    ``ForkTask`` can delegate launch behavior through hook_state callbacks.
    """

    async def _launch_task(
        args: dict,
        *,
        tool_name: str,
        default_fork: bool,
    ) -> dict:
        prompt = str(args.get("prompt", "")).strip()
        description = str(args.get("description", "")).strip() or None
        resume = _normalize_resume_arg(args.get("resume"))
        run_in_background = args.get("run_in_background")
        team_name = str(args.get("team_name", "")).strip() or None
        agent_name = str(args.get("name") or args.get("agent_name") or "").strip() or None
        fork = default_fork
        if "fork" in args:
            try:
                fork = _coerce_bool_arg(args.get("fork"), name="fork")
            except ValueError:
                return _error_result(f"Cannot launch {tool_name}: fork must be true or false")
        timeout_ms_raw = args.get("timeout_ms")
        max_turns_raw = args.get("max_turns")
        if not prompt:
            return _error_result(f"Cannot launch {tool_name}: prompt is required")

        if run_in_background is not None:
            try:
                run_in_background = _coerce_bool_arg(
                    run_in_background,
                    name="run_in_background",
                )
            except ValueError:
                return _error_result(
                    f"Cannot launch {tool_name}: run_in_background must be true"
                )
            if run_in_background is False:
                return _error_result(f"Cannot launch {tool_name}: run_in_background must be true")

        if hook_state is None or hook_state.fork_task_launcher is None:
            return _transport_unavailable(tool_name)

        timeout_ms: int | None = None
        if timeout_ms_raw is not None:
            try:
                timeout_ms = max(int(timeout_ms_raw), 1)
            except (TypeError, ValueError):
                return _error_result(f"Cannot launch {tool_name}: timeout_ms must be an integer")
        max_turns: int | None = None
        if max_turns_raw is not None:
            try:
                max_turns = int(max_turns_raw)
            except (TypeError, ValueError):
                return _error_result(f"Cannot launch {tool_name}: max_turns must be an integer")
            if max_turns <= 0:
                return _error_result(f"Cannot launch {tool_name}: max_turns must be positive")

        try:
            effective_session_id = get_session_id()
            if not effective_session_id and hook_state is not None:
                effective_session_id = hook_state.session_id
            return await hook_state.fork_task_launcher({
                "session_id": effective_session_id,
                "prompt": prompt,
                "description": description,
                "resume": resume,
                "run_in_background": True,
                "timeout_ms": timeout_ms,
                "max_turns": max_turns,
                "fork": fork,
                "team_name": team_name,
                "agent_name": agent_name,
                "task_tool_name": tool_name,
                "tool_use_id": hook_state.current_tool_use_id,
            })
        except Exception as exc:
            logger.exception("%s launch failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    @tool(
        "ForkTask",
        "Compatibility alias for AgentTask with fork=true.",
        {
            "prompt": {
                "type": "string",
                "description": "Full task prompt for the forked child session",
            },
            "description": {
                "type": "string",
                "description": "Short user-facing label for the child topic",
            },
            "resume": {
                "type": "string",
                "description": "Optional agentId to resume an existing fork task",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Must be true for ForkTask",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional timeout budget in milliseconds for the child task",
            },
            "max_turns": {
                "type": "integer",
                "description": "Optional max turns for child execution; defaults to unbounded if omitted",
            },
            "name": {
                "type": "string",
                "description": "Optional worker name (team workflows).",
            },
            "team_name": {
                "type": "string",
                "description": "Optional team/task-list name for native team env bootstrap.",
            },
        },
    )
    async def fork_task(args: dict) -> dict:
        return await _launch_task(args, tool_name="ForkTask", default_fork=True)

    @tool(
        "AgentTask",
        "Launch a delegated child agent in a new Telegram topic. "
        "Set fork=true to continue from current session head, or fork=false to start "
        "a fresh child session in the new topic.",
        {
            "prompt": {
                "type": "string",
                "description": "Full task prompt for the child session",
            },
            "description": {
                "type": "string",
                "description": "Short user-facing label for the child topic",
            },
            "resume": {
                "type": "string",
                "description": "Optional agentId to resume an existing child task",
            },
            "fork": {
                "type": "boolean",
                "description": "When true, fork from parent head; when false, start fresh",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Must be true for AgentTask",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional timeout budget in milliseconds for the child task",
            },
            "max_turns": {
                "type": "integer",
                "description": "Optional max turns for child execution; defaults to unbounded if omitted",
            },
            "name": {
                "type": "string",
                "description": "Optional worker name (team workflows).",
            },
            "team_name": {
                "type": "string",
                "description": "Optional team/task-list name for native team env bootstrap.",
            },
        },
    )
    async def agent_task(args: dict) -> dict:
        return await _launch_task(args, tool_name="AgentTask", default_fork=True)

    async def _task_output(args: dict, *, tool_name: str) -> dict:
        task_id = str(args.get("task_id", "")).strip()
        block_raw = args.get("block")
        timeout = args.get("timeout")
        if not task_id:
            return _error_result(f"Cannot use {tool_name}: task_id is required")
        try:
            block = _coerce_bool_arg(block_raw, name="block")
        except ValueError:
            return _error_result(f"Cannot use {tool_name}: block must be true or false")
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return _error_result(f"Cannot use {tool_name}: timeout must be an integer")
        if timeout < 0:
            return _error_result(f"Cannot use {tool_name}: timeout must be non-negative")
        if hook_state is None or hook_state.fork_task_outputter is None:
            return _transport_unavailable(tool_name)
        try:
            return await hook_state.fork_task_outputter(
                {
                    "task_id": task_id,
                    "block": block,
                    "timeout": timeout,
                    "tool_use_id": hook_state.current_tool_use_id,
                }
            )
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    @tool(
        "ForkTaskOutput",
        "Compatibility alias for AgentTaskOutput (task_id, block, timeout).",
        {
            "task_id": {
                "type": "string",
                "description": "The agentId/task handle returned by ForkTask",
            },
            "block": {
                "type": "boolean",
                "description": "Whether to wait for completion",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum wait time in milliseconds",
            },
        },
    )
    async def fork_task_output(args: dict) -> dict:
        return await _task_output(args, tool_name="ForkTaskOutput")

    @tool(
        "AgentTaskOutput",
        "Inspect a running or completed AgentTask using native TaskOutput-style "
        "parameters. Use task_id, block, and timeout.",
        {
            "task_id": {
                "type": "string",
                "description": "The agentId/task handle returned by AgentTask",
            },
            "block": {
                "type": "boolean",
                "description": "Whether to wait for completion",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum wait time in milliseconds",
            },
        },
    )
    async def agent_task_output(args: dict) -> dict:
        return await _task_output(args, tool_name="AgentTaskOutput")

    async def _task_stop(args: dict, *, tool_name: str) -> dict:
        task_id = str(args.get("task_id") or args.get("shell_id") or "").strip()
        if not task_id:
            return _error_result(f"Cannot use {tool_name}: task_id is required")
        if hook_state is None or hook_state.fork_task_stopper is None:
            return _transport_unavailable(tool_name)
        try:
            return await hook_state.fork_task_stopper(
                {"task_id": task_id, "tool_use_id": hook_state.current_tool_use_id}
            )
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    @tool(
        "ForkTaskStop",
        "Compatibility alias for AgentTaskStop.",
        {
            "task_id": {
                "type": "string",
                "description": "The agentId/task handle returned by ForkTask",
            },
            "shell_id": {
                "type": "string",
                "description": "Deprecated alias for task_id, matching native TaskStop",
            },
        },
    )
    async def fork_task_stop(args: dict) -> dict:
        return await _task_stop(args, tool_name="ForkTaskStop")

    @tool(
        "AgentTaskStop",
        "Stop a running AgentTask using native TaskStop-style task_id input.",
        {
            "task_id": {
                "type": "string",
                "description": "The agentId/task handle returned by AgentTask",
            },
            "shell_id": {
                "type": "string",
                "description": "Deprecated alias for task_id, matching native TaskStop",
            },
        },
    )
    async def agent_task_stop(args: dict) -> dict:
        return await _task_stop(args, tool_name="AgentTaskStop")

    async def _send_inbox_message(args: dict) -> dict:
        team_name = str(args.get("team_name", "")).strip()
        recipient = str(args.get("recipient", "")).strip()
        content = str(args.get("content", "")).strip()
        summary = str(args.get("summary", "")).strip() or None
        sender = str(args.get("sender", "")).strip() or "obs-worker"
        if not team_name:
            return _error_result("Cannot use SendInboxMessage: team_name is required")
        if not recipient:
            return _error_result("Cannot use SendInboxMessage: recipient is required")
        if not content:
            return _error_result("Cannot use SendInboxMessage: content is required")

        inbox_path = (
            Path.home()
            / ".claude"
            / "teams"
            / team_name
            / "inboxes"
            / f"{recipient}.json"
        )
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _inbox_lock(inbox_path)
        async with lock:
            entries: list[dict] = []
            if inbox_path.exists():
                try:
                    loaded = json.loads(inbox_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        entries = [item for item in loaded if isinstance(item, dict)]
                except Exception:
                    logger.warning("Failed reading inbox JSON: %s", inbox_path, exc_info=True)
            message = {
                "from": sender,
                "text": content,
                "summary": summary or "",
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "read": False,
            }
            entries.append(message)
            inbox_path.write_text(json.dumps(entries, ensure_ascii=True), encoding="utf-8")

        response = {
            "success": True,
            "team_name": team_name,
            "recipient": recipient,
            "message_count": len(entries),
        }
        if hook_state is not None and hook_state.inbox_message_notifier is not None:
            try:
                await hook_state.inbox_message_notifier(
                    {
                        "team_name": team_name,
                        "recipient": recipient,
                        "sender": sender,
                        "content": content,
                        "summary": summary,
                    }
                )
            except Exception:
                logger.warning("SendInboxMessage notifier failed", exc_info=True)
        return {
            "content": [{"type": "text", "text": json.dumps(response, ensure_ascii=True)}],
            "tool_use_result": response,
        }

    async def _read_inbox(args: dict) -> dict:
        team_name = str(args.get("team_name", "")).strip()
        agent = str(args.get("agent", "")).strip()
        if not team_name:
            return _error_result("Cannot use ReadInbox: team_name is required")
        if not agent:
            return _error_result("Cannot use ReadInbox: agent is required")
        include_read = bool(args.get("include_read", False))
        mark_read = bool(args.get("mark_read", True))
        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError):
            return _error_result("Cannot use ReadInbox: limit must be an integer")
        if limit <= 0:
            return _error_result("Cannot use ReadInbox: limit must be positive")

        inbox_path = (
            Path.home()
            / ".claude"
            / "teams"
            / team_name
            / "inboxes"
            / f"{agent}.json"
        )
        lock = _inbox_lock(inbox_path)
        async with lock:
            entries: list[dict] = []
            if inbox_path.exists():
                try:
                    loaded = json.loads(inbox_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        entries = [item for item in loaded if isinstance(item, dict)]
                except Exception:
                    logger.warning("Failed reading inbox JSON: %s", inbox_path, exc_info=True)

            selected = [
                item for item in entries
                if include_read or not bool(item.get("read", False))
            ]
            if len(selected) > limit:
                selected = selected[-limit:]
            if mark_read and entries:
                changed = False
                for item in entries:
                    if not bool(item.get("read", False)):
                        item["read"] = True
                        changed = True
                if changed:
                    inbox_path.parent.mkdir(parents=True, exist_ok=True)
                    inbox_path.write_text(json.dumps(entries, ensure_ascii=True), encoding="utf-8")

        result = {
            "team_name": team_name,
            "agent": agent,
            "count": len(selected),
            "messages": selected,
        }
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
            "tool_use_result": result,
        }

    @tool(
        "SendInboxMessage",
        "Write a message to a native-compatible team inbox JSON file.",
        {
            "team_name": {"type": "string", "description": "Team name"},
            "recipient": {"type": "string", "description": "Recipient agent name"},
            "content": {"type": "string", "description": "Message body"},
            "summary": {"type": "string", "description": "Optional short summary"},
            "sender": {"type": "string", "description": "Optional sender label"},
        },
    )
    async def send_inbox_message(args: dict) -> dict:
        return await _send_inbox_message(args)

    @tool(
        "ReadInbox",
        "Read messages from a native-compatible team inbox JSON file.",
        {
            "team_name": {"type": "string", "description": "Team name"},
            "agent": {"type": "string", "description": "Agent inbox to read"},
            "include_read": {"type": "boolean", "description": "Include already-read messages"},
            "mark_read": {"type": "boolean", "description": "Mark unread messages as read"},
            "limit": {"type": "integer", "description": "Maximum number of messages to return"},
        },
    )
    async def read_inbox(args: dict) -> dict:
        return await _read_inbox(args)

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

    server = create_sdk_mcp_server(
        "obs-agent",
        tools=[
            agent_task,
            agent_task_output,
            agent_task_stop,
            send_inbox_message,
            read_inbox,
            fork_task,
            fork_task_output,
            fork_task_stop,
            session_info,
            context_info,
        ],
    )
    return server
