"""MCP tools for OBS Agent.

Provides in-process MCP tools that the Claude Agent SDK exposes to the model.
Current task orchestration primitives: ``AgentTask`` (with ``fork`` mode),
``AgentTaskOutput``, and ``AgentTaskStop``.

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
from obs_agent.config import parse_context_suffix
from obs_agent.lineage import (
    find_latest_obs_bootstrap_for_session,
    agent_name_for_lineage,
    lineage_fingerprint,
    normalize_lineage_name,
    obs_bootstrap_to_dict,
    parse_obs_bootstrap_xml,
    slugify_projection_label,
)

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig
    from obs_agent.hooks import HookState

logger = logging.getLogger("obs_agent.tools")
_INBOX_FILE_LOCKS: dict[Path, asyncio.Lock] = {}


def _error_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _resolve_hook_spec_path(spec: str, vault_path: Path) -> str:
    file_part, function_name = spec.rsplit("::", 1)
    file_path = Path(file_part)
    if file_part.startswith("~"):
        file_path = file_path.expanduser()
    elif not file_path.is_absolute():
        file_path = vault_path / file_path
    return f"{file_path}::{function_name}"


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


def _member_activity_sort_key(member: dict[str, Any], agent_name: str) -> tuple[float, str]:
    candidates = [
        member.get("last_active_at"),
        member.get("last_activity_at"),
        member.get("completed_at"),
        member.get("created_at"),
        member.get("updated_at"),
    ]
    for candidate in candidates:
        if isinstance(candidate, (int, float)):
            return (-float(candidate), str(agent_name))
        if isinstance(candidate, str) and candidate.strip():
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                continue
            return (-parsed.timestamp(), str(agent_name))
    return (0.0, str(agent_name))


def _load_team_projection_metadata(team_name: str) -> dict[str, dict[str, Any]]:
    config_path = Path.home() / ".claude" / "teams" / team_name / "config.json"
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed reading team config JSON: %s", config_path, exc_info=True)
        return {}
    members = payload.get("members")
    if not isinstance(members, list):
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        agent_name = str(member.get("name") or "").strip()
        if not agent_name or agent_name == "team-lead":
            continue
        obs = member.get("obs")
        if not isinstance(obs, dict):
            continue
        entry: dict[str, Any] = {"agent_name": agent_name}
        display_name = obs.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            entry["display_name"] = display_name.strip()
        parent_agent_name = obs.get("parent_agent_name")
        if isinstance(parent_agent_name, str) and parent_agent_name.strip():
            entry["parent_agent_name"] = parent_agent_name.strip()
        parent_display_name = obs.get("parent_display_name")
        if isinstance(parent_display_name, str) and parent_display_name.strip():
            entry["parent_display_name"] = parent_display_name.strip()
        lineage = obs.get("lineage")
        if isinstance(lineage, list):
            normalized_lineage = [
                str(item).strip()
                for item in lineage
                if isinstance(item, str) and str(item).strip()
            ]
            if normalized_lineage:
                entry["lineage"] = normalized_lineage
                entry["lineage_length"] = len(normalized_lineage)
        metadata[agent_name] = entry
    return metadata


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


def _normalize_session_source_arg(value: object) -> str | None:
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
    ``AgentTask`` can delegate launch behavior through hook_state callbacks.
    """

    def _current_obs_bootstrap():
        if hook_state is not None and hook_state.pending_obs_bootstrap_xml:
            try:
                return parse_obs_bootstrap_xml(hook_state.pending_obs_bootstrap_xml)
            except Exception:
                logger.warning("Failed parsing pending OBS bootstrap XML", exc_info=True)
        session_id = get_session_id()
        if not session_id and hook_state is not None:
            session_id = hook_state.session_id
        result = find_latest_obs_bootstrap_for_session(
            session_id=session_id,
            cwd=config.vault_path,
        )
        return result

    async def _launch_task(
        args: dict,
        *,
        tool_name: str,
        default_fork: bool,
    ) -> dict:
        prompt = str(args.get("prompt", "")).strip()
        prompt_file = str(args.get("prompt_file", "")).strip()
        prompt_file_content = ""

        # --- prompt_file resolution ---
        if prompt_file:
            try:
                file_path = Path(prompt_file)
                if str(prompt_file).startswith("~"):
                    file_path = file_path.expanduser()
                elif not file_path.is_absolute():
                    file_path = config.vault_path / file_path
                file_content = file_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return _error_result(f"Cannot launch {tool_name}: prompt_file not found: {file_path}")
            except PermissionError:
                return _error_result(f"Cannot launch {tool_name}: permission denied reading prompt_file: {file_path}")
            except Exception as exc:
                return _error_result(f"Cannot launch {tool_name}: cannot read prompt_file: {exc}")
            prompt_file_content = file_content.strip()

        # display_name is the canonical param. alias/description/name are
        # deprecated fallbacks. This is the human-readable label, NOT the
        # machine agent_name (computed from lineage by agent_name_for_lineage).
        display_name = str(
            args.get("display_name") or args.get("alias") or args.get("description") or args.get("name") or ""
        ).strip() or None
        description = display_name  # Internal payload still uses "description" key
        resume = _normalize_resume_arg(args.get("resume"))
        session_source = _normalize_session_source_arg(args.get("session_source"))
        run_in_background = args.get("run_in_background")
        team_name = str(args.get("team_name", "")).strip() or None
        agent_name = str(args.get("agent_name") or "").strip() or None
        fork = default_fork
        if "fork" in args:
            try:
                fork = _coerce_bool_arg(args.get("fork"), name="fork")
            except ValueError:
                return _error_result(f"Cannot launch {tool_name}: fork must be true or false")
        if session_source and not fork:
            return _error_result(
                f"Cannot launch {tool_name}: session_source is only supported with fork=true"
            )
        if session_source and resume:
            return _error_result(
                f"Cannot launch {tool_name}: resume and session_source are mutually exclusive"
            )
        # Model selection — "inherit" or empty means use parent's model.
        model_raw = str(args.get("model", "")).strip()
        model: str | None = None
        if model_raw and model_raw.lower() != "inherit":
            if fork:
                return _error_result(
                    f"Cannot launch {tool_name}: cross-model forking is not supported. "
                    f"When fork=true, the model parameter must be omitted or set to 'inherit' "
                    f"(got model='{model_raw}'). Use fork=false instead to launch a fresh "
                    f"session with a different model."
                )
            model = model_raw
        # --- inherit_schedules ---
        inherit_schedules = True
        if "inherit_schedules" in args:
            try:
                inherit_schedules = _coerce_bool_arg(args.get("inherit_schedules"), name="inherit_schedules")
            except ValueError:
                return _error_result(f"Cannot launch {tool_name}: inherit_schedules must be true or false")

        # --- env passthrough ---
        env_override: dict[str, str] | None = None
        env_raw = args.get("env")
        if env_raw:
            try:
                env_override = json.loads(env_raw) if isinstance(env_raw, str) else env_raw
            except (json.JSONDecodeError, TypeError) as exc:
                return _error_result(f"Cannot launch {tool_name}: env must be a valid JSON object: {exc}")
            if not isinstance(env_override, dict):
                return _error_result(f"Cannot launch {tool_name}: env must be a JSON object (dict), got {type(env_override).__name__}")

        # --- temperature ---
        temperature: float | None = None
        temperature_raw = args.get("temperature")
        if temperature_raw is not None and str(temperature_raw).strip():
            try:
                temperature = float(temperature_raw)
            except (TypeError, ValueError):
                return _error_result(f"Cannot launch {tool_name}: temperature must be a number (e.g. '0', '0.5', '1')")
            if temperature < 0 or temperature > 2:
                return _error_result(f"Cannot launch {tool_name}: temperature must be between 0 and 2")

        # --- hooks ---
        user_hooks: dict[str, str] | None = None
        hooks_raw = args.get("hooks")
        if hooks_raw:
            try:
                user_hooks = json.loads(hooks_raw) if isinstance(hooks_raw, str) else hooks_raw
            except (json.JSONDecodeError, TypeError) as exc:
                return _error_result(f"Cannot launch {tool_name}: hooks must be a valid JSON object: {exc}")
            if not isinstance(user_hooks, dict):
                return _error_result(
                    f"Cannot launch {tool_name}: hooks must be a JSON object, got {type(user_hooks).__name__}"
                )
            resolved_hooks: dict[str, str] = {}
            for event_name, spec in user_hooks.items():
                if not isinstance(spec, str) or "::" not in spec:
                    return _error_result(
                        f"Cannot launch {tool_name}: hooks['{event_name}'] must be "
                        f"'file_path::function_name', got: {spec!r}"
                    )
                resolved_hooks[str(event_name)] = _resolve_hook_spec_path(spec, config.vault_path)
            user_hooks = resolved_hooks

        # --- inherit_hooks ---
        inherit_hooks = False
        if "inherit_hooks" in args:
            try:
                inherit_hooks = _coerce_bool_arg(args.get("inherit_hooks"), name="inherit_hooks")
            except ValueError:
                return _error_result(f"Cannot launch {tool_name}: inherit_hooks must be true or false")

        # Kept for internal/future use — not exposed in MCP schema but still processed if passed
        timeout_ms_raw = args.get("timeout_ms")
        max_turns_raw = args.get("max_turns")
        if not prompt and not prompt_file_content:
            return _error_result(f"Cannot launch {tool_name}: prompt or prompt_file is required")

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
            payload: dict[str, Any] = {
                "session_id": effective_session_id,
                "prompt": prompt,
                "description": description,
                "resume": resume,
                "session_source": session_source,
                "run_in_background": True,
                "timeout_ms": timeout_ms,
                "max_turns": max_turns,
                "fork": fork,
                "model": model,
                "team_name": team_name,
                "agent_name": agent_name,
                "task_tool_name": tool_name,
                "tool_use_id": hook_state.current_tool_use_id,
                "inherit_schedules": inherit_schedules,
                "env": env_override,
                "temperature": temperature,
                "hooks": user_hooks,
                "inherit_hooks": inherit_hooks,
            }
            if prompt_file:
                payload["prompt_file"] = prompt_file
                payload["prompt_file_content"] = prompt_file_content
            return await hook_state.fork_task_launcher(payload)
        except Exception as exc:
            logger.exception("%s launch failed", tool_name)
            return _error_result(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    @tool(
        "AgentTask",
        "Launch a delegated child agent in a new Telegram topic. "
        "Set fork=true to continue from current session head, or fork=false to start "
        "a fresh child session in the new topic.",
        {
            "type": "object",
            "properties": {
            "prompt": {
                "type": "string",
                "description": "Full task prompt for the child session. May be combined with prompt_file.",
            },
            "prompt_file": {
                "type": "string",
                "description": (
                    "Path to a file containing additional prompt context. Vault-relative by default, "
                    "absolute paths (/...) and ~/paths supported. May be combined with prompt."
                ),
            },
            "display_name": {
                "type": "string",
                "description": "Human-readable name for the child agent (used in topic titles and lineage).",
            },
            "alias": {
                "type": "string",
                "description": "Deprecated: use display_name.",
            },
            "description": {
                "type": "string",
                "description": "Deprecated: use display_name.",
            },
            "resume": {
                "type": "string",
                "description": "Optional agentId to resume an existing child task",
            },
            "session_source": {
                "type": "string",
                "description": (
                    "Optional Claude session source to fork from when fork=true. "
                    "Accepts a stored session ID or JSONL file path. Not supported with fork=false."
                ),
            },
            "fork": {
                "type": "boolean",
                "description": "When true, fork from parent head; when false, start fresh",
            },
            # run_in_background: hardcoded true, not exposed in MCP schema
            # timeout_ms and max_turns: kept for internal/future use — not exposed in MCP schema
            "model": {
                "type": "string",
                "description": (
                    "Model for the child session. Accepts shorthands "
                    "(sol, claude, gpt, gemini) which resolve to the latest tier, local aliases "
                    "(local-qwen3.5-27b, local-gemma4-31b), or full names "
                    "(gpt-5.6-sol, gemini-2.5-pro). "
                    "Append a context suffix like [1m] or [200k] to control the context window "
                    "(default: model-specific; local aliases default to their deployed 32K limit). "
                    "Local aliases require fork=false and a compact project context: the child still "
                    "loads the daemon's configured project/entry context, and prompts that exceed the "
                    "local 32K window fail loudly rather than falling back. 'inherit' or omitted = use "
                    "the same model as the current session. When fork=true, the model must be omitted "
                    "or set to 'inherit' — cross-model forking is not supported because the forked "
                    "JSONL contains conversation turns from the parent's model format. Use fork=false "
                    "instead to launch a fresh session with a different model."
                ),
            },
            "name": {
                "type": "string",
                "description": "Deprecated: use display_name.",
            },
            "team_name": {
                "type": "string",
                "description": "Optional team/task-list name for flat team env bootstrap.",
            },
            "inherit_schedules": {
                "type": "string",
                "description": (
                    "Whether the child agent inherits the parent's schedules. "
                    "Default 'true'. Set to 'false' to prevent schedule inheritance."
                ),
            },
            "env": {
                "type": "string",
                "description": (
                    "JSON object of environment variable overrides for the child session. "
                    "These are merged into the child's SDK env overrides, e.g. "
                    '\'{"SOME_VAR": "value"}\'. User-provided env vars take precedence '
                    "over auto-configured ones, including ANTHROPIC_BASE_URL when the daemon's "
                    "cache proxy is enabled, so a single fresh child can target a local provider "
                    "without restarting or reconfiguring the parent daemon."
                ),
            },
            "temperature": {
                "type": "string",
                "description": (
                    "Temperature for the child session (e.g. '0', '0.5', '1'). "
                    "WARNING: setting temperature requires disabling extended thinking — "
                    "the Anthropic API rejects temperature != 1 when thinking is enabled. "
                    "This trades reasoning quality for determinism. If both temperature "
                    "and env.CLAUDE_CODE_EXTRA_BODY are provided, temperature takes precedence."
                ),
            },
            "hooks": {
                "type": "string",
                "description": (
                    "JSON object mapping hook event names to Python function specs. "
                    "Each value must be 'file_path::function_name'; relative paths are resolved "
                    "against the vault path. The function is dynamically loaded and called with "
                    "(hook_input, tool_use_id, context). Hook functions run AFTER built-in OBS guards. Errors in hooks are "
                    "logged but never crash the session. "
                    'Example: \'{"PreToolUse": "procedures/hooks/guard.py::check_access"}\''
                ),
            },
            "inherit_hooks": {
                "type": "string",
                "description": (
                    "Whether the child agent inherits its parent's hooks. "
                    "Default 'false'. Set to 'true' to propagate the parent session's "
                    "hooks to the child."
                ),
            },
            },
            "required": ["display_name"],
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
        "AgentTaskOutput",
        "Inspect a running or completed AgentTask using TaskOutput-style "
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
        "AgentTaskStop",
        "Stop a running AgentTask using TaskStop-style task_id input.",
        {
            "task_id": {
                "type": "string",
                "description": "The agentId/task handle returned by AgentTask",
            },
            "shell_id": {
                "type": "string",
                "description": "Deprecated alias for task_id, matching TaskStop.",
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
        return await _cron_delete(args, tool_name="CronDelete")

    async def _send_inbox_message(args: dict) -> dict:
        # On delivery failure, return "message underdelivered" and use _rollback_written_message after writes.
        bootstrap = _current_obs_bootstrap()
        team_name = str(args.get("team_name", "")).strip() or (
            bootstrap.root_team_key if bootstrap is not None else ""
        )
        recipient = str(args.get("recipient", "")).strip()
        content = str(args.get("content", "")).strip()
        summary = str(args.get("summary", "")).strip() or None
        sender = str(args.get("sender", "")).strip() or (
            bootstrap.agent_name if bootstrap is not None and bootstrap.agent_name else "obs-worker"
        )
        # needs_reply is the canonical name; must_reply accepted for backward compat.
        # Use _coerce_bool_arg to handle string "false" correctly (bool("false") is True).
        if "needs_reply" in args:
            needs_reply_raw = args.get("needs_reply")
        else:
            needs_reply_raw = args.get("must_reply", False)
        try:
            must_reply = _coerce_bool_arg(needs_reply_raw, name="needs_reply")
        except ValueError:
            must_reply = False
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

        async def _validate_recipient(target: str) -> dict[str, Any] | None:
            if hook_state is None or hook_state.inbox_recipient_validator is None:
                return None
            try:
                validation = await hook_state.inbox_recipient_validator(
                    {
                        "team_name": team_name,
                        "recipient": target,
                    }
                )
            except Exception:
                logger.warning("SendInboxMessage validator failed", exc_info=True)
                return {"deliverable": False, "reason": "recipient validation failed"}
            if isinstance(validation, dict):
                return validation
            return {"deliverable": bool(validation)}

        def _is_deliverable(validation: dict[str, Any] | None) -> bool:
            return bool(validation and validation.get("deliverable"))

        inbox_dir = Path.home() / ".claude" / "teams" / team_name / "inboxes"
        resolved_recipient = recipient
        inbox_path = inbox_dir / f"{resolved_recipient}.json"
        validation = await _validate_recipient(resolved_recipient)

        # Alias resolution is intentionally limited to direct children:
        # exact agent_name wins, otherwise try "{sender_lineage_hash}-{alias_slug}".
        if (
            not inbox_path.exists()
            and not _is_deliverable(validation)
            and bootstrap is not None
            and bootstrap.lineage
        ):
            child_hash = lineage_fingerprint(
                tuple(normalize_lineage_name(n) for n in bootstrap.lineage)
            )
            child_slug = slugify_projection_label(recipient, fallback="")
            if child_slug:
                candidate = f"{child_hash}-{child_slug}"
                candidate_path = inbox_dir / f"{candidate}.json"
                candidate_validation = await _validate_recipient(candidate)
                if candidate_path.exists() or _is_deliverable(candidate_validation):
                    resolved_recipient = candidate
                    inbox_path = candidate_path
                    validation = candidate_validation

        if validation is not None and not _is_deliverable(validation):
            reason = str(validation.get("reason") or validation.get("warning") or "recipient has no current route binding")
            return {
                "content": [{"type": "text", "text": f"message underdelivered: {reason}"}],
                "tool_use_result": {
                    "success": False,
                    "delivered": False,
                    "outcome": "underdelivered",
                    "reason": reason,
                    "recipient": resolved_recipient,
                    "team_name": team_name,
                },
                "is_error": True,
            }
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

        async def _rollback_written_message() -> None:
            async with lock:
                if not inbox_path.exists():
                    return
                try:
                    loaded = json.loads(inbox_path.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning("Failed reading inbox JSON during rollback: %s", inbox_path, exc_info=True)
                    return
                if not isinstance(loaded, list):
                    return
                remaining = [item for item in loaded if isinstance(item, dict)]
                removed = False
                for index in range(len(remaining) - 1, -1, -1):
                    item = remaining[index]
                    if (
                        item.get("timestamp") == message["timestamp"]
                        and item.get("from") == sender
                        and item.get("text") == content
                        and item.get("summary", "") == (summary or "")
                    ):
                        del remaining[index]
                        removed = True
                        break
                if not removed:
                    return
                if remaining:
                    inbox_path.write_text(json.dumps(remaining, ensure_ascii=True), encoding="utf-8")
                else:
                    inbox_path.unlink(missing_ok=True)

        response = {
            "success": True,
            "delivered": True,
            "outcome": "reached",
            "team_name": team_name,
            "recipient": resolved_recipient,
            "message_count": len(entries),
        }
        if validation is not None and validation.get("warning"):
            response["warning"] = str(validation["warning"])
        notifier_result: dict[str, Any] | None = None
        if hook_state is not None and hook_state.inbox_message_notifier is not None:
            try:
                notification_payload: dict[str, Any] = {
                    "team_name": team_name,
                    "recipient": resolved_recipient,
                    "sender": sender,
                    "content": content,
                    "summary": summary,
                    "_direct_send": True,
                }
                if must_reply:
                    notification_payload["_must_reply"] = True
                raw_notifier_result = await hook_state.inbox_message_notifier(notification_payload)
                if isinstance(raw_notifier_result, dict):
                    notifier_result = raw_notifier_result
            except Exception:
                logger.warning("SendInboxMessage notifier failed", exc_info=True)
        if notifier_result is not None and notifier_result.get("delivered") is False:
            await _rollback_written_message()
            reason = str(notifier_result.get("reason") or "recipient wake failed")
            return {
                "content": [{"type": "text", "text": f"message underdelivered: {reason}"}],
                "tool_use_result": {
                    "success": False,
                    "delivered": False,
                    "outcome": "underdelivered",
                    "reason": reason,
                    "recipient": resolved_recipient,
                    "team_name": team_name,
                },
                "is_error": True,
            }

        # Reply detection: if we just sent a message to `recipient`, check our OWN
        # inbox for must_reply messages FROM that recipient that are unreplied.
        # If found, mark them replied.  When ALL must_reply messages are replied,
        # delete the reply_wake schedule.
        if sender and sender != resolved_recipient:
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
                                sender_entries, resolved_recipient
                            )
                            replied_something = any(
                                e.get("replied") is True
                                for e in updated
                                if e.get("must_reply") is True and e.get("from") == resolved_recipient
                            )
                            if replied_something:
                                sender_inbox_path.write_text(
                                    json.dumps(updated, ensure_ascii=True),
                                    encoding="utf-8",
                                )
                            if all_replied and hook_state is not None:
                                if hook_state.inbox_message_notifier is not None:
                                    try:
                                        await hook_state.inbox_message_notifier(
                                            {
                                                "team_name": team_name,
                                                "recipient": sender,
                                                "sender": resolved_recipient,
                                                "content": "__reply_wake_clear__",
                                                "summary": "all must_reply messages replied",
                                                "_reply_wake_clear": True,
                                                "_direct_send": True,
                                            }
                                        )
                                    except Exception:
                                        logger.warning("Reply wake clear notification failed", exc_info=True)
                    except Exception:
                        logger.warning("Reply detection: failed reading sender inbox %s", sender_inbox_path, exc_info=True)
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
            bootstrap.agent_name if bootstrap is not None else ""
        )
        if not team_name:
            return _error_result(
                "Cannot use ReadInbox: team_name is required or must be inferable from current lineage"
            )
        if not agent:
            return _error_result(
                "Cannot use ReadInbox: agent is required or must be inferable from current lineage"
            )
        include_read = _coerce_bool_arg(args.get("include_read", False), name="include_read")
        mark_read = _coerce_bool_arg(args.get("mark_read", True), name="mark_read")
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
                for item in selected:
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
        "Write a message to a team inbox JSON file.",
        {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Optional team name; defaults to the current root team.",
                },
                "recipient": {
                    "type": "string",
                    "description": "Recipient agent name.",
                },
                "content": {
                    "type": "string",
                    "description": "Message body.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional short summary.",
                },
                "sender": {
                    "type": "string",
                    "description": "Optional sender label; defaults to the current agent name.",
                },
                "needs_reply": {
                    "type": "boolean",
                    "description": "Set true only when the message asks a question or makes a request that needs a reply.",
                },
                "must_reply": {
                    "type": "boolean",
                    "description": "Deprecated alias for needs_reply.",
                },
            },
            "required": ["recipient", "content"],
            "additionalProperties": False,
        },
    )
    async def send_inbox_message(args: dict) -> dict:
        return await _send_inbox_message(args)

    @tool(
        "ReadInbox",
        "Read messages from a team inbox JSON file.",
        {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Optional team name; defaults to the current root team.",
                },
                "agent": {
                    "type": "string",
                    "description": "Optional agent inbox name; defaults to the current agent name.",
                },
                "include_read": {
                    "type": "boolean",
                    "description": "Include already-read messages.",
                },
                "mark_read": {
                    "type": "boolean",
                    "description": "Mark returned unread messages as read.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    async def read_inbox(args: dict) -> dict:
        return await _read_inbox(args)

    def _effective_context_window_tokens() -> int:
        if hook_state is not None and hook_state.effective_model:
            _clean, tokens = parse_context_suffix(hook_state.effective_model)
            return tokens
        return config.context_window_estimate_tokens

    async def _render_context_and_session() -> str:
        data = hook_state.last_result_data if hook_state is not None else None
        snapshot = build_context_snapshot(
            session_id=get_session_id(),
            data=data,
            context_window_estimate_tokens=_effective_context_window_tokens(),
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
        include_xml = _coerce_bool_arg(args.get("include_xml", False), name="include_xml")
        bootstrap = _current_obs_bootstrap()
        if bootstrap is None:
            return _error_result("Cannot use session_lineage: no OBS bootstrap found for current session")
        payload = obs_bootstrap_to_dict(
            bootstrap,
            session_id=get_session_id(),
            include_xml=include_xml,
        )
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}],
            "tool_use_result": payload,
        }

    @tool(
        "search_team",
        "Discover teammates in the current lineage tree by scanning team inboxes.",
        {
            "mode": {
                "type": "string",
                "description": "One of: parent, children, siblings, ancestors, descendants, family, tree",
            },
        },
    )
    async def search_team(args: dict) -> dict:
        mode = str(args.get("mode") or args.get("who") or "family").strip().lower()
        if mode == "all":
            mode = "family"
        if mode == "tree_children":
            mode = "children"
        if mode not in {"children", "siblings", "parent", "ancestors", "descendants", "family", "tree"}:
            return _error_result(
                "search_team: 'mode' must be one of: parent, children, siblings, ancestors, descendants, family, tree, tree_children"
            )
        bootstrap = _current_obs_bootstrap()
        if bootstrap is None:
            return _error_result("Cannot use search_team: no OBS bootstrap found")
        if not bootstrap.root_team_key or not bootstrap.agent_name:
            return _error_result("Cannot use search_team: missing team key or agent name")
        if not bootstrap.lineage:
            return _error_result("Cannot use search_team: empty lineage")

        inboxes_dir = (
            Path.home()
            / ".claude"
            / "teams"
            / bootstrap.root_team_key
            / "inboxes"
        )
        team_projection_metadata = _load_team_projection_metadata(bootstrap.root_team_key)
        result: dict[str, Any] = {
            "mode": mode,
            "team_name": bootstrap.root_team_key,
            "current_agent": bootstrap.agent_name,
        }
        all_agents = []
        if inboxes_dir.is_dir():
            for f in inboxes_dir.iterdir():
                if f.suffix == ".json" and f.stem:
                    all_agents.append(f.stem)
        all_agents = sorted(set(all_agents))

        my_lineage = bootstrap.lineage
        my_agent_name = bootstrap.agent_name
        my_hash = lineage_fingerprint(tuple(normalize_lineage_name(n) for n in my_lineage))

        def _sort_member_names(names: list[str]) -> list[str]:
            return sorted(
                names,
                key=lambda name: _member_activity_sort_key(
                    team_projection_metadata.get(name) or {},
                    name,
                ),
            )

        children = _sort_member_names([
            name for name in all_agents
            if name.startswith(f"{my_hash}-") and name != my_agent_name
        ])
        if mode in {"children", "family"}:
            result["children"] = children

        parent: str | None = None
        if mode in {"parent", "family"}:
            if len(my_lineage) > 1:
                parent_name = bootstrap.parent_agent_name
                if not parent_name:
                    parent_lineage = my_lineage[:-1]
                    parent_name = agent_name_for_lineage(
                        parent_lineage,
                        team_key=bootstrap.root_team_key,
                    )
                if parent_name in all_agents:
                    parent = parent_name
            result["parent"] = parent

        siblings: list[str] = []
        if len(my_lineage) > 1:
            parent_lineage = tuple(normalize_lineage_name(n) for n in my_lineage[:-1])
            parent_hash = lineage_fingerprint(parent_lineage)
            siblings = _sort_member_names([
                name for name in all_agents
                if name.startswith(f"{parent_hash}-") and name != my_agent_name
            ])
        if mode in {"siblings", "family"}:
            result["siblings"] = siblings

        if mode == "ancestors":
            ancestors = [
                agent_name_for_lineage(
                    my_lineage[: idx + 1],
                    team_key=bootstrap.root_team_key,
                )
                for idx in range(len(my_lineage) - 1)
            ]
            result["ancestors"] = ancestors

        if mode == "descendants":
            descendants: list[str] = []
            for agent_name, details in team_projection_metadata.items():
                lineage = details.get("lineage")
                if not isinstance(lineage, list):
                    continue
                normalized_lineage = tuple(
                    str(item).strip()
                    for item in lineage
                    if isinstance(item, str) and str(item).strip()
                )
                if len(normalized_lineage) <= len(my_lineage):
                    continue
                if normalized_lineage[: len(my_lineage)] == my_lineage:
                    descendants.append(agent_name)
            result["descendants"] = _sort_member_names(list(set(descendants)))

        if mode == "tree":
            result["tree"] = all_agents
            child_set = set(children)
            sibling_set = set(siblings)
            tree_members: list[dict[str, Any]] = []
            for agent_name in all_agents:
                details = dict(team_projection_metadata.get(agent_name) or {})
                details["agent_name"] = agent_name
                if agent_name == my_agent_name:
                    details["relation"] = "self"
                elif parent is not None and agent_name == parent:
                    details["relation"] = "parent"
                elif agent_name in child_set:
                    details["relation"] = "child"
                elif agent_name in sibling_set:
                    details["relation"] = "sibling"
                else:
                    details["relation"] = "tree"
                tree_members.append(details)
            tree_members.sort(
                key=lambda item: _member_activity_sort_key(
                    item,
                    str(item.get("agent_name") or ""),
                )
            )
            result["tree_members"] = tree_members

        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
            "tool_use_result": result,
        }

    @tool(
        "PlaceholderTool",
        "A placeholder tool whose behavior is defined by hooks. "
        "Call this tool when instructed by the system or a procedure. "
        "A pre-tool-use hook intercepts the call and provides the actual implementation.",
        {
            "action": {
                "type": "string",
                "description": "The action to perform.",
            },
            "input": {
                "type": "string",
                "description": "Free-form input for the action.",
            },
        },
    )
    async def placeholder_tool(args: dict) -> dict:
        action = str(args.get("action", "")).strip()
        tool_input = str(args.get("input", "")).strip()
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"PlaceholderTool executed: action={action!r}, input={tool_input!r}",
                }
            ],
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
            session_info,
            context_info,
            session_lineage,
            search_team,
            placeholder_tool,
        ],
    )
    return server
