"""OBS event tap for the E2E harness (obs-22 contract C7, design-v3 §2.3, ADR-0018).

When ``OBS_E2E_EVENT_LOG=<path>`` is set, OBS appends one JSON object per line
to that file for each event below. The E2E harness (``tests/e2e/harness``) tails
the file to wake tests the moment something happens, instead of sleeping.
Production never sets the variable, so there the tap is a no-op: no file I/O,
and lazy fields (callables) are never evaluated.

Kinds and their extra fields (every line also has ``ts``, ``kind``, ``pid``,
``seq`` and, where known, ``agent_name``, ``team``, ``route``, ``chat_id``,
``topic_id``, ``session_id``):

- ``turn_start``: ``trigger`` (message|schedule|inbox|other), ``trigger_message_id``,
  ``schedule_id``, ``text_len``, ``text_sha``
- ``turn_end``: the same trigger fields plus ``subtype`` (the CLI result subtype
  when known, else success|error|interrupted), ``failed``, ``turns``, ``cli_runtime``,
  ``cost_usd`` (the CLI's reported total, when present)
- ``telegram_send``: ``message_id``, ``reply_to_message_id``, ``text_len``, ``text_sha``,
  ``text_head``
- ``schedule_fired``: ``schedule_id``, ``trigger_kind``
- ``inbox_delivered``: ``mode`` (wake|queued|post_stop), ``sender``
- ``session_build``: ``runtime``, ``version``, ``credential_profile``, ``source``
  (fresh|resume), ``model``, ``cli_path``
- ``stop_cleanup``: ``outcome`` (turn_ended|disconnected), ``owned``, ``survivors``
- ``recovery``: ``source``, ``outcome`` (forked|failed), ``old_session_id``, ``reason``

INVARIANT(protocol): the tap only observes. It never raises into its caller, never
blocks, never changes OBS state or any request byte, and does nothing at all when
OBS_E2E_EVENT_LOG is unset. On a write error it drops the event and logs one
"[event_tap] degraded" warning; it retries opening the file at most every
5 seconds. Each line is one O_APPEND write() under 4 KB, so concurrent writers
never interleave. Breaking it makes a harness defect take down agent turns,
schedules or launches (vm-schedules-6), or silently re-bills conversations if a
field read ever mutated session state (the tap sits on cache-sensitive paths).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Events Wait Layer#C7 event schema
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("obs_agent.event_tap")

ENV_VAR = "OBS_E2E_EVENT_LOG"
MAX_LINE_BYTES = 4000
_RETRY_OPEN_SECONDS = 5.0

_lock = threading.Lock()
_path: str | None = None
_resolved = False
_fd: int | None = None
_next_open_at = 0.0
_degraded_logged = False
_seq = 0
dropped = 0


def enabled() -> bool:
    """True when the tap writes events (OBS_E2E_EVENT_LOG set at first use)."""
    global _path, _resolved
    if not _resolved:
        _path = (os.environ.get(ENV_VAR) or "").strip() or None
        _resolved = True
    return _path is not None


def _degrade(exc: BaseException) -> None:
    global dropped, _degraded_logged
    dropped += 1
    if not _degraded_logged:
        _degraded_logged = True
        try:
            logger.warning(
                "[event_tap] degraded: %s: %s; dropping E2E events (path=%s)",
                type(exc).__name__, exc, _path,
            )
        except Exception:
            pass


def _open_locked() -> int | None:
    global _fd, _next_open_at
    if _fd is not None:
        return _fd
    now = time.monotonic()
    if now < _next_open_at:
        return None
    _next_open_at = now + _RETRY_OPEN_SECONDS
    _fd = os.open(_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o644)
    return _fd


def _encode(record: dict[str, Any]) -> bytes:
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    data = line.encode("utf-8")
    if len(data) <= MAX_LINE_BYTES:
        return data
    for key, value in list(record.items()):
        if isinstance(value, str) and len(value) > 64:
            record[key] = value[:64]
    record["truncated"] = True
    data = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8")
    if len(data) <= MAX_LINE_BYTES:
        return data
    core = ("ts", "kind", "pid", "seq", "agent_name", "team", "route", "session_id", "truncated")
    slim = {key: record[key] for key in core if key in record}
    return (json.dumps(slim, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def emit(kind: str, **fields: Any) -> None:
    """Append one event; a no-op when the tap is off. Callable fields are evaluated lazily."""
    global _seq, _fd
    if not enabled():
        return
    try:
        record: dict[str, Any] = {"ts": time.time(), "kind": kind, "pid": os.getpid()}
        for key, value in fields.items():
            if callable(value):
                try:
                    value = value()
                except Exception as exc:
                    value = f"<error {type(exc).__name__}>"
            if value is not None:
                record[key] = value
        with _lock:
            _seq += 1
            record["seq"] = _seq
            fd = _open_locked()
            if fd is None:
                raise OSError("event log not open (retrying)")
            data = _encode(record)
            try:
                os.write(fd, data)
            except OSError:
                try:
                    os.close(fd)
                finally:
                    _fd = None
                raise
    except Exception as exc:
        _degrade(exc)


def text_fields(text: Any, *, head: int = 0) -> dict[str, Any]:
    """Length, short digest and (optionally) a head of a text, for event payloads."""
    if not isinstance(text, str):
        return {}
    out: dict[str, Any] = {
        "text_len": len(text),
        "text_sha": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12],
    }
    if head:
        out["text_head"] = text[:head]
    return out


def route_fields(bot: Any, route: Any, state: Any = None) -> dict[str, Any]:
    """Agent identity for a Telegram route: cheap dict lookups only, never a JSONL read."""
    out: dict[str, Any] = {}
    try:
        chat_id = getattr(route, "chat_id", None)
        topic_id = getattr(route, "thread_id", None)
        out.update(route=f"{chat_id}:{topic_id}", chat_id=chat_id, topic_id=topic_id)
        key = (getattr(bot, "_route_inbox_target_keys_by_route", None) or {}).get(route)
        if key is None and state is not None:
            projection = getattr(bot, "_state_inbox_projection", None)
            if projection is not None:
                key = projection(state, allow_jsonl_lookup=False)
        if key:
            out["team"], out["agent_name"] = key[0], key[1]
        if state is not None and getattr(state, "session_id", None):
            out["session_id"] = state.session_id
    except Exception as exc:
        out["identity_error"] = type(exc).__name__
    return out


def emit_route(
    kind: str,
    bot: Any,
    route: Any,
    *,
    state: Any = None,
    text: Any = None,
    text_head: int = 0,
    trigger_message: Any = None,
    **fields: Any,
) -> None:
    """``emit`` with the route's agent identity; explicit fields win over derived ones.

    ``text`` adds ``text_len``/``text_sha`` (and ``text_head`` characters of it);
    ``trigger_message`` (a QueuedMessage) adds ``trigger`` and ``trigger_message_id``.
    Nothing is computed when the tap is off.
    """
    if not enabled():
        return
    try:
        merged = route_fields(bot, route, state)
        if text is not None:
            merged.update(text_fields(text, head=text_head))
        if kind in ("turn_start", "turn_end"):
            message_id = getattr(trigger_message, "telegram_message_id", None)
            merged["trigger_message_id"] = message_id if isinstance(message_id, int) else None
            merged["trigger"] = (
                "schedule" if fields.get("schedule_id")
                else "message" if isinstance(message_id, int)
                else "none"
            )
        merged.update({key: value for key, value in fields.items() if value is not None})
    except Exception as exc:
        _degrade(exc)
        return
    emit(kind, **merged)


def emit_session_build(options: Any, *, env_overrides: Any, session_id: Any, model: Any, claude_route: bool) -> None:
    """``session_build`` for one ClaudeAgentOptions build (session.py ``_build_options``)."""
    if not enabled():
        return
    try:
        overrides = env_overrides if isinstance(env_overrides, dict) else {}
        cli_path = getattr(options, "cli_path", None)
        emit(
            "session_build",
            agent_name=overrides.get("CLAUDE_CODE_AGENT_NAME") or None,
            team=overrides.get("CLAUDE_CODE_TEAM_NAME") or None,
            session_id=session_id,
            source="resume" if getattr(options, "resume", None) else "fresh",
            model=model,
            cli_path=str(cli_path) if cli_path else None,
            runtime="cli-path" if cli_path else _bundled_runtime(),
            version=None if cli_path else _bundled_version(),
            credential_profile="shared-claude" if claude_route else "cli-proxy",
        )
    except Exception as exc:
        _degrade(exc)


def _bundled_version() -> str:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
        return str(__cli_version__)
    except Exception:
        return "unknown"


def _bundled_runtime() -> str:
    return f"cc-{_bundled_version()}"


def cli_runtime(state: Any) -> str:
    """``cc-2.1.59`` for the bundled CLI, else ``cli:<basename>`` of the client's cli_path."""
    client = getattr(getattr(state, "session_manager", None), "_client", None)
    cli_path = getattr(getattr(client, "options", None), "cli_path", None)
    if not cli_path:
        return _bundled_runtime()
    return f"cli:{os.path.basename(str(cli_path))}"


def result_subtype(runner: Any, *, failed: bool, interrupted: bool) -> str:
    """The CLI result subtype when the runner saw one, else a derived label."""
    subtype = getattr(getattr(runner, "_last_result_message", None), "subtype", None)
    if isinstance(subtype, str) and subtype:
        return subtype
    if interrupted:
        return "interrupted"
    return "error" if failed else "success"
