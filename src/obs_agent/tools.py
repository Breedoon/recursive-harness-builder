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
from typing import TYPE_CHECKING, Any, Callable

from claude_agent_sdk import tool, create_sdk_mcp_server

from obs_agent.context_probe import probe_context_via_claude_cli
from obs_agent.context_stats import (
    apply_context_probe,
    build_context_snapshot,
    format_context_snapshot_lines,
)
from obs_agent.lineage import (
    find_latest_obs_bootstrap_for_session,
    lineage_fingerprint,
    native_agent_name_for_lineage,
    normalize_lineage_name,
)

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig
    from obs_agent.hooks import HookState

logger = logging.getLogger("obs_agent.tools")
_INBOX_FILE_LOCKS: dict[Path, asyncio.Lock] = {}


def _error_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def validate_must_reply_recipient(
    *, sender: str, recipient: str, must_reply: bool
) -> dict[str, Any]:
    """Check whether a must_reply message is valid.

    Returns ``{"ok": True}`` on success or ``{"ok": False, "error": "..."}``
    when the message should be rejected (e.g. must_reply to self).
    """
    if not must_reply:
        return {"ok": True}
    if sender == recipient:
        return {"ok": False, "error": "must_reply to self is blocked (would cause infinite wake loop)"}
    return {"ok": True}


def detect_must_reply_completions(
    inbox_entries: list[dict],
    recipient_of_outgoing_message: str,
) -> tuple[list[dict], bool]:
    """Mark must_reply messages from *recipient_of_outgoing_message* as replied.

    When agent B sends a message to agent A, B calls this on B's own inbox
    entries to mark must_reply messages from A as ``replied: True``.

    Returns ``(updated_entries, all_replied)`` where *all_replied* indicates
    whether every must_reply message in *inbox_entries* is now replied.
    """
    changed = False
    for entry in inbox_entries:
        if (
            isinstance(entry, dict)
            and entry.get("must_reply") is True
            and entry.get("replied") is not True
            and entry.get("from") == recipient_of_outgoing_message
        ):
            entry["replied"] = True
            changed = True
    # Check if ALL must_reply obligations are now cleared
    all_replied = not any(
        isinstance(e, dict)
        and e.get("must_reply") is True
        and e.get("replied") is not True
        for e in inbox_entries
    )
    return inbox_entries, all_replied


def check_and_clear_must_reply_obligations(
    inbox_entries: list[dict],
) -> bool:
    """Check whether all must_reply messages have been replied to.

    Returns ``True`` if there are no unreplied must_reply messages
    (i.e. the reply_wake schedule can be deleted).
    """
    return not any(
        isinstance(e, dict)
        and e.get("must_reply") is True
        and e.get("replied") is not True
        for e in inbox_entries
    )



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

    def _current_obs_bootstrap():
        session_id = get_session_id()
        if not session_id and hook_state is not None:
            session_id = hook_state.session_id
        return find_latest_obs_bootstrap_for_session(
            session_id=session_id,
            cwd=config.vault_path,
        )

    async def _launch_task(
        args: dict,
        *,
        tool_name: str,
        default_fork: bool,
    ) -> dict:
        prompt = str(args.get("prompt", "")).strip()
        description = str(args.get("alias") or args.get("description") or "").strip() or None
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
            "alias": {
                "type": "string",
                "description": "Short stable alias for the child agent/topic.",
            },
            "description": {
                "type": "string",
                "description": "Deprecated alias for alias.",
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
            "alias": {
                "type": "string",
                "description": "Short stable alias for the child agent/topic.",
            },
            "description": {
                "type": "string",
                "description": "Deprecated alias for alias.",
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

    async def _cron_create(args: dict, *, tool_name: str) -> dict:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return _error_result(f"Cannot use {tool_name}: prompt is required")
        cron = str(args.get("cron", "")).strip()
        schedule_mode_raw = args.get("schedule_mode")
        schedule_mode = (
            str(schedule_mode_raw).strip().lower()
            if schedule_mode_raw is not None
            else ""
        )
        if not schedule_mode:
            if args.get("interval_seconds") is not None:
                schedule_mode = "interval"
            elif cron:
                schedule_mode = "cron"
        if schedule_mode not in {"interval", "cron"}:
            return _error_result(
                f"Cannot use {tool_name}: schedule_mode must be interval or cron"
            )
        if schedule_mode == "cron" and not cron:
            return _error_result(f"Cannot use {tool_name}: cron is required for schedule_mode=cron")

        interval_seconds_raw = args.get("interval_seconds")
        interval_seconds: int | None = None
        if interval_seconds_raw is not None:
            try:
                interval_seconds = int(interval_seconds_raw)
            except (TypeError, ValueError):
                return _error_result(
                    f"Cannot use {tool_name}: interval_seconds must be an integer"
                )
            if interval_seconds < 0:
                return _error_result(
                    f"Cannot use {tool_name}: interval_seconds must be non-negative"
                )
        max_runs_raw = args.get("max_runs")
        max_runs: int = 1
        if max_runs_raw is not None:
            try:
                max_runs = int(max_runs_raw)
            except (TypeError, ValueError):
                return _error_result(f"Cannot use {tool_name}: max_runs must be an integer")
            if max_runs <= 0:
                return _error_result(f"Cannot use {tool_name}: max_runs must be positive")

        reset_session_raw = args.get("reset_session")
        reset_session: bool | None = None
        if reset_session_raw is not None:
            if isinstance(reset_session_raw, bool):
                reset_session = reset_session_raw
            elif isinstance(reset_session_raw, (int, float)):
                reset_session = bool(int(reset_session_raw))
            elif isinstance(reset_session_raw, str):
                normalized = reset_session_raw.strip().lower()
                if normalized in {"true", "1", "yes", "on"}:
                    reset_session = True
                elif normalized in {"false", "0", "no", "off"}:
                    reset_session = False
                else:
                    return _error_result(
                        f"Cannot use {tool_name}: reset_session must be a boolean"
                    )
            else:
                return _error_result(
                    f"Cannot use {tool_name}: reset_session must be a boolean"
                )

        legacy_run_mode = str(args.get("run_mode") or "").strip().lower()
        if reset_session is None and legacy_run_mode:
            if legacy_run_mode not in {"continue", "reset_session"}:
                return _error_result(
                    f"Cannot use {tool_name}: run_mode must be continue or reset_session"
                )
            reset_session = legacy_run_mode == "reset_session"
        if reset_session is None:
            reset_session = False

        inherit = str(args.get("inherit") or "").strip().lower() or "none"
        if inherit not in {"none", "fork", "all"}:
            return _error_result(
                f"Cannot use {tool_name}: inherit must be none, fork, or all"
            )

        if hook_state is None or hook_state.cron_creator is None:
            return _transport_unavailable(tool_name)
        try:
            return await hook_state.cron_creator(
                {
                    "schedule_mode": schedule_mode,
                    "cron": cron,
                    "prompt": prompt,
                    "interval_seconds": interval_seconds,
                    "reset_session": reset_session,
                    "description": str(args.get("description") or "").strip() or None,
                    "max_runs": max_runs,
                    "from": str(args.get("from") or "").strip() or None,
                    "until": str(args.get("until") or "").strip() or None,
                    "inherit": inherit,
                    "tool_use_id": hook_state.current_tool_use_id,
                }
            )
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    async def _cron_list(args: dict, *, tool_name: str) -> dict:
        _ = args
        if hook_state is None or hook_state.cron_lister is None:
            return _transport_unavailable(tool_name)
        try:
            return await hook_state.cron_lister({"tool_use_id": hook_state.current_tool_use_id})
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    async def _cron_delete(args: dict, *, tool_name: str) -> dict:
        schedule_id = str(args.get("id") or "").strip()
        if not schedule_id:
            return _error_result(f"Cannot use {tool_name}: id is required")
        if hook_state is None or hook_state.cron_deleter is None:
            return _transport_unavailable(tool_name)
        try:
            return await hook_state.cron_deleter(
                {"id": schedule_id, "tool_use_id": hook_state.current_tool_use_id}
            )
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    @tool(
        "CronCreate",
        "Create a per-topic schedule in interval or cron mode. "
        "Interval mode is inactivity-based: after each topic turn finishes, "
        "next_run_at is reset to now + interval_seconds (not fixed wall-clock slots). "
        "Cron mode is wall-clock based (standard 5-field cron). "
        "Schedules run only when the topic is idle and the bot is available. "
        "Default behavior is one-shot (max_runs=1). "
        "Repeat by setting max_runs > 1.",
        {
            "schedule_mode": {
                "type": "string",
                "description": (
                    "interval or cron. interval = inactivity-based (re-anchored on topic completion/stop); "
                    "cron = wall-clock. If omitted, inferred from provided fields."
                ),
            },
            "cron": {
                "type": "string",
                "description": (
                    "Cron expression for schedule_mode=cron (wall-clock). "
                    "Standard 5-field cron syntax."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "Prompt injected when this schedule fires.",
            },
            "interval_seconds": {
                "type": "integer",
                "description": (
                    "Interval seconds for schedule_mode=interval. "
                    "After each topic completion/stop, next run is scheduled after this many seconds. "
                    "0 means trigger on topic stop."
                ),
            },
            "reset_session": {
                "type": "boolean",
                "description": "If true, clear the session before each scheduled run.",
            },
            "description": {
                "type": "string",
                "description": "Optional short schedule name shown in completion messages.",
            },
            "max_runs": {
                "type": "integer",
                "description": "Run cap. Defaults to 1 when omitted.",
            },
            "from": {
                "type": "string",
                "description": "Optional RFC3339 start timestamp for active window start (inclusive).",
            },
            "until": {
                "type": "string",
                "description": "Optional RFC3339 cutoff timestamp; schedule stops at/after this time.",
            },
            "inherit": {
                "type": "string",
                "description": "Optional inheritance mode: none, fork, or all.",
            },
            "run_mode": {
                "type": "string",
                "description": "Deprecated alias: continue or reset_session.",
            },
        },
    )
    async def cron_create(args: dict) -> dict:
        return await _cron_create(args, tool_name="CronCreate")

    @tool(
        "CronList",
        "List schedules for the current topic route only, including next_run_at and run counters.",
        {},
    )
    async def cron_list(args: dict) -> dict:
        return await _cron_list(args, tool_name="CronList")

    @tool(
        "CronDelete",
        "Delete one schedule by ID in the current topic route.",
        {
            "id": {
                "type": "string",
                "description": "Schedule ID returned by CronCreate.",
            },
        },
    )
    async def cron_delete(args: dict) -> dict:
        # Agent-initiated schedule deletion is deprecated.
        # Agents were deleting their own schedules unprompted, undermining user intent.
        # Users can still delete schedules via /unschedule command.
        # Considering reintroduction with guardrails (e.g., user confirmation,
        # only delete schedules the agent created).
        return _error_result(
            "CronDelete is disabled for agents. "
            "Schedules can only be removed by the user via /unschedule command."
        )

    async def _send_inbox_message(args: dict) -> dict:
        bootstrap = _current_obs_bootstrap()
        team_name = str(args.get("team_name", "")).strip() or (
            bootstrap.root_team_key if bootstrap is not None else ""
        )
        recipient = str(args.get("recipient", "")).strip()
        content = str(args.get("content", "")).strip()
        summary = str(args.get("summary", "")).strip() or None
        sender = str(args.get("sender", "")).strip() or (
            bootstrap.native_agent_name if bootstrap is not None and bootstrap.native_agent_name else "obs-worker"
        )
        must_reply = bool(args.get("must_reply", False))
        if not team_name:
            return _error_result(
                "Cannot use SendInboxMessage: team_name is required or must be inferable from current lineage"
            )
        if not recipient:
            return _error_result("Cannot use SendInboxMessage: recipient is required")
        if not content:
            return _error_result("Cannot use SendInboxMessage: content is required")
        # Block must_reply to self (would cause infinite wake loop)
        if must_reply and sender == recipient:
            return _error_result(
                "Cannot send must_reply to yourself — this would cause an infinite wake loop."
            )
        if hook_state is not None and hook_state.inbox_recipient_validator is not None:
            try:
                validation = await hook_state.inbox_recipient_validator(
                    {
                        "team_name": team_name,
                        "recipient": recipient,
                    }
                )
            except Exception:
                logger.warning("SendInboxMessage validator failed", exc_info=True)
                validation = {"deliverable": True}
            deliverable = bool(validation.get("deliverable", True)) if isinstance(validation, dict) else bool(validation)
            if not deliverable:
                reason = ""
                if isinstance(validation, dict):
                    reason = str(validation.get("reason") or "").strip()
                reason = reason or "recipient is not a live agent in this tree"
                response = {
                    "success": False,
                    "delivered": False,
                    "team_name": team_name,
                    "recipient": recipient,
                    "error": reason,
                }
                return {
                    "content": [{"type": "text", "text": f"message undelivered: {reason}"}],
                    "tool_use_result": response,
                    "is_error": True,
                }

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
            message: dict[str, Any] = {
                "from": sender,
                "text": content,
                "summary": summary or "",
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "read": False,
            }
            if must_reply:
                message["must_reply"] = True
                message["replied"] = False
            entries.append(message)
            inbox_path.write_text(json.dumps(entries, ensure_ascii=True), encoding="utf-8")

        # Reply detection: if we just sent a message to `recipient`, check our OWN
        # inbox for must_reply messages FROM that recipient that are unreplied.
        # If found, mark them replied.  When ALL must_reply messages are replied,
        # delete the reply_wake schedule.
        # Reply detection: mark must_reply messages from `recipient` in our inbox as replied
        if sender and sender != recipient:
            sender_inbox_path = (
                Path.home()
                / ".claude"
                / "teams"
                / team_name
                / "inboxes"
                / f"{sender}.json"
            )
            if sender_inbox_path.exists():
                sender_lock = _inbox_lock(sender_inbox_path)
                async with sender_lock:
                    try:
                        sender_entries = json.loads(sender_inbox_path.read_text(encoding="utf-8"))
                        if isinstance(sender_entries, list):
                            updated, all_replied = detect_must_reply_completions(
                                sender_entries, recipient
                            )
                            # Only write if something changed
                            if any(
                                e.get("replied") is True
                                for e in updated
                                if e.get("must_reply") is True and e.get("from") == recipient
                            ):
                                sender_inbox_path.write_text(
                                    json.dumps(updated, ensure_ascii=True),
                                    encoding="utf-8",
                                )
                            if all_replied and hook_state is not None:
                                # All must_reply messages are replied — signal
                                # schedule cleanup (handled by telegram.py)
                                if hook_state.inbox_message_notifier is not None:
                                    try:
                                        await hook_state.inbox_message_notifier(
                                            {
                                                "team_name": team_name,
                                                "recipient": sender,
                                                "sender": recipient,
                                                "content": "__reply_wake_clear__",
                                                "summary": "all must_reply messages replied",
                                                "_reply_wake_clear": True,
                                            }
                                        )
                                    except Exception:
                                        logger.warning("Reply wake clear notification failed", exc_info=True)
                    except Exception:
                        logger.warning("Reply detection: failed reading sender inbox %s", sender_inbox_path, exc_info=True)

        response = {
            "success": True,
            "team_name": team_name,
            "recipient": recipient,
            "message_count": len(entries),
        }
        if hook_state is not None and hook_state.inbox_message_notifier is not None:
            try:
                notification_payload: dict[str, Any] = {
                    "team_name": team_name,
                    "recipient": recipient,
                    "sender": sender,
                    "content": content,
                    "summary": summary,
                }
                if must_reply:
                    notification_payload["_must_reply"] = True
                await hook_state.inbox_message_notifier(notification_payload)
            except Exception:
                logger.warning("SendInboxMessage notifier failed", exc_info=True)
        return {
            "content": [{"type": "text", "text": json.dumps(response, ensure_ascii=True)}],
            "tool_use_result": response,
        }

    async def _read_inbox(args: dict) -> dict:
        bootstrap = _current_obs_bootstrap()
        team_name = str(args.get("team_name", "")).strip() or (
            bootstrap.root_team_key if bootstrap is not None else ""
        )
        agent = str(args.get("agent", "")).strip() or (
            bootstrap.native_agent_name if bootstrap is not None else ""
        )
        if not team_name:
            return _error_result(
                "Cannot use ReadInbox: team_name is required or must be inferable from current lineage"
            )
        if not agent:
            return _error_result(
                "Cannot use ReadInbox: agent is required or must be inferable from current lineage"
            )
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
            "team_name": {"type": "string", "description": "Optional team name; defaults to current tree root team"},
            "recipient": {"type": "string", "description": "Recipient agent name"},
            "content": {"type": "string", "description": "Message body"},
            "summary": {"type": "string", "description": "Optional short summary"},
            "sender": {"type": "string", "description": "Optional sender label"},
            "must_reply": {"type": "boolean", "description": "If true, recipient will be reminded to reply. Creates a reply_wake schedule (interval=1s, max_runs=3)."},
        },
    )
    async def send_inbox_message(args: dict) -> dict:
        return await _send_inbox_message(args)

    @tool(
        "ReadInbox",
        "Read messages from a native-compatible team inbox JSON file.",
        {
            "team_name": {"type": "string", "description": "Optional team name; defaults to current tree root team"},
            "agent": {"type": "string", "description": "Optional agent inbox name; defaults to current native agent projection"},
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

    @tool(
        "session_lineage",
        "Return the current OBS lineage/bootstrap identity for this session.",
        {
            "include_xml": {
                "type": "boolean",
                "description": "Include the raw bootstrap XML payload in the response. Defaults to false.",
            },
        },
    )
    async def session_lineage(args: dict) -> dict:
        include_xml = bool(args.get("include_xml", False))
        bootstrap = _current_obs_bootstrap()
        if bootstrap is None:
            return _error_result("Cannot use session_lineage: no OBS bootstrap found for current session")
        payload: dict[str, Any] = {
            "lineage": list(bootstrap.lineage),
            "lineage_length": len(bootstrap.lineage),
            "origin": bootstrap.origin,
            "is_fork": bootstrap.is_fork,
            "session_id": bootstrap.session_id or get_session_id(),
            "agent_id": bootstrap.agent_id,
            "parent_session_id": bootstrap.parent_session_id,
            "root_team_key": bootstrap.root_team_key,
            "native_agent_name": bootstrap.native_agent_name,
            "path": "/".join(bootstrap.lineage),
        }
        # Compute agent_names for each node in the lineage
        agent_names = []
        for i in range(len(bootstrap.lineage)):
            sub = bootstrap.lineage[: i + 1]
            agent_names.append(native_agent_name_for_lineage(sub))
        payload["agent_names"] = agent_names
        if include_xml:
            payload["xml"] = bootstrap.raw_xml
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}],
            "tool_use_result": payload,
        }

    @tool(
        "get_family",
        "Find relatives in the team tree — children, siblings, or parent — by scanning inbox files.",
        {
            "who": {
                "type": "string",
                "description": "One of: children, siblings, parent, all",
            },
        },
    )
    async def get_family(args: dict) -> dict:
        who = str(args.get("who", "all")).strip().lower()
        if who not in {"children", "siblings", "parent", "all"}:
            return _error_result("get_family: 'who' must be one of: children, siblings, parent, all")
        bootstrap = _current_obs_bootstrap()
        if bootstrap is None:
            return _error_result("Cannot use get_family: no OBS bootstrap found")
        if not bootstrap.root_team_key or not bootstrap.native_agent_name:
            return _error_result("Cannot use get_family: missing team key or agent name")
        if not bootstrap.lineage:
            return _error_result("Cannot use get_family: empty lineage")

        inboxes_dir = (
            Path.home()
            / ".claude"
            / "teams"
            / bootstrap.root_team_key
            / "inboxes"
        )
        result: dict[str, list[str]] = {}
        all_agents = []
        if inboxes_dir.is_dir():
            for f in inboxes_dir.iterdir():
                if f.suffix == ".json" and f.stem:
                    all_agents.append(f.stem)

        my_lineage = bootstrap.lineage
        my_agent_name = bootstrap.native_agent_name
        my_hash = lineage_fingerprint(tuple(normalize_lineage_name(n) for n in my_lineage))

        # Children: agents whose name starts with my lineage hash
        if who in {"children", "all"}:
            children = [
                name for name in all_agents
                if name.startswith(f"{my_hash}-") and name != my_agent_name
            ]
            result["children"] = children

        # Parent: derive from my agent_name's hash prefix
        if who in {"parent", "all"}:
            if len(my_lineage) > 1:
                parent_lineage = my_lineage[:-1]
                parent_name = native_agent_name_for_lineage(parent_lineage)
                result["parent"] = [parent_name] if parent_name in all_agents else []
            else:
                result["parent"] = []  # trunk has no parent

        # Siblings: agents with same parent hash prefix as me
        if who in {"siblings", "all"}:
            if len(my_lineage) > 1:
                parent_lineage = tuple(normalize_lineage_name(n) for n in my_lineage[:-1])
                parent_hash = lineage_fingerprint(parent_lineage)
                siblings = [
                    name for name in all_agents
                    if name.startswith(f"{parent_hash}-") and name != my_agent_name
                ]
                result["siblings"] = siblings
            else:
                result["siblings"] = []  # trunk has no siblings

        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
            "tool_use_result": result,
        }

    server = create_sdk_mcp_server(
        "obs-agent",
        tools=[
            agent_task,
            agent_task_output,
            agent_task_stop,
            cron_create,
            cron_list,
            cron_delete,
            send_inbox_message,
            read_inbox,
            fork_task,
            fork_task_output,
            fork_task_stop,
            session_info,
            context_info,
            session_lineage,
            get_family,
        ],
    )
    return server
