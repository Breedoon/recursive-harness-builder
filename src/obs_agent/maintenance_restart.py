"""Opt-in maintenance restart: snapshot running agents, restart, resume them.

A plain restart (``supervisorctl restart obs-telegram-prod``) or ``/stop`` is
the kill switch and is unchanged. When no marker file exists at startup,
nothing is resumed.

A *maintenance* restart is requested explicitly, either with the Telegram
``/maintenance_restart`` command or with SIGUSR1 to the ``telegram_main``
process. Running ``python -m obs_agent.maintenance_restart`` sends that signal.
The running daemon then:

1. writes ``maintenance-resume.json`` next to the Telegram state DB. The file
   lists every route that is mid-turn (``busy`` / ``execution_active``), with
   its session id, fork/team identity, local-vs-hosted flag and queued
   messages;
2. terminates its own process group, so supervisord's ``autorestart`` brings
   OBS back, with the same effect as a plain restart for the CLI children.

On startup the new daemon renames the marker to ``*.consumed`` *before* acting
on it, so a crash loop can never resurrect agents twice. It ignores markers
older than :data:`DEFAULT_MAX_AGE_SECONDS`. It then resumes each recorded
route with a note saying the turn was cut off. Local-model routes are resumed
strictly one at a time behind the single inference server; hosted routes are
staggered.

This module holds the pure, testable pieces and the operator CLI. The daemon
integration lives in :mod:`obs_agent.telegram`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MARKER_FILENAME = "maintenance-resume.json"
CONSUMED_SUFFIX = ".consumed"
MARKER_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 15 * 60
DEFAULT_HOSTED_STAGGER_SECONDS = 3.0
MAINTENANCE_SIGNAL = signal.SIGUSR1
TELEGRAM_MAIN_PATTERN = "obs_agent.telegram_main"


@dataclass
class QueuedEntry:
    text: str
    telegram_message_id: int | None = None
    reply_to_message_id: int | None = None


@dataclass
class ResumeEntry:
    chat_id: int
    thread_id: int | None
    session_id: str | None
    task_id: str | None = None
    team_name: str | None = None
    agent_name: str | None = None
    is_local: bool = False
    model: str | None = None
    jsonl_head_uuid: str | None = None
    topic_title: str | None = None
    queued: list[QueuedEntry] = field(default_factory=list)


@dataclass
class ResumeMarker:
    requested_at: float
    source: str
    entries: list[ResumeEntry]
    version: int = MARKER_VERSION


def marker_path(state_dir: Path) -> Path:
    return Path(state_dir) / MARKER_FILENAME


def is_local_model(model: str | None) -> bool:
    """Local models are named ``local-*`` (an optional ``[Nk]`` suffix is ignored)."""
    return bool(model) and str(model).strip().lower().startswith("local-")


def write_marker(path: Path, marker: ResumeMarker) -> Path:
    """Write the marker atomically (tmp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(marker), ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _entry_from_dict(raw: dict) -> ResumeEntry:
    queued = [
        QueuedEntry(
            text=str(item.get("text") or ""),
            telegram_message_id=item.get("telegram_message_id"),
            reply_to_message_id=item.get("reply_to_message_id"),
        )
        for item in (raw.get("queued") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    return ResumeEntry(
        chat_id=int(raw["chat_id"]),
        thread_id=raw.get("thread_id"),
        session_id=raw.get("session_id"),
        task_id=raw.get("task_id"),
        team_name=raw.get("team_name"),
        agent_name=raw.get("agent_name"),
        is_local=bool(raw.get("is_local", False)),
        model=raw.get("model"),
        jsonl_head_uuid=raw.get("jsonl_head_uuid"),
        topic_title=raw.get("topic_title"),
        queued=queued,
    )


def consume_marker(
    path: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> tuple[ResumeMarker | None, str]:
    """Rename the marker to ``*.consumed`` first, then parse it.

    Returns ``(marker, reason)``. ``marker`` is None when there is nothing to
    resume: missing, unreadable, stale, or a future version. The rename happens
    before parsing, so even a marker that crashes the resume path cannot be
    replayed on the next start.
    """
    path = Path(path)
    if not path.exists():
        return None, "no_marker"
    consumed = path.with_name(path.name + CONSUMED_SUFFIX)
    try:
        os.replace(path, consumed)
    except OSError as exc:
        return None, f"rename_failed:{exc}"
    try:
        raw = json.loads(consumed.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt marker must never crash startup
        return None, f"unreadable:{type(exc).__name__}"
    if not isinstance(raw, dict) or int(raw.get("version", 0)) != MARKER_VERSION:
        return None, "unsupported_version"
    requested_at = float(raw.get("requested_at") or 0.0)
    current = time.time() if now is None else now
    age = current - requested_at
    if age < 0 or age > max_age_seconds:
        return None, f"stale:{int(age)}s"
    entries: list[ResumeEntry] = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(_entry_from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return (
        ResumeMarker(requested_at=requested_at, source=str(raw.get("source") or ""), entries=entries),
        "ok",
    )


def split_resume_order(entries: list[ResumeEntry]) -> tuple[list[ResumeEntry], list[ResumeEntry]]:
    """Return ``(hosted, local)`` preserving the recorded order within each group."""
    hosted = [entry for entry in entries if not entry.is_local]
    local = [entry for entry in entries if entry.is_local]
    return hosted, local


def build_resume_prompt(*, requested_at: float, queued_count: int = 0) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(requested_at))
    lines = [
        f"(System: OBS was restarted for maintenance at {stamp} while your turn was in progress; "
        "the turn was cut off mid-way.)",
        "A tool call that was in flight may or may not have taken effect. Check its result or "
        "side effect (file contents, git log, bead state, sent messages) before repeating it; do "
        "not blindly redo non-idempotent actions.",
        "Continue your task from where you left off.",
    ]
    if queued_count:
        lines.append(
            f"{queued_count} message(s) that were queued for you before the restart are attached "
            "after this note."
        )
    return "\n".join(lines)


def max_age_from_env() -> float:
    raw = (os.environ.get("OBS_MAINTENANCE_RESUME_MAX_AGE_SECONDS") or "").strip()
    try:
        return float(raw) if raw else float(DEFAULT_MAX_AGE_SECONDS)
    except ValueError:
        return float(DEFAULT_MAX_AGE_SECONDS)


def hosted_stagger_from_env() -> float:
    raw = (os.environ.get("OBS_MAINTENANCE_RESUME_STAGGER_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_HOSTED_STAGGER_SECONDS
    except ValueError:
        return DEFAULT_HOSTED_STAGGER_SECONDS


# --- operator CLI ---------------------------------------------------------


def find_daemon_pids(pattern: str = TELEGRAM_MAIN_PATTERN) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"python.*-m {pattern}"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        return []
    own = os.getpid()
    return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != own]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m obs_agent.maintenance_restart",
        description=(
            "Request an OBS maintenance restart: agents that are mid-turn are snapshotted and "
            "resumed automatically after the restart. A plain `supervisorctl restart "
            "obs-telegram-prod` stays the kill switch (nothing is resumed)."
        ),
    )
    parser.add_argument("--pid", type=int, help="telegram_main PID (default: auto-detect)")
    parser.add_argument(
        "--status",
        metavar="STATE_DIR",
        help="print the pending or last consumed marker in STATE_DIR and exit",
    )
    args = parser.parse_args(argv)

    if args.status:
        base = marker_path(Path(args.status))
        for candidate in (base, base.with_name(base.name + CONSUMED_SUFFIX)):
            if candidate.exists():
                print(f"{candidate}:")
                print(candidate.read_text(encoding="utf-8"))
                return 0
        print(f"no marker in {args.status}")
        return 1

    pids = [args.pid] if args.pid else find_daemon_pids()
    if len(pids) != 1:
        print(
            f"expected exactly one telegram_main process, found {pids or 'none'}; pass --pid",
            file=sys.stderr,
        )
        return 2
    os.kill(pids[0], MAINTENANCE_SIGNAL)
    print(f"sent SIGUSR1 (maintenance restart) to telegram_main pid {pids[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
