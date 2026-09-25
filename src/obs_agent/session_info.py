"""Read-only agent/session projection shared by the human-facing transports.

This is not another persistence model. Inputs remain owned by SessionManager,
HookState, the transport, and the existing JSONL/bootstrap readers. In particular,
inspection must never create SDK options (which load hooks and mutate state).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import ParseError

from obs_agent.config import is_claude_model, resolve_model_context
from obs_agent.context_jsonl import find_session_jsonl
from obs_agent.context_stats import build_context_snapshot, format_context_snapshot_compact
from obs_agent.lineage import resolve_obs_bootstrap

if TYPE_CHECKING:
    from obs_agent.session import SessionManager

# Kept explicit so inspection never imports or executes user hook code. A
# regression test compares this inventory with create_hook_matchers().
BUILTIN_HOOK_EVENTS = (
    "PreToolUse", "PostToolUse", "Notification", "SubagentStart", "SubagentStop", "Stop",
    "PreCompact",
)


@dataclass(frozen=True)
class SessionViewContext:
    """Transport-owned metadata, excluding messages, prompts and credentials."""

    transport: str = "CLI / daemon"
    topic_title: str | None = None
    lineage: tuple[str, ...] = ()
    pending_bootstrap: str | None = None
    team_name: str | None = None
    agent_name: str | None = None
    task_id: str | None = None
    task_status: str | None = None
    origin: str | None = None
    is_fork: bool | None = None
    parent_session_id: str | None = None
    parent_source_uuid: str | None = None
    head_uuid: str | None = None
    chat_id: int | None = None
    thread_id: int | None = None
    busy: bool = False
    pending_messages: int = 0
    active_children: int = 0
    schedule_count: int = 0
    notify_on_completion: bool | None = None
    inbox_wake_pending: bool = False
    state_db_path: str | None = None
    timeout_ms: int | None = None
    max_turns: int | None = None


def session_context_window(manager: SessionManager, fallback_tokens: int) -> int:
    """Use the running model budget, or the selection before the first SDK turn.

    A restored/cold model override must not fall back to the global model's
    window. This does not change how occupied tokens are measured.
    """
    return resolve_model_context(
        manager.hook_state.effective_model or manager.effective_model,
        default_context_tokens=fallback_tokens,
    ).context_tokens


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")


def build_session_info(
    manager: SessionManager, *, view: SessionViewContext | None = None,
) -> dict[str, Any]:
    """Project safe metadata without connecting, probing, persisting or loading hooks.

    Environment values and raw settings/extra-body mappings are intentionally
    absent even from the structured result, not just hidden by the formatter.
    Hook specifications describe configuration, not confirmed loading/execution.
    """
    view = view or SessionViewContext()
    config, state = manager.config, manager.hook_state
    warnings: list[str] = []
    bootstrap = None
    try:
        bootstrap = resolve_obs_bootstrap(
            pending_xml=view.pending_bootstrap or state.pending_obs_bootstrap_xml,
            session_id=manager.session_id, cwd=config.vault_path,
        )
    except (OSError, UnicodeError, ValueError, ParseError):
        warnings.append("Stored lineage is unreadable; showing available runtime metadata.")

    lineage = view.lineage or (bootstrap.lineage if bootstrap else ())
    # Only these identity fields may be projected from environment configuration.
    # Never generate timestamp-based names merely because somebody asked for info.
    env = manager.sdk_env_overrides
    team = (bootstrap.root_team_key if bootstrap else None) or view.team_name or env.get("CLAUDE_CODE_TEAM_NAME")
    name = (bootstrap.agent_name if bootstrap else None) or view.agent_name or env.get("CLAUDE_CODE_AGENT_NAME")
    model = resolve_model_context(manager.effective_model)
    budget = session_context_window(manager, config.context_window_estimate_tokens)
    result_data = state.last_result_data
    # A reset/recovery can leave an old result briefly available. Never join its
    # transcript to a different session ID (or to a not-yet-started session).
    if result_data and result_data.get("session_id") not in (None, manager.session_id):
        result_data = None
    try:
        snapshot = build_context_snapshot(
            session_id=manager.session_id, data=result_data,
            context_window_estimate_tokens=budget, cwd=config.vault_path,
        )
    except (OSError, UnicodeError):
        warnings.append("Transcript usage is unreadable; using available result usage.")
        snapshot = build_context_snapshot(
            session_id=None,
            data={key: value for key, value in (result_data or {}).items() if key != "session_id"},
            context_window_estimate_tokens=budget, cwd=config.vault_path,
        )
    transcript = snapshot["jsonl_session_file"]
    if transcript is None and manager.session_id:
        try:
            found = find_session_jsonl(session_id=manager.session_id, cwd=config.vault_path)
            transcript = str(found) if found is not None else None
        except OSError:
            warnings.append("Session transcript is unavailable.")
    available = bool(snapshot["jsonl_session_file"] or (result_data or {}).get("usage"))
    try:
        effort = manager.effective_effort
    except ValueError:
        # Do not echo malformed environment values in a diagnostic error.
        effort = "unavailable (invalid effort configuration)"
    return {
        "agent": {
            "display_name": lineage[-1] if lineage else view.topic_title,
            "agent_name": name, "team_name": team, "lineage": list(lineage),
            "parent_agent_name": bootstrap.parent_agent_name if bootstrap else None,
            "parent_display_name": bootstrap.parent_display_name if bootstrap else None,
            "agent_id": bootstrap.agent_id if bootstrap else None,
            "task_id": view.task_id, "task_status": view.task_status,
            "origin": bootstrap.origin if bootstrap else view.origin,
            "is_fork": bootstrap.is_fork if bootstrap else view.is_fork,
            "topic_title": view.topic_title,
        },
        "session": {
            "session_id": manager.session_id, "head_uuid": view.head_uuid,
            "parent_session_id": view.parent_session_id or (bootstrap.parent_session_id if bootstrap else None),
            "parent_source_uuid": view.parent_source_uuid,
            "connected": manager.has_connected_client(),
            "last_activity_utc": _timestamp(manager.last_activity),
            "resumable": manager.should_resume(),
            "transport": view.transport, "chat_id": view.chat_id, "thread_id": view.thread_id,
        },
        "model": {
            "selection": manager.effective_model, "resolved_name": model.model,
            "selection_source": "session override" if manager.model_override else "OBS configuration",
            "context_window_tokens": budget, "effort": effort,
            "effort_override": manager.effort_override,
            "provider_family": "local" if model.model.startswith("local-") else (
                "Claude" if is_claude_model(model.model) else "proxy-compatible"
            ),
        },
        "context": {
            "available": available,
            "used_tokens": snapshot["estimated_context_used_tokens"] if available else None,
            "window_tokens": budget,
            "summary": format_context_snapshot_compact(snapshot) if available else (
                f"context: unavailable / {budget:,} tokens (no usage yet)"
            ),
        },
        "files": {
            "jsonl_session_file": transcript,
            "lineage_storage": "<obs-bootstrap> in the session JSONL; runtime/persisted route metadata",
            "state_db": view.state_db_path,
            "working_directory": str(config.vault_path),
            "entry_file": str(config.context_path),
            "project_settings": str(config.claude_path / "settings.json"),
        },
        "runtime": {
            "busy": view.busy or state.execution_active,
            "pending_messages": view.pending_messages, "hook_queue_messages": state.message_queue.qsize(),
            "active_children": view.active_children,
            "background_tasks": len(state.background_tasks),
            "current_tool_use_id": state.current_tool_use_id,
            "interrupt_requested": state.interrupt_requested or state.interrupt_flag,
            "inbox_wake_pending": view.inbox_wake_pending,
            "schedule_count": view.schedule_count, "schedule_run_active": state.schedule_run_active,
            "triggered_schedule_id": state.triggered_schedule_id,
            "notify_on_completion": view.notify_on_completion,
            "cache_window_seconds": config.cache_window_seconds,
            "max_queue_continuations": config.max_queue_continuations,
            "max_buffer_size_bytes": config.max_buffer_size,
            "timeout_ms": view.timeout_ms, "max_turns": view.max_turns,
            "permission_mode": "bypassPermissions", "setting_sources": ["project"],
            "system_prompt": "claude_code preset + persisted entry-file context",
            "mcp_servers": ["obs-agent"],
        },
        "hooks": {
            "builtin_events": list(BUILTIN_HOOK_EVENTS),
            "configured": dict(manager.user_hooks or {}),
            "unsupported_events": sorted(set(manager.user_hooks or {}) - set(BUILTIN_HOOK_EVENTS)),
        },
        "environment": {"session_override_count": len(env), "values_disclosed": False},
        "warnings": warnings,
    }


def _text(value: Any) -> str:
    """Keep metadata on one line, rendered as plain text by every transport."""
    if value is None or value == "":
        return "(none)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return " ".join(str(value).split())


def format_session_info_lines(info: dict[str, Any]) -> list[str]:
    """Format the projection with exactly one context-occupancy line."""
    agent, session, model = info["agent"], info["session"], info["model"]
    files, runtime, hooks = info["files"], info["runtime"], info["hooks"]
    lines = ["Agent"]

    def row(label: str, value: Any) -> None:
        lines.append(f"{label}: {_text(value)}")

    for label in ("display_name", "agent_name", "team_name"):
        row(label, agent[label])
    row("lineage", " → ".join(agent["lineage"]))
    for label in ("topic_title", "parent_agent_name", "parent_display_name", "agent_id", "task_id", "task_status", "origin", "is_fork"):
        if agent[label] is not None:
            row(label, agent[label])
    lines.extend(["", "Session"])
    row("session_id", session["session_id"])
    for label in ("head_uuid", "parent_session_id", "parent_source_uuid"):
        if session[label] is not None:
            row(label, session[label])
    row("transport", session["transport"])
    if session["chat_id"] is not None:
        row("route", f"chat {session['chat_id']}, thread {session['thread_id'] if session['thread_id'] is not None else '(DM)'}")
    row("client_connected", session["connected"])
    row("resumable", session["resumable"])
    row("last_activity_utc", session["last_activity_utc"])
    lines.extend(["", "Model & context"])
    row("model", f"{model['resolved_name']} ({model['selection_source']})")
    row("selection", model["selection"])
    row("provider_family", model["provider_family"])
    row("effort", model["effort"])
    if model["effort_override"] is not None:
        row("effort_selection", model["effort_override"])
    lines.append(info["context"]["summary"])
    lines.extend(["", "Files"])
    row("jsonl_session_file", files["jsonl_session_file"] or "not found / not created yet")
    row("transcript / lineage", "same JSONL; lineage is <obs-bootstrap>, not a separate file")
    for label in ("state_db", "working_directory", "entry_file", "project_settings"):
        if files[label] is not None:
            row(label, files[label])
    lines.extend(["", "Runtime & parameters"])
    row("status", "working" if runtime["busy"] else "idle")
    row("queued_messages", f"{runtime['pending_messages']} pending; {runtime['hook_queue_messages']} at hooks")
    row("children / background_tasks", f"{runtime['active_children']} / {runtime['background_tasks']}")
    row("schedules", f"{runtime['schedule_count']} attached; running: {_text(runtime['schedule_run_active'])}")
    for label in ("current_tool_use_id", "triggered_schedule_id", "timeout_ms", "max_turns", "notify_on_completion"):
        if runtime[label] is not None:
            row(label, runtime[label])
    if runtime["interrupt_requested"]:
        row("interrupt_requested", True)
    if runtime["inbox_wake_pending"]:
        row("inbox_wake_pending", True)
    row("cache_window_seconds", runtime["cache_window_seconds"])
    row("queue_continuations / buffer_bytes", f"{runtime['max_queue_continuations']} / {runtime['max_buffer_size_bytes']}")
    row("SDK", "bypassPermissions; project settings; claude_code preset; MCP: obs-agent")
    row("environment", f"{info['environment']['session_override_count']} session overrides; values hidden (including credentials/provider body)")
    lines.extend(["", "Hooks"])
    row("built-in events", ", ".join(hooks["builtin_events"]))
    if hooks["configured"]:
        for event, spec in sorted(hooks["configured"].items()):
            row(f"configured {event}", spec)
        lines.append("Configured hooks are not proof of successful loading; inspection does not execute them.")
    else:
        row("configured user hooks", "none")
    if hooks["unsupported_events"]:
        row("unsupported hook events (not wired)", ", ".join(hooks["unsupported_events"]))
    for warning in info["warnings"]:
        row("note", warning)
    return lines
