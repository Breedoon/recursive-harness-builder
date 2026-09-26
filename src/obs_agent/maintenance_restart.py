"""Maintenance restart and crash resume: snapshot running agents, resume them.

Three ways OBS can go down, and what comes back:

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
  the kill switch never resumes anything.

For a maintenance restart the running daemon:

1. writes ``maintenance-resume.json`` next to the Telegram state DB. The file
   lists every route that is mid-turn (``busy`` / ``execution_active``), with
   its session id, fork/team identity, local-vs-hosted flag and queued
   messages;
2. terminates its own process group, so supervisord's ``autorestart`` brings
   OBS back, with the same effect as a plain restart for the CLI children.

On startup the new daemon renames the marker to ``*.consumed`` *before* acting
on it, so a crash loop can never resurrect agents twice. It ignores markers
and crash snapshots whose last write is older than
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
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MARKER_FILENAME = "maintenance-resume.json"
CRASH_SNAPSHOT_FILENAME = "crash-resume.json"
CONSUMED_SUFFIX = ".consumed"
KILLSWITCH_SUFFIX = ".killswitch"
KILLSWITCH_SENTINEL_ENV = "OBS_KILLSWITCH_SENTINEL"
DEFAULT_CRASH_SNAPSHOT_SECONDS = 15.0
DEFAULT_CRASH_MAX_CONSECUTIVE = 2
DEFAULT_ORPHAN_REAP_GRACE_SECONDS = 5.0
AGENT_CLI_MARKER = "claude_agent_sdk/_bundled/claude"
MARKER_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 5 * 60
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


def peek_requested_at(path: Path, *, now: float | None = None, max_age_seconds: float) -> float | None:
    """Read a marker's ``requested_at`` without consuming it (None if absent/stale)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict) or int(raw.get("version", 0) or 0) != MARKER_VERSION:
        return None
    try:
        requested_at = float(raw.get("requested_at") or 0.0)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    age = _write_age_seconds(Path(path), requested_at, current)
    if age < 0 or age > max_age_seconds:
        return None
    return requested_at


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
        crash_resume_count=int(raw.get("crash_resume_count") or 0),
        inflight_prompt=(str(raw.get("inflight_prompt")) if raw.get("inflight_prompt") else None),
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
    return _marker_from_raw(raw, path=consumed, now=now, max_age_seconds=max_age_seconds)


def _marker_from_raw(
    raw: object,
    *,
    path: Path,
    now: float | None,
    max_age_seconds: float,
) -> tuple[ResumeMarker | None, str]:
    if not isinstance(raw, dict) or int(raw.get("version", 0) or 0) != MARKER_VERSION:
        return None, "unsupported_version"
    try:
        requested_at = float(raw.get("requested_at") or 0.0)
    except (TypeError, ValueError):
        return None, "unreadable:requested_at"
    current = time.time() if now is None else now
    age = _write_age_seconds(path, requested_at, current)
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


def peek_marker(path: Path, *, now: float | None = None, max_age_seconds: float) -> ResumeMarker | None:
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


def kill_process_tree(root_pid: int, *, proc_root: Path = Path("/proc"), kill=os.kill) -> list[int]:
    """SIGKILL a Claude CLI and every process it started (tool shells etc.).

    The CLI shares the daemon's process group, so a group kill is not an
    option; the tree is walked through ``/proc`` instead. Descendants are
    collected before the root dies so they cannot escape by reparenting.
    """
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
        "--status",
        metavar="STATE_DIR",
        help="print the pending or last consumed maintenance marker and crash snapshot in STATE_DIR and exit",
    )
    args = parser.parse_args(argv)

    if args.status:
        found = False
        for base in (marker_path(Path(args.status)), crash_snapshot_path(Path(args.status))):
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

    if args.plain:
        return plain_restart()

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
