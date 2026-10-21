"""Maintenance restart and crash resume: snapshot running agents, resume them.

OBS shutdown/restart paths and what comes back:

* **Maintenance restart (the default for planned restarts).** Telegram
  ``/restart`` (alias ``/maintenance_restart``), SIGUSR1 to ``telegram_main``,
  or ``python -m obs_agent.maintenance_restart``. Mid-turn agents resume.
* **Plain restart (the kill switch).** ``supervisorctl restart|stop
  obs-telegram-prod``, Telegram ``/restart plain`` or ``python -m
  obs_agent.maintenance_restart --plain``. Nothing resumes. ``/stop``,
  ``/stop_branch`` and ``/stop_tree`` also still resume nothing: stopped
  routes are left out of every snapshot.
* **Crash** (OOM kill, cache-proxy watchdog, unhandled exit). The daemon keeps
  a periodic snapshot of mid-turn routes (``crash-resume.json``); on the next
  start the routes in it resume with a "restarted unexpectedly" note. A
  SIGTERM (supervisord stop, plain restart) discards that snapshot first, and
  the supervisord wrapper leaves a kill-switch sentinel as a second guard, so
  ordinary kill-switch stops do not resume the crash snapshot.
* **Intentional shutdown.** ``/shutdown`` leaves OBS stopped with no resume;
  ``/shutdown_autoresume`` saves a durable ``shutdown-resume.json`` checkpoint
  before stopping. Only that explicitly staged checkpoint has no age limit.
  The host recovery timer respects the shutdown latch until the wrapper clears
  it on the next explicit start or boot autostart.

For a maintenance restart the running daemon:

1. writes ``maintenance-resume.json`` next to the Telegram state DB. The file
   lists every route that is mid-turn (``busy`` / ``execution_active``), with
   its session id, fork/team identity, local-vs-hosted flag and queued
   messages;
2. terminates its own process group, so supervisord's ``autorestart`` brings
   OBS back, with the same effect as a plain restart for the CLI children.

On startup the new daemon renames the marker to ``*.consumed`` *before* acting
on it and fsyncs the directory, so a crash loop cannot replay the same marker.
It ignores ordinary maintenance markers and crash snapshots whose last write is older than
:data:`DEFAULT_MAX_AGE_SECONDS` (5 minutes): a quick crash plus supervisord
autorestart resumes, while a server that stayed down longer (unplugged,
stopped) resumes nothing. It then resumes each recorded
route with a note saying the turn was cut off. Local-model routes are resumed
strictly one at a time behind the single inference server; hosted routes are
staggered.

This module holds the pure, testable pieces and the operator CLI. The daemon
integration lives in :mod:`obs_agent.telegram`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MARKER_FILENAME = "maintenance-resume.json"
CRASH_SNAPSHOT_FILENAME = "crash-resume.json"
SHUTDOWN_MARKER_FILENAME = "shutdown-resume.json"
SHUTDOWN_SENTINEL_ENV = "OBS_SHUTDOWN_SENTINEL"
SHUTDOWN_SENTINEL_FILENAME = "obs-telegram-prod.shutdown"
CONSUMED_SUFFIX = ".consumed"
KILLSWITCH_SUFFIX = ".killswitch"
KILLSWITCH_SENTINEL_ENV = "OBS_KILLSWITCH_SENTINEL"
DEFAULT_CRASH_SNAPSHOT_SECONDS = 15.0
DEFAULT_CRASH_MAX_CONSECUTIVE = 2
DEFAULT_ORPHAN_REAP_GRACE_SECONDS = 5.0
AGENT_CLI_MARKER = "claude_agent_sdk/_bundled/claude"
MARKER_VERSION = 1
# 15 min (was 5): a power cut plus host boot plus Wi-Fi return took ~5 min on
# 2026-10-21; Daniel asked for 15 min headroom. Also the kill-switch horizon.
DEFAULT_MAX_AGE_SECONDS = 15 * 60
DEFAULT_HOSTED_STAGGER_SECONDS = 3.0
MAINTENANCE_SIGNAL = signal.SIGUSR1
TELEGRAM_MAIN_PATTERN = "obs_agent.telegram_main"
DAEMON_SUPERVISOR_PROCESS = "obs-telegram-prod"


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
    # How many crash resumes in a row this turn has had (crash-loop guard).
    crash_resume_count: int = 0
    # The turn's input, recorded only while the route has no session id yet
    # (no transcript to resume). Older markers lack it; readers default None.
    inflight_prompt: str | None = None


@dataclass
class ResumeMarker:
    requested_at: float
    source: str
    entries: list[ResumeEntry]
    version: int = MARKER_VERSION
    # Fork/AgentTask runs in flight when the marker was written (their parent
    # has not heard back yet). Bounds late parent callbacks to the outage this
    # marker describes (vault-u3b.81). Older markers lack it; readers default [].
    inflight_task_ids: list[str] = field(default_factory=list)


def marker_path(state_dir: Path) -> Path:
    return Path(state_dir) / MARKER_FILENAME


def crash_snapshot_path(state_dir: Path) -> Path:
    return Path(state_dir) / CRASH_SNAPSHOT_FILENAME


def shutdown_marker_path(state_dir: Path) -> Path:
    return Path(state_dir) / SHUTDOWN_MARKER_FILENAME


def shutdown_sentinel_path(state_dir: Path) -> Path:
    return Path(os.environ.get(SHUTDOWN_SENTINEL_ENV) or Path(state_dir) / SHUTDOWN_SENTINEL_FILENAME)


def discard_resume_markers(state_dir: Path) -> None:
    """A plain stop must also cancel any previously staged maintenance resume."""
    for path in (marker_path(state_dir), shutdown_marker_path(state_dir)):
        try:
            os.replace(path, path.with_name(path.name + KILLSWITCH_SUFFIX))
        except FileNotFoundError:
            pass


def discard_crash_snapshot(state_dir: Path) -> bool:
    """Kill switch: move the crash snapshot aside so the next start resumes nothing.

    Kept as ``crash-resume.json.killswitch`` for diagnosis. Returns True when a
    snapshot existed. Must be cheap and synchronous: it runs from the SIGTERM
    handler.
    """
    path = crash_snapshot_path(state_dir)
    try:
        os.replace(path, path.with_name(path.name + KILLSWITCH_SUFFIX))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def consume_killswitch_sentinel(sentinel: str | os.PathLike[str] | None) -> bool:
    """True when the supervisord wrapper recorded a kill-switch stop; removes it.

    The wrapper touches the sentinel from its TERM trap (``supervisorctl
    stop/restart``, container stop, and the group SIGTERM of a maintenance or
    plain restart). A crash never runs that trap.
    """
    if not sentinel:
        return False
    path = Path(sentinel)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def _write_age_seconds(path: Path, requested_at: float, now: float) -> float:
    """Age of a marker/snapshot: time since its last write.

    Uses the older of ``requested_at`` (stamped at every write) and the file
    mtime (preserved by the consume rename), so either one being old makes the
    file stale. A ``requested_at`` in the future is reported as negative.
    """
    age = now - requested_at
    if age < 0:
        return age
    try:
        age = max(age, now - Path(path).stat().st_mtime)
    except OSError:
        pass
    return age


def peek_requested_at(path: Path, *, now: float | None = None, max_age_seconds: float | None) -> float | None:
    """Read a marker's original ``requested_at`` without consuming it."""
    marker = peek_marker(path, now=now, max_age_seconds=max_age_seconds)
    return marker.requested_at if marker is not None else None


def is_local_model(model: str | None) -> bool:
    """Local models are named ``local-*`` (an optional ``[Nk]`` suffix is ignored)."""
    return bool(model) and str(model).strip().lower().startswith("local-")


def write_marker(path: Path, marker: ResumeMarker) -> Path:
    """Write the marker atomically and durably before shutdown."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(asdict(marker), stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
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
        crash_resume_count=int(raw.get("crash_resume_count") or 0),
        inflight_prompt=(str(raw.get("inflight_prompt")) if raw.get("inflight_prompt") else None),
    )


def consume_marker(
    path: Path,
    *,
    now: float | None = None,
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS,
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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        return None, f"rename_failed:{exc}"
    try:
        raw = json.loads(consumed.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt marker must never crash startup
        return None, f"unreadable:{type(exc).__name__}"
    return _marker_from_raw(raw, path=consumed, now=now, max_age_seconds=max_age_seconds)


def _marker_from_raw(
    raw: object,
    *,
    path: Path,
    now: float | None,
    max_age_seconds: float | None,
) -> tuple[ResumeMarker | None, str]:
    if not isinstance(raw, dict):
        return None, "unsupported_version"
    try:
        if int(raw.get("version", 0) or 0) != MARKER_VERSION:
            return None, "unsupported_version"
    except (TypeError, ValueError):
        return None, "unsupported_version"
    try:
        requested_at = float(raw.get("requested_at") or 0.0)
    except (TypeError, ValueError):
        return None, "unreadable:requested_at"
    if not math.isfinite(requested_at):
        return None, "unreadable:requested_at"
    current = time.time() if now is None else now
    age = _write_age_seconds(path, requested_at, current)
    if age < 0 or (max_age_seconds is not None and age > max_age_seconds):
        return None, f"stale:{int(age)}s"
    entries: list[ResumeEntry] = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(_entry_from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    inflight_raw = raw.get("inflight_task_ids")
    inflight = (
        [str(item) for item in inflight_raw if isinstance(item, str) and item]
        if isinstance(inflight_raw, list)
        else []
    )
    return (
        ResumeMarker(
            requested_at=requested_at,
            source=str(raw.get("source") or ""),
            entries=entries,
            inflight_task_ids=inflight,
        ),
        "ok",
    )


def peek_marker(path: Path, *, now: float | None = None, max_age_seconds: float | None) -> ResumeMarker | None:
    """Parse a fresh marker without consuming it (None if absent, stale or unreadable)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt marker must never crash startup
        return None
    marker, _reason = _marker_from_raw(raw, path=Path(path), now=now, max_age_seconds=max_age_seconds)
    return marker


def split_resume_order(entries: list[ResumeEntry]) -> tuple[list[ResumeEntry], list[ResumeEntry]]:
    """Return ``(hosted, local)`` preserving the recorded order within each group."""
    hosted = [entry for entry in entries if not entry.is_local]
    local = [entry for entry in entries if entry.is_local]
    return hosted, local


# CACHE-PREFIX WARNING: every resumed route replays its JSONL in a NEW CLI process,
# so a restart is a full resume test for every mid-turn agent at once. The resume
# note built here must be sent as a NEW appended user message, never merged into
# or replacing earlier history; the variant pins and GPT cache keys in the proxy
# survive a restart only because they live in effort-pins.json and are derived
# from the conversation itself. A break is silent: no error, the API just re-bills the whole conversation.
# Live check: scripts/cache_regression.py resume_new_proc queued_resume ws_resume (docs/cache-proxy-safety.md).
def build_resume_prompt(
    *,
    requested_at: float,
    queued_count: int = 0,
    kind: str = "maintenance",
    original_prompt: str | None = None,
) -> str:
    """Resume note for a cut-off turn.

    ``original_prompt`` is given when the route had no session transcript yet;
    the note then carries the turn's original request instead of "continue".
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(requested_at))
    if original_prompt:
        cause = (
            f"OBS restarted unexpectedly (a crash, e.g. out of memory) shortly after {stamp}"
            if kind == "crash"
            else f"OBS was shut down with auto-resume staged at {stamp} and has now started again"
            if kind == "shutdown"
            else f"OBS was restarted for maintenance at {stamp}"
        )
        lines = [
            f"(System: {cause} just as your turn was starting, before a session transcript "
            "existed, so this is a fresh session. Any tool call you had begun may or may not "
            "have taken effect: check its result or side effect (file contents, git log, bead "
            "state, sent messages) before repeating it; do not blindly redo non-idempotent actions.)",
        ]
        if queued_count:
            lines.append(
                f"{queued_count} message(s) that were queued for you before the restart are "
                "attached after this note."
            )
        lines += ["The original request of the cut-off turn follows.", "", original_prompt]
        return "\n".join(lines)
    if kind == "crash":
        lines = [
            f"(System: OBS restarted unexpectedly (a crash, e.g. out of memory) shortly after "
            f"{stamp}. Your turn was most likely still in progress and was cut off mid-way.)",
        ]
    elif kind == "shutdown":
        lines = [
            f"(System: OBS was shut down with auto-resume staged at {stamp} while your turn was "
            "in progress and has now started again; the turn was cut off mid-way.)",
        ]
    else:
        lines = [
            f"(System: OBS was restarted for maintenance at {stamp} while your turn was in progress; "
            "the turn was cut off mid-way.)",
        ]
    lines += [
        "A tool call that was in flight may or may not have taken effect. Check its result or "
        "side effect (file contents, git log, bead state, sent messages) before repeating it; do "
        "not blindly redo non-idempotent actions.",
        "Continue your task from where you left off.",
    ]
    if kind == "crash":
        lines.append(
            "If your turn had in fact already finished, reply with a one-line status and stop."
        )
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


def crash_snapshot_interval_from_env() -> float:
    """Seconds between crash-resume snapshots; ``0`` disables crash resume."""
    raw = (os.environ.get("OBS_CRASH_RESUME_SNAPSHOT_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_CRASH_SNAPSHOT_SECONDS
    except ValueError:
        return DEFAULT_CRASH_SNAPSHOT_SECONDS


def crash_max_consecutive_from_env() -> int:
    """A turn crash-resumed this many times in a row is not resumed again."""
    raw = (os.environ.get("OBS_CRASH_RESUME_MAX_CONSECUTIVE") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_CRASH_MAX_CONSECUTIVE
    except ValueError:
        return DEFAULT_CRASH_MAX_CONSECUTIVE


# Local resumes are strictly serial (vault-u3b.86): the next local route starts
# only after the previous resumed turn ends (or errors / is stopped). This
# safety timeout is a logged last resort for a wedged turn, not a pacing knob.
DEFAULT_LOCAL_RESUME_SAFETY_SECONDS = 6 * 3600.0


def local_resume_wait_from_env(default: float = DEFAULT_LOCAL_RESUME_SAFETY_SECONDS) -> float:
    """Safety timeout for one serial local resume before the chain moves on.

    The resumed turn is never cancelled: if this last-resort timeout expires
    the chain logs an error and starts the next local route while the wedged
    one keeps running. ``0`` or a negative value means wait without limit.
    """
    raw = (os.environ.get("OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS") or "").strip()
    try:
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


# --- orphaned agent CLIs (vault-u3b.66) -----------------------------------


def _proc_stat_fields(pid: int, proc_root: Path) -> tuple[int, int] | None:
    """Return ``(ppid, pgid)`` from ``/proc/<pid>/stat`` (comm may contain spaces)."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        rest = raw[raw.rindex(")") + 2 :].split()
        return int(rest[1]), int(rest[2])
    except (ValueError, IndexError):
        return None


def descendant_pids(root_pid: int, *, proc_root: Path = Path("/proc")) -> list[int]:
    """All live descendants of ``root_pid`` (children first-found order)."""
    children: dict[int, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fields = _proc_stat_fields(int(entry.name), proc_root)
        if fields is not None:
            children.setdefault(fields[0], []).append(int(entry.name))
    found: list[int] = []
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.append(child)
                stack.append(child)
    return found


def _process_identity(pid: int, proc_root: Path) -> tuple[int, int] | None:
    """Return (parent pid, kernel start ticks); a zombie is no longer running."""
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        return None
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        fields = raw[raw.rindex(")") + 2 :].split()
        if fields[0] in {"Z", "X"}:
            return None
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def process_identity_matches(pid: int, start: int, *, proc_root: Path = Path("/proc")) -> bool:
    identity = _process_identity(pid, proc_root)
    return type(start) is int and start > 0 and identity is not None and identity[1] == start


def refresh_process_tree(owned: dict[int, int], *, proc_root: Path = Path("/proc")) -> dict[int, int]:
    """Retain owned identities and discover children of their still-live members.

    Retained tool shells remain roots after the CLI exits and they reparent to
    PID 1. Neither a recycled pid nor the daemon's shared group establishes ownership.
    """
    found = dict(owned)
    records: dict[int, tuple[int, int]] = {}
    children: dict[int, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name.isdigit():
            pid = int(entry.name)
            identity = _process_identity(pid, proc_root)
            if identity is not None:
                records[pid] = identity
                children.setdefault(identity[0], []).append(pid)
    stack = [pid for pid, start in owned.items() if records.get(pid, (None, None))[1] == start]
    seen = set(stack)
    while stack:
        parent = stack.pop()
        for child in children.get(parent, []):
            if child not in seen and records[child][1] >= records[parent][1]:
                seen.add(child)
                found[child] = records[child][1]
                stack.append(child)
    return found


def capture_process_tree(root_pid: int, *, proc_root: Path = Path("/proc")) -> dict[int, int]:
    """Capture kill authority only from a plain-int, live SDK CLI root."""
    if type(root_pid) is not int or root_pid <= 1 or root_pid == os.getpid():
        raise ValueError(f"capture_process_tree refused root pid {root_pid!r}")
    identity = _process_identity(root_pid, proc_root)
    argv = _proc_argv(root_pid, proc_root)
    if identity is None or not argv or AGENT_CLI_MARKER not in argv[0]:
        raise ValueError(f"capture_process_tree refused pid {root_pid}: not a live agent CLI")
    return refresh_process_tree({root_pid: identity[1]}, proc_root=proc_root)


def kill_process_identities(owned: dict[int, int], *, proc_root: Path = Path("/proc"), kill=os.kill) -> list[int]:
    """Reap retained tools even after their original CLI vanished; never a reused pid."""
    killed = []
    for pid, start in reversed(list(owned.items())):
        if not process_identity_matches(pid, start, proc_root=proc_root):
            continue
        try:
            kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    return killed


def kill_process_tree(root_pid: int, *, proc_root: Path = Path("/proc"), kill=os.kill) -> list[int]:
    """SIGKILL a Claude CLI and every process it started (tool shells etc.).

    The CLI shares the daemon's process group, so a group kill is not an
    option; the tree is walked through ``/proc`` instead. Descendants are
    collected before the root dies so they cannot escape by reparenting.

    The root must be a real agent CLI. On 2026-10-16 a unit test handed this a MagicMock pid;
    ``int(MagicMock())`` is 1, so the walk SIGKILLed every process under supervisord three
    times (obs-3). Anything that is not a plain int > 1 naming a live SDK CLI is refused.
    """
    if type(root_pid) is not int or root_pid <= 1 or root_pid == os.getpid():
        raise ValueError(f"kill_process_tree refused root pid {root_pid!r}")
    argv = _proc_argv(root_pid, proc_root)
    if not argv or AGENT_CLI_MARKER not in argv[0]:
        raise ValueError(f"kill_process_tree refused pid {root_pid}: not an agent CLI ({argv[:1]})")
    tree = [root_pid, *descendant_pids(root_pid, proc_root=proc_root)]
    for pid in tree:
        try:
            kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return tree


def find_orphaned_agent_clis(*, own_pgid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """Claude CLIs left behind by a previous daemon.

    A process qualifies only when all hold: its argv[0] is the Agent SDK's
    bundled ``claude`` binary; it was reparented to PID 1 (its daemon is gone);
    it is not in this daemon's process group; and its process-group leader no
    longer exists (the old supervisord wrapper exited). Live CLIs of any other
    running OBS instance keep a live parent and group leader, so they never
    match.
    """
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        argv = _proc_argv(pid, proc_root)
        if not argv or AGENT_CLI_MARKER not in argv[0]:
            continue
        fields = _proc_stat_fields(pid, proc_root)
        if fields is None:
            continue
        ppid, pgid = fields
        if ppid != 1 or pgid == own_pgid or pgid == pid:
            continue
        if (proc_root / str(pgid)).exists():
            continue
        found.append(pid)
    return sorted(found)


def reap_orphaned_agent_clis(
    *,
    own_pgid: int,
    grace_seconds: float = DEFAULT_ORPHAN_REAP_GRACE_SECONDS,
    proc_root: Path = Path("/proc"),
    kill=os.kill,
    sleep=time.sleep,
) -> list[int]:
    """SIGTERM then, after ``grace_seconds``, SIGKILL every orphaned agent CLI.

    SIGKILL matters: the orphans seen on 2026-09-25/26 were spinning at 100 %
    CPU and never processed stdin EOF (vault-u3b.66, .76). Returns the PIDs
    that were signalled.
    """
    pids = find_orphaned_agent_clis(own_pgid=own_pgid, proc_root=proc_root)
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not pids:
        return []
    sleep(grace_seconds)
    survivors = set(find_orphaned_agent_clis(own_pgid=own_pgid, proc_root=proc_root))
    for pid in pids:
        if pid in survivors:
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return pids


# --- operator CLI ---------------------------------------------------------


def _proc_argv(pid: int, proc_root: Path = Path("/proc")) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _proc_supervisor_name(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for part in raw.split(b"\0"):
        if part.startswith(b"SUPERVISOR_PROCESS_NAME="):
            return part.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def _is_daemon_argv(argv: list[str], pattern: str) -> bool:
    """True only for ``<python> -m <pattern> [args]`` - not shells that mention it."""
    if len(argv) < 3 or "python" not in Path(argv[0]).name:
        return False
    return argv[1] == "-m" and argv[2] == pattern


def find_daemon_pids(
    pattern: str = TELEGRAM_MAIN_PATTERN,
    *,
    supervisor_process: str = DAEMON_SUPERVISOR_PROCESS,
    proc_root: Path = Path("/proc"),
) -> list[int]:
    """Locate telegram_main, preferring the supervisord-managed prod daemon.

    Exact argv matching skips shells or pgrep invocations whose command line
    merely contains the pattern. When several daemons match (e.g. a test daemon
    started from a worktree), the one supervisord runs as ``supervisor_process``
    wins; if none carries that marker, every match is returned so the caller
    can refuse to guess.
    """
    own = os.getpid()
    matches: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own:
            continue
        if _is_daemon_argv(_proc_argv(pid, proc_root), pattern):
            matches.append(pid)
    matches.sort()
    supervised = [pid for pid in matches if _proc_supervisor_name(pid, proc_root) == supervisor_process]
    if supervised:
        return supervised
    return matches


def stop_daemon(*, run=subprocess.run) -> None:
    """Stop via Supervisor without autorestart, raising if the stop fails.

    Deliberately synchronous: no new daemon turn is admitted between its final
    checkpoint and the stop. On success this process is terminated by Supervisor;
    on failure the caller remains alive and can unstage the checkpoint/latch.
    The control client has its own session so the group stop cannot kill it.
    """
    run(
        ["supervisorctl", "stop", DAEMON_SUPERVISOR_PROCESS],
        check=True,
        timeout=15,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def plain_restart(*, popen=subprocess.Popen) -> int:
    """Kill-switch restart: ``supervisorctl restart obs-telegram-prod``, resuming nothing.

    Started in its own session so the supervisorctl client survives when
    supervisord stops the process group it was launched from (an agent's shell
    is inside that group); otherwise the stop half could run without the
    start half. The daemon's SIGTERM handler and the wrapper's kill-switch
    sentinel make sure no crash snapshot is resumed.
    """
    popen(
        ["supervisorctl", "restart", DAEMON_SUPERVISOR_PROCESS],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"started `supervisorctl restart {DAEMON_SUPERVISOR_PROCESS}` (plain restart: nothing resumes)")
    return 0


FREEZE_BLOCKED_RC = 4


def freeze_check_command(*, plain: bool) -> str:
    """The command text a CLI restart is about to perform, as freeze.py should judge it."""
    if plain:
        return f"python -m obs_agent.maintenance_restart --plain; supervisorctl restart {DAEMON_SUPERVISOR_PROCESS}"
    return "python -m obs_agent.maintenance_restart"


def _freeze_blocks(*, plain: bool) -> str | None:
    """Refusal text when an active freeze covers this restart (ops-47.6); None = go ahead.

    The CLI is how agents and scripts restart OBS, so it refuses during a covering freeze unless
    ``OPS_FREEZE_ACK=<freeze id>`` is set. A freeze-tool failure never blocks (fail open).
    Daniel's own Telegram ``/restart`` is not routed through here; it is honoured with a warning.
    """
    try:
        from obs_agent import freeze_guard

        result = freeze_guard.check(freeze_check_command(plain=plain))
    except Exception as exc:  # noqa: BLE001 - fail open
        print(f"freeze check could not run ({exc}); continuing", file=sys.stderr)
        return None
    if result.error:
        print(f"freeze check unavailable ({result.error}); continuing", file=sys.stderr)
    if result.blocked:
        return result.message + f"\n(restart refused by the freeze guard, ops-47.6; exit {FREEZE_BLOCKED_RC})"
    return None


def _run_cache_gate(repo: Path):
    """The cache-prefix gate (vault-3px5); None if it cannot run (never blocks on its own bug)."""
    try:
        from obs_agent import cache_gate

        return cache_gate.check(repo)
    except Exception as exc:
        print(f"cache gate could not run ({exc}); continuing", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m obs_agent.maintenance_restart",
        description=(
            "Restart OBS. Default: maintenance restart - agents that are mid-turn are "
            "snapshotted and resumed automatically after the restart. --plain: the kill "
            "switch, same as `supervisorctl restart obs-telegram-prod` - nothing is resumed."
        ),
    )
    parser.add_argument("--pid", type=int, help="telegram_main PID (default: auto-detect)")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="plain kill-switch restart: resume nothing (runs supervisorctl restart obs-telegram-prod)",
    )
    parser.add_argument(
        "--skip-cache-gate",
        metavar="REASON",
        help=(
            "emergency only: restart even though the cache-prefix gate refuses (no passing "
            "scripts/cache_regression.py report for this code); the reason is logged"
        ),
    )
    parser.add_argument(
        "--status",
        metavar="STATE_DIR",
        help="print the pending or last consumed maintenance marker and crash snapshot in STATE_DIR and exit",
    )
    args = parser.parse_args(argv)

    if args.status:
        found = False
        for base in (shutdown_marker_path(Path(args.status)), marker_path(Path(args.status)), crash_snapshot_path(Path(args.status))):
            for candidate in (
                base,
                base.with_name(base.name + CONSUMED_SUFFIX),
                base.with_name(base.name + KILLSWITCH_SUFFIX),
            ):
                if candidate.exists():
                    found = True
                    print(f"{candidate}:")
                    print(candidate.read_text(encoding="utf-8"))
        if not found:
            print(f"no marker in {args.status}")
            return 1
        return 0

    blocked = _freeze_blocks(plain=args.plain)
    if blocked is not None:
        print(blocked, file=sys.stderr)
        return FREEZE_BLOCKED_RC

    repo = Path(__file__).resolve().parents[2]
    gate = _run_cache_gate(repo)
    if args.plain:
        if gate is not None and not gate.ok:
            from obs_agent import cache_gate

            cache_gate.log_bypass(reason="plain restart (kill switch, ungated)", source="cli --plain", result=gate)
        return plain_restart()
    if gate is not None and not gate.ok:
        if args.skip_cache_gate is not None:
            if not args.skip_cache_gate.strip():
                print("--skip-cache-gate needs a reason", file=sys.stderr)
                return 2
            from obs_agent import cache_gate

            cache_gate.log_bypass(reason=args.skip_cache_gate, source="cli", result=gate)
            print(f"cache gate bypassed (logged): {args.skip_cache_gate}")
        else:
            print(gate.message, file=sys.stderr)
            return 3
    elif gate is not None:
        print(gate.message)

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
