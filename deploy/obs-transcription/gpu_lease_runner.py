"""Reusable client-side GPU lease runner over the accepted r4 gate.

Implements the full lifecycle: begin -> flock -> acquired -> child subprocess
-> cleanup verification -> unlock -> release.  The workload runs as an
argv-only child subprocess; this module never imports CTranslate2 or
faster_whisper.

Usage:
    from gpu_lease_runner import run_lease
    result = run_lease(
        command=["/path/to/python", "transcribe_lease.py", "_worker", ...],
        job_id="20260824T141500Z-fw-abc12345",
        lock_path=Path("/workspace/gpu-coordination/rtx3090.lock"),
        ssh_target="breedoon@host.docker.internal",
        jobs_dir=Path("/workspace/runtime/transcription/jobs"),
    )
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOST_HELPER = "/usr/local/sbin/obs-llm-gate-control"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_RUNTIME_SECONDS = int(os.environ.get("OBS_TRANSCRIPTION_CHILD_TIMEOUT_SECONDS", "900"))
GPU_WAIT_TIMEOUT = int(os.environ.get("OBS_TRANSCRIPTION_GPU_WAIT_TIMEOUT_SECONDS", "3600"))
ADMISSION_WAIT_TIMEOUT = int(
    os.environ.get("OBS_TRANSCRIPTION_ADMISSION_WAIT_TIMEOUT_SECONDS", "4620")
)
GPU_CLEANUP_TIMEOUT = int(os.environ.get("OBS_TRANSCRIPTION_GPU_CLEANUP_TIMEOUT_SECONDS", "45"))
GATE_CONTROL_TIMEOUT_SECONDS = int(
    os.environ.get("OBS_TRANSCRIPTION_GATE_CONTROL_TIMEOUT_SECONDS", "20")
)
GATE_ACQUIRED_CONTROL_TIMEOUT_SECONDS = int(
    os.environ.get("OBS_TRANSCRIPTION_GATE_ACQUIRED_TIMEOUT_SECONDS", "45")
)
SIDECAR_WORKER_TIMEOUT_SECONDS = 5670
WORKER_OWNERSHIP_SECONDS = int(
    os.environ.get("OBS_TRANSCRIPTION_WORKER_OWNERSHIP_SECONDS", "5520")
)
TEARDOWN_RESERVE_SECONDS = int(
    os.environ.get("OBS_TRANSCRIPTION_TEARDOWN_RESERVE_SECONDS", "220")
)
OWNER_DEADLINE_GUARD_SECONDS = int(
    os.environ.get("OBS_TRANSCRIPTION_OWNER_DEADLINE_GUARD_SECONDS", "30")
)
CHILD_LAUNCH_RESERVE_SECONDS = 5
CHILD_TERM_KILL_BUDGET_SECONDS = 30
CHILD_WATCHDOG_BUDGET_SECONDS = 10
FIFO_TERMINAL_BUDGET_SECONDS = 20
FILESYSTEM_TERMINAL_BUDGET_SECONDS = 10
TERMINAL_GATE_CONTROL_BUDGET_SECONDS = 2 * GATE_CONTROL_TIMEOUT_SECONDS
TERMINAL_MARGIN_SECONDS = 25
MINIMUM_SIDECAR_MARGIN_SECONDS = 120
GPU_CLEANUP_TOLERANCE_MIB = 64
GPU_CLEANUP_INTERVAL = 0.5
POLL_SECONDS = 0.25
KNOWN_HOSTS = Path("/home/agent/.ssh/known_hosts")
DISABLED_SENTINEL = Path(
    "/workspace/runtime/transcription/run/gpu-only-disabled.json"
)
ADMISSION_ROOT = Path(
    os.environ.get(
        "OBS_TRANSCRIPTION_ADMISSION_ROOT",
        "/workspace/runtime/transcription/run/gpu-fifo",
    )
)
ADMISSION_FAIL_CLOSED = ADMISSION_ROOT / "fail-closed.json"
ADMISSION_RELEASE_GUARD = ADMISSION_ROOT / "release-guard.json"
_PROCESS_STARTED_MONOTONIC = time.monotonic()
PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.prctl.argtypes = [
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
]
_LIBC.prctl.restype = ctypes.c_int
_LIBC.alarm.argtypes = [ctypes.c_uint]
_LIBC.alarm.restype = ctypes.c_uint

_child: subprocess.Popen[Any] | None = None
_watchdog: subprocess.Popen[Any] | None = None
_watchdog_lifeline_write: int | None = None
_interrupted: str | None = None
_cleanup_in_progress = False


class GateControlError(RuntimeError):
    def __init__(self, action: str, message: str):
        super().__init__(message)
        self.action = action


class GateControlRejected(GateControlError):
    pass


class GateControlTransportError(GateControlError):
    pass


class GateControlTransportTimeout(GateControlTransportError):
    pass


class ProductionTranscriptionDisabled(RuntimeError):
    pass


class FifoReleaseError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _atomic_json_no_replace(path: Path, value: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    try:
        with temp.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return True
    finally:
        temp.unlink(missing_ok=True)


def _append_event(job: Path, event: str, **fields: Any) -> None:
    row = {"at": _now(), "event": event, **fields}
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with (job / "timeline.jsonl").open("ab", buffering=0) as handle:
        handle.write(encoded)
        os.fsync(handle.fileno())


def _append_event_best_effort(job: Path, event: str, **fields: Any) -> None:
    try:
        _append_event(job, event, **fields)
    except BaseException:
        pass


def _write_state(job: Path, phase: str, **fields: Any) -> dict[str, Any]:
    path = job / "state.json"
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    state = {**previous, **fields, "job_id": job.name, "phase": phase, "updated_at": _now()}
    _atomic_json(path, state)
    return state


def _write_state_best_effort(job: Path, phase: str, **fields: Any) -> None:
    try:
        _write_state(job, phase, **fields)
    except BaseException as exc:
        _append_event_best_effort(
            job,
            "state_write_failed",
            phase=phase,
            error=f"{type(exc).__name__}: {exc}",
        )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _remaining_seconds(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"lifecycle deadline exhausted before {operation}")
    return remaining


def _raise_if_production_disabled() -> None:
    if DISABLED_SENTINEL.exists() or DISABLED_SENTINEL.is_symlink():
        raise ProductionTranscriptionDisabled(
            "GPU voice transcription is temporarily disabled after failed "
            "lifecycle qualification; retry later"
        )


def _lifecycle_deadlines(started: float) -> tuple[float, float, float]:
    terminal_window = TEARDOWN_RESERVE_SECONDS - OWNER_DEADLINE_GUARD_SECONDS
    minimum_terminal_window = (
        CHILD_TERM_KILL_BUDGET_SECONDS
        + CHILD_WATCHDOG_BUDGET_SECONDS
        + GPU_CLEANUP_TIMEOUT
        + TERMINAL_GATE_CONTROL_BUDGET_SECONDS
        + FIFO_TERMINAL_BUDGET_SECONDS
        + FILESYSTEM_TERMINAL_BUDGET_SECONDS
        + TERMINAL_MARGIN_SECONDS
    )
    sidecar_margin = SIDECAR_WORKER_TIMEOUT_SECONDS - WORKER_OWNERSHIP_SECONDS
    if WORKER_OWNERSHIP_SECONDS <= 0:
        raise ValueError("worker ownership deadline must be positive")
    if sidecar_margin < MINIMUM_SIDECAR_MARGIN_SECONDS:
        raise ValueError(
            "runner ownership deadline does not leave the required sidecar margin: "
            f"margin={sidecar_margin}s required={MINIMUM_SIDECAR_MARGIN_SECONDS}s"
        )
    if terminal_window < minimum_terminal_window:
        raise ValueError(
            "terminal lifecycle window is too short: "
            f"window={terminal_window}s required={minimum_terminal_window}s"
        )
    owner_deadline = started + WORKER_OWNERSHIP_SECONDS
    work_deadline = owner_deadline - TEARDOWN_RESERVE_SECONDS
    terminal_deadline = owner_deadline - OWNER_DEADLINE_GUARD_SECONDS
    if work_deadline <= started or terminal_deadline <= work_deadline:
        raise ValueError("invalid transcription lifecycle deadline configuration")
    return owner_deadline, work_deadline, terminal_deadline


_CHILD_WATCHDOG_CODE = r"""
import os
import select
import signal
import sys

lifeline = int(sys.argv[1])
registration = int(sys.argv[2])
acknowledgment = int(sys.argv[3])
pgid = None
while pgid is None:
    readable, _, _ = select.select([lifeline, registration], [], [])
    if registration in readable:
        payload = os.read(registration, 64)
        if not payload:
            raise SystemExit(70)
        pgid = int(payload.strip())
        os.write(acknowledgment, b"1")
        os.close(registration)
        os.close(acknowledgment)
    if lifeline in readable and os.read(lifeline, 1) == b"":
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise SystemExit(0)
while True:
    readable, _, _ = select.select([lifeline], [], [])
    if lifeline in readable and os.read(lifeline, 1) == b"":
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise SystemExit(0)
"""


def _prepare_child_process(
    parent_pid: int,
    registration_write: int,
    acknowledgment_read: int,
) -> None:
    try:
        os.setsid()
        if _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(126)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
        os.write(registration_write, f"{os.getpid()}\n".encode())
        _LIBC.alarm(5)
        acknowledgment = os.read(acknowledgment_read, 1)
        _LIBC.alarm(0)
        if acknowledgment != b"1":
            os._exit(126)
        os.close(registration_write)
        os.close(acknowledgment_read)
    except BaseException:
        os._exit(126)


def _start_child_watchdog() -> tuple[int, int]:
    global _watchdog, _watchdog_lifeline_write
    lifeline_read, lifeline_write = os.pipe2(os.O_CLOEXEC)
    registration_read, registration_write = os.pipe2(os.O_CLOEXEC)
    acknowledgment_read, acknowledgment_write = os.pipe2(os.O_CLOEXEC)
    try:
        _watchdog = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_WATCHDOG_CODE,
                str(lifeline_read),
                str(registration_read),
                str(acknowledgment_write),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(lifeline_read, registration_read, acknowledgment_write),
            start_new_session=True,
        )
    except BaseException:
        for descriptor in (
            lifeline_read,
            lifeline_write,
            registration_read,
            registration_write,
            acknowledgment_read,
            acknowledgment_write,
        ):
            os.close(descriptor)
        raise
    os.close(lifeline_read)
    os.close(registration_read)
    os.close(acknowledgment_write)
    _watchdog_lifeline_write = lifeline_write
    return registration_write, acknowledgment_read


def _stop_child_watchdog(
    job: Path,
    *,
    lifecycle_deadline: float | None = None,
) -> list[str]:
    global _watchdog, _watchdog_lifeline_write
    errors: list[str] = []
    if _watchdog_lifeline_write is not None:
        try:
            os.close(_watchdog_lifeline_write)
        except BaseException as exc:
            errors.append(f"watchdog lifeline close failed: {type(exc).__name__}: {exc}")
        _watchdog_lifeline_write = None
    watchdog = _watchdog
    _watchdog = None
    if watchdog is not None:
        timeout = 10.0
        if lifecycle_deadline is not None:
            try:
                timeout = min(
                    timeout,
                    _remaining_seconds(lifecycle_deadline, "child watchdog reap"),
                )
            except TimeoutError as exc:
                errors.append(str(exc))
                timeout = 0.0
        try:
            watchdog.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                watchdog.kill()
            except ProcessLookupError:
                pass
            except BaseException as exc:
                errors.append(f"watchdog SIGKILL failed: {type(exc).__name__}: {exc}")
            try:
                watchdog.wait(timeout=2)
            except BaseException as exc:
                errors.append(f"watchdog reap failed: {type(exc).__name__}: {exc}")
        except BaseException as exc:
            errors.append(f"watchdog wait failed: {type(exc).__name__}: {exc}")
    for error in errors:
        _append_event_best_effort(job, "child_watchdog_error", error=error)
    return errors


def _wait_for_child_until(child: subprocess.Popen[Any], deadline: float) -> int:
    while True:
        returncode = child.poll()
        if returncode is not None:
            return returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            returncode = child.poll()
            if returncode is not None:
                return returncode
            raise TimeoutError(
                f"child exceeded its finite {MAX_RUNTIME_SECONDS}-second runtime budget"
            )
        time.sleep(min(0.5, remaining))


def _lock_exclusive_until(handle: Any, deadline: float | None, operation: str) -> None:
    if deadline is None:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return
    while True:
        _remaining_seconds(deadline, operation)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            _remaining_seconds(deadline, operation)
            time.sleep(POLL_SECONDS)


def _process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = value.rfind(")")
        fields = value[closing + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _process_identity_alive(pid: Any, start_ticks: Any) -> bool:
    if not isinstance(pid, int) or not isinstance(start_ticks, int):
        return False
    return _process_start_ticks(pid) == start_ticks


def _cleanup_stale_admission_locked() -> list[str]:
    removed: list[str] = []
    for ticket in ADMISSION_ROOT.glob("*.ticket.json"):
        record = _read_json(ticket)
        if record is not None and _process_identity_alive(
            record.get("pid"), record.get("start_ticks")
        ):
            continue
        ticket.unlink(missing_ok=True)
        removed.append(ticket.name)

    owner_path = ADMISSION_ROOT / "owner.json"
    owner = _read_json(owner_path)
    if owner is not None and not _process_identity_alive(
        owner.get("pid"), owner.get("start_ticks")
    ):
        owner_path.unlink(missing_ok=True)
        removed.append(owner_path.name)
    elif owner is None and owner_path.exists():
        owner_path.unlink(missing_ok=True)
        removed.append(owner_path.name)
    return removed


def _mark_admission_failed_closed(job_id: str, reason: str) -> bool:
    ADMISSION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ADMISSION_ROOT, 0o700)
    return _atomic_json_no_replace(
        ADMISSION_FAIL_CLOSED,
        {
            "job_id": job_id,
            "reason": reason,
            "failed_closed_at": _now(),
        },
    )


def _mark_admission_failed_closed_best_effort(
    job: Path,
    job_id: str,
    reason: str,
) -> bool:
    try:
        created = _mark_admission_failed_closed(job_id, reason)
    except BaseException as exc:
        _append_event_best_effort(
            job,
            "admission_fail_closed_write_failed",
            error=f"{type(exc).__name__}: {exc}",
            reason=reason,
        )
        return False
    event = "admission_failed_closed" if created else "admission_fail_closed_preserved"
    _append_event_best_effort(job, event, reason=reason)
    return True


def _raise_if_admission_failed_closed() -> None:
    value = _read_json(ADMISSION_FAIL_CLOSED)
    if value is not None:
        raise RuntimeError(
            "GPU transcription admission is fail-closed pending cleanup reconciliation: "
            f"{value.get('reason', 'unknown reason')}"
        )
    if ADMISSION_FAIL_CLOSED.exists() or ADMISSION_FAIL_CLOSED.is_symlink():
        raise RuntimeError("GPU transcription admission is fail-closed")

    release_guard = _read_json(ADMISSION_RELEASE_GUARD)
    if release_guard is not None:
        raise RuntimeError(
            "GPU transcription admission has an incomplete release pending cleanup "
            f"reconciliation: {release_guard.get('job_id', 'unknown job')}"
        )
    if ADMISSION_RELEASE_GUARD.exists() or ADMISSION_RELEASE_GUARD.is_symlink():
        raise RuntimeError("GPU transcription admission has an incomplete release")


def reconcile_admission_fail_closed(
    *,
    expected_job_id: str,
    lock_path: Path,
    ssh_target: str,
) -> dict[str, Any]:
    """Explicitly clear fail-closed admission after proving all GPU state clean."""
    if not JOB_ID_RE.fullmatch(expected_job_id):
        raise ValueError(f"invalid expected job_id: {expected_job_id}")

    ADMISSION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ADMISSION_ROOT, 0o700)
    mutex_path = ADMISSION_ROOT / "queue.mutex"
    with mutex_path.open("a+") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        try:
            evidence: dict[str, dict[str, Any]] = {}
            for name, path in (
                ("fail_closed", ADMISSION_FAIL_CLOSED),
                ("release_guard", ADMISSION_RELEASE_GUARD),
            ):
                value = _read_json(path)
                if value is None:
                    if path.exists() or path.is_symlink():
                        raise RuntimeError(f"{name} admission evidence is unreadable")
                    continue
                evidence[name] = value
            if not evidence:
                return {"cleared": False, "reason": "no fail-closed evidence"}

            mismatched = {
                name: value.get("job_id")
                for name, value in evidence.items()
                if value.get("job_id") != expected_job_id
            }
            if mismatched:
                raise RuntimeError(
                    "admission evidence job mismatch: "
                    f"expected {expected_job_id}, found {mismatched}"
                )

            stale_removed = _cleanup_stale_admission_locked()
            tickets = sorted(path.name for path in ADMISSION_ROOT.glob("*.ticket.json"))
            owner = _read_json(ADMISSION_ROOT / "owner.json")
            if tickets or owner is not None:
                raise RuntimeError(
                    f"FIFO state is not clean: tickets={tickets}, owner={owner}"
                )

            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("canonical GPU lock is still owned") from exc
                try:
                    gate = _gate_control(ssh_target, {"action": "status"})
                    demands = gate.get("demands")
                    if not isinstance(demands, dict) or demands:
                        raise RuntimeError(f"gate demands are not clean: {demands!r}")
                    gpu = _gpu_snapshot()
                    if gpu["compute_apps"]:
                        raise RuntimeError(
                            f"GPU compute processes are still present: {gpu['compute_apps']}"
                        )

                    reconciled_at = _now()
                    archive = (
                        ADMISSION_ROOT
                        / "reconciled"
                        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{expected_job_id}.json"
                    )
                    receipt = {
                        "cleared": True,
                        "reconciled_at": reconciled_at,
                        "evidence": evidence,
                        "stale_removed": stale_removed,
                        "gate": gate,
                        "gpu": gpu,
                        "lock_path": str(lock_path),
                    }
                    _atomic_json(archive, receipt)
                    for path in (ADMISSION_FAIL_CLOSED, ADMISSION_RELEASE_GUARD):
                        path.unlink(missing_ok=True)
                    dirfd = os.open(ADMISSION_ROOT, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dirfd)
                    finally:
                        os.close(dirfd)
                    return {**receipt, "archive": str(archive)}
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)


def _acquire_fifo_admission(
    job: Path,
    job_id: str,
    *,
    lifecycle_deadline: float | None = None,
) -> tuple[Any, int]:
    if lifecycle_deadline is not None:
        _remaining_seconds(lifecycle_deadline, "FIFO admission start")
    ADMISSION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ADMISSION_ROOT, 0o700)
    mutex_path = ADMISSION_ROOT / "queue.mutex"
    counter_path = ADMISSION_ROOT / "counter.json"
    admission_path = ADMISSION_ROOT / "admission.lock"
    pid = os.getpid()
    start_ticks = _process_start_ticks(pid)
    if start_ticks is None:
        raise RuntimeError("cannot establish transcription worker process identity")

    with mutex_path.open("a+") as mutex:
        _lock_exclusive_until(mutex, lifecycle_deadline, "FIFO ticket creation")
        try:
            _raise_if_admission_failed_closed()
            stale = _cleanup_stale_admission_locked()
            counter = _read_json(counter_path) or {}
            ticket = int(counter.get("next_ticket", 0)) + 1
            _atomic_json(counter_path, {"next_ticket": ticket, "updated_at": _now()})
            ticket_path = ADMISSION_ROOT / f"{ticket:020d}-{job_id}.ticket.json"
            _atomic_json(
                ticket_path,
                {
                    "ticket": ticket,
                    "job_id": job_id,
                    "pid": pid,
                    "start_ticks": start_ticks,
                    "queued_at": _now(),
                },
            )
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)

    _append_event(job, "fifo_ticket_created", ticket=ticket, stale_removed=stale)
    admission = admission_path.open("a+")
    deadline = time.monotonic() + ADMISSION_WAIT_TIMEOUT
    if lifecycle_deadline is not None:
        deadline = min(deadline, lifecycle_deadline)
    try:
        while True:
            owns_admission = False
            with mutex_path.open("a+") as mutex:
                _lock_exclusive_until(mutex, deadline, "FIFO queue inspection")
                try:
                    _raise_if_admission_failed_closed()
                    stale = _cleanup_stale_admission_locked()
                    if stale:
                        _append_event(job, "fifo_stale_removed", entries=stale)
                    waiting = sorted(ADMISSION_ROOT.glob("*.ticket.json"))
                    if not ticket_path.exists():
                        raise RuntimeError("transcription FIFO ticket disappeared")
                    if waiting and waiting[0] == ticket_path:
                        try:
                            fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            ticket_path.unlink()
                            _atomic_json(
                                ADMISSION_ROOT / "owner.json",
                                {
                                    "ticket": ticket,
                                    "job_id": job_id,
                                    "pid": pid,
                                    "start_ticks": start_ticks,
                                    "acquired_at": _now(),
                                },
                            )
                            owns_admission = True
                finally:
                    fcntl.flock(mutex, fcntl.LOCK_UN)

            if owns_admission:
                _append_event(job, "fifo_admission_acquired", ticket=ticket)
                return admission, ticket
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "transcription FIFO admission exceeded its finite lifecycle deadline"
                )
            time.sleep(0.25)
    except BaseException:
        cleanup_error: str | None = None
        try:
            with mutex_path.open("a+") as mutex:
                _lock_exclusive_until(mutex, lifecycle_deadline, "FIFO ticket cleanup")
                try:
                    ticket_path.unlink(missing_ok=True)
                finally:
                    fcntl.flock(mutex, fcntl.LOCK_UN)
        except BaseException as exc:
            cleanup_error = f"FIFO ticket cleanup failed: {type(exc).__name__}: {exc}"
            _append_event_best_effort(job, "fifo_ticket_cleanup_failed", error=cleanup_error)
            _mark_admission_failed_closed_best_effort(job, job_id, cleanup_error)
        try:
            admission.close()
        except BaseException as exc:
            close_error = f"FIFO admission close failed: {type(exc).__name__}: {exc}"
            _append_event_best_effort(job, "fifo_admission_close_failed", error=close_error)
            if cleanup_error is None:
                _mark_admission_failed_closed_best_effort(job, job_id, close_error)
        raise


def _release_fifo_admission(
    admission: Any,
    job: Path,
    job_id: str,
    ticket: int,
    *,
    lifecycle_deadline: float | None = None,
) -> None:
    mutex_path = ADMISSION_ROOT / "queue.mutex"
    owner_path = ADMISSION_ROOT / "owner.json"
    release_token = uuid.uuid4().hex
    errors: list[str] = []
    mutex: Any | None = None
    mutex_locked = False
    foreign_evidence = False

    def record_error(exc: BaseException, prefix: str = "") -> None:
        error = f"{prefix}{type(exc).__name__}: {exc}"
        errors.append(error)
        _append_event_best_effort(
            job,
            "fifo_admission_release_ambiguous",
            ticket=ticket,
            error="; ".join(errors),
        )

    try:
        mutex = mutex_path.open("a+")
        _lock_exclusive_until(mutex, lifecycle_deadline, "FIFO admission release")
        mutex_locked = True
        try:
            if ADMISSION_FAIL_CLOSED.exists() or ADMISSION_FAIL_CLOSED.is_symlink():
                foreign_evidence = True
                raise RuntimeError("FIFO admission was already fail-closed during release")
            if ADMISSION_RELEASE_GUARD.exists() or ADMISSION_RELEASE_GUARD.is_symlink():
                foreign_evidence = True
                raise RuntimeError("a prior FIFO admission release guard still exists")

            _atomic_json(
                ADMISSION_RELEASE_GUARD,
                {
                    "job_id": job_id,
                    "ticket": ticket,
                    "release_token": release_token,
                    "release_started_at": _now(),
                },
            )

            owner = _read_json(owner_path)
            expected_start_ticks = _process_start_ticks(os.getpid())
            if (
                owner is None
                or owner.get("job_id") != job_id
                or owner.get("ticket") != ticket
                or owner.get("pid") != os.getpid()
                or owner.get("start_ticks") != expected_start_ticks
            ):
                raise RuntimeError(
                    "FIFO owner identity mismatch during release: "
                    f"expected_job={job_id!r} expected_ticket={ticket} owner={owner!r}"
                )
            owner_path.unlink()
            directory = os.open(ADMISSION_ROOT, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if owner_path.exists() or owner_path.is_symlink():
                raise RuntimeError("FIFO owner record remained after release")

            fcntl.flock(admission, fcntl.LOCK_UN)
            admission.close()
            if not admission.closed:
                raise RuntimeError("FIFO admission descriptor remained open after release")

            release_guard = _read_json(ADMISSION_RELEASE_GUARD)
            if release_guard is None or release_guard.get("release_token") != release_token:
                raise RuntimeError("FIFO admission release guard identity changed")
            if ADMISSION_FAIL_CLOSED.exists() or ADMISSION_FAIL_CLOSED.is_symlink():
                raise RuntimeError("FIFO admission became fail-closed during release")
            ADMISSION_RELEASE_GUARD.unlink()
            directory = os.open(ADMISSION_ROOT, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException as exc:
            record_error(exc)
            if not foreign_evidence:
                _mark_admission_failed_closed_best_effort(
                    job,
                    job_id,
                    "; ".join(errors),
                )
            if not admission.closed:
                try:
                    admission.close()
                except BaseException as close_exc:
                    record_error(close_exc, "admission close failed: ")
                    if not foreign_evidence:
                        _mark_admission_failed_closed_best_effort(
                            job,
                            job_id,
                            "; ".join(errors),
                        )
        finally:
            if mutex_locked:
                try:
                    fcntl.flock(mutex, fcntl.LOCK_UN)
                except BaseException as exc:
                    record_error(exc, "FIFO mutex unlock failed: ")
                    _mark_admission_failed_closed_best_effort(
                        job,
                        job_id,
                        "; ".join(errors),
                    )
                mutex_locked = False
    except BaseException as exc:
        record_error(exc)
        _mark_admission_failed_closed_best_effort(
            job,
            job_id,
            "; ".join(errors),
        )
        if not admission.closed:
            try:
                admission.close()
            except BaseException as close_exc:
                record_error(close_exc, "admission close failed: ")
                _mark_admission_failed_closed_best_effort(
                    job,
                    job_id,
                    "; ".join(errors),
                )
    finally:
        if mutex is not None:
            try:
                mutex.close()
            except BaseException as exc:
                record_error(exc, "FIFO mutex close failed: ")
                _mark_admission_failed_closed_best_effort(
                    job,
                    job_id,
                    "; ".join(errors),
                )

    if errors:
        raise FifoReleaseError("; ".join(errors))

    _append_event_best_effort(job, "fifo_admission_released", ticket=ticket)


def _bounded_control_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    value = value.strip()
    return value if len(value) <= 2000 else f"...{value[-2000:]}"


def _parse_gate_envelope(action: str, stdout: bytes) -> dict[str, Any]:
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateControlTransportError(
            action,
            f"gate control {action} returned an invalid JSON envelope: {exc}",
        ) from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise GateControlTransportError(
            action,
            f"gate control {action} returned an invalid envelope",
        )
    if result["ok"] is False:
        error = result.get("error")
        if not isinstance(error, str) or not error.strip():
            raise GateControlTransportError(
                action,
                f"gate control {action} returned a rejection without an error",
            )
    return result


def _gate_control(
    ssh_target: str,
    request: dict[str, Any],
    *,
    lifecycle_deadline: float | None = None,
) -> dict[str, Any]:
    """Send a control request to the gate via the host helper."""
    action = str(request.get("action") or "unknown")
    encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode()
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        ssh_target,
        "sudo", "-n", HOST_HELPER,
    ]
    timeout = float(
        GATE_ACQUIRED_CONTROL_TIMEOUT_SECONDS
        if action == "acquired"
        else GATE_CONTROL_TIMEOUT_SECONDS
    )
    if lifecycle_deadline is not None:
        try:
            timeout = min(timeout, _remaining_seconds(lifecycle_deadline, f"gate {action}"))
        except TimeoutError as exc:
            raise GateControlTransportTimeout(action, str(exc)) from exc
    try:
        process = subprocess.run(
            command,
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded_control_output(exc.stdout)
        stderr = _bounded_control_output(exc.stderr)
        raise GateControlTransportTimeout(
            action,
            f"gate control {action} transport timed out after {timeout:.3f}s: "
            f"stdout={stdout!r} stderr={stderr!r}",
        ) from exc
    except OSError as exc:
        raise GateControlTransportError(
            action,
            f"gate control {action} transport failed: {type(exc).__name__}: {exc}",
        ) from exc
    result = _parse_gate_envelope(action, process.stdout)
    if result["ok"] is False:
        if process.returncode == 1:
            raise GateControlRejected(
                action,
                f"gate control {action} rejected: {result['error']}",
            )
        stderr = _bounded_control_output(process.stderr)
        raise GateControlTransportError(
            action,
            f"gate control {action} helper failed rc={process.returncode}: "
            f"error={result['error']!r} stderr={stderr!r}",
        )
    if process.returncode != 0:
        stderr = _bounded_control_output(process.stderr)
        raise GateControlTransportError(
            action,
            f"gate control {action} returned success with helper rc={process.returncode}: "
            f"stderr={stderr!r}",
        )
    state = result.get("state")
    if not isinstance(state, dict):
        raise GateControlTransportError(
            action,
            f"gate control {action} returned no state object",
        )
    return state


def _gpu_snapshot(*, lifecycle_deadline: float | None = None) -> dict[str, Any]:
    """Capture GPU state: memory, compute apps, power (read-only)."""
    gpu_timeout = 15.0
    if lifecycle_deadline is not None:
        gpu_timeout = min(
            gpu_timeout,
            _remaining_seconds(lifecycle_deadline, "GPU summary query"),
        )
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.free,utilization.gpu,power.draw,power.limit,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=gpu_timeout, check=False,
    )
    if gpu.returncode != 0 or not gpu.stdout.strip():
        raise RuntimeError(f"nvidia-smi GPU query failed: {gpu.stderr.strip()}")
    values = [v.strip() for v in gpu.stdout.strip().splitlines()[0].split(",")]

    apps_timeout = 15.0
    if lifecycle_deadline is not None:
        apps_timeout = min(
            apps_timeout,
            _remaining_seconds(lifecycle_deadline, "GPU process query"),
        )
    apps = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=apps_timeout, check=False,
    )
    if apps.returncode != 0:
        raise RuntimeError(f"nvidia-smi process query failed: {apps.stderr.strip()}")

    compute_apps = []
    for line in apps.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            compute_apps.append({
                "pid": int(parts[0]),
                "name": parts[1],
                "used_memory_mib": int(parts[2]),
            })

    return {
        "at": _now(),
        "memory_used_mib": int(float(values[0])),
        "memory_free_mib": int(float(values[1])),
        "utilization_gpu_percent": int(float(values[2])),
        "power_draw_w": float(values[3]),
        "power_limit_w": float(values[4]),
        "temperature_c": int(float(values[5])),
        "compute_apps": compute_apps,
    }


def _verify_child_cleanup(
    job: Path,
    child_pid: int,
    baseline: dict[str, Any],
    *,
    lifecycle_deadline: float | None = None,
) -> dict[str, Any]:
    """Verify child process and GPU resources are fully released."""
    deadline = time.monotonic() + GPU_CLEANUP_TIMEOUT
    if lifecycle_deadline is not None:
        deadline = min(deadline, lifecycle_deadline)
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        # Check child PID gone
        if Path(f"/proc/{child_pid}").exists():
            time.sleep(GPU_CLEANUP_INTERVAL)
            continue

        last = _gpu_snapshot(lifecycle_deadline=deadline)
        _append_event(job, "gpu_cleanup_sample", snapshot=last)

        # The child PID must be absent from CUDA, regardless of process name.
        forbidden = [
            app for app in last["compute_apps"]
            if app["pid"] == child_pid
            or any(
                kw in app["name"].lower()
                for kw in ("whisper", "ctranslate", "faster")
            )
        ]
        if forbidden:
            time.sleep(GPU_CLEANUP_INTERVAL)
            continue

        # Memory returned to baseline within tolerance
        baseline_mib = baseline["memory_used_mib"]
        if last["memory_used_mib"] <= baseline_mib + GPU_CLEANUP_TOLERANCE_MIB:
            return last

        time.sleep(GPU_CLEANUP_INTERVAL)

    raise RuntimeError(
        f"GPU cleanup not verified within its finite lifecycle deadline: last={last}"
    )


def _signal_handler(signum: int, _frame: Any) -> None:
    global _interrupted
    first_signal = _interrupted is None
    if first_signal:
        _interrupted = signal.Signals(signum).name
        if _child is not None and _child.poll() is None:
            try:
                os.killpg(os.getpgid(_child.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if _cleanup_in_progress or not first_signal:
        return
    raise InterruptedError(f"received {_interrupted}")


def _terminate_child(
    job: Path,
    *,
    lifecycle_deadline: float | None = None,
) -> list[str]:
    global _child
    child = _child
    errors: list[str] = []
    if child is None:
        return errors
    try:
        child_status = child.poll()
    except BaseException as exc:
        errors.append(f"child status failed: {type(exc).__name__}: {exc}")
        child_status = None
    if child_status is not None:
        return errors

    _append_event_best_effort(job, "child_terminate_requested", pid=child.pid)
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except BaseException as exc:
        errors.append(f"SIGTERM failed: {type(exc).__name__}: {exc}")

    term_timeout = 15.0
    if lifecycle_deadline is not None:
        try:
            term_timeout = min(
                term_timeout,
                _remaining_seconds(lifecycle_deadline, "child TERM wait"),
            )
        except TimeoutError as exc:
            errors.append(str(exc))
            term_timeout = 0.0
    try:
        child.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        _append_event_best_effort(job, "child_kill_requested", pid=child.pid)
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except BaseException as exc:
            errors.append(f"SIGKILL failed: {type(exc).__name__}: {exc}")
        kill_timeout = 15.0
        if lifecycle_deadline is not None:
            try:
                kill_timeout = min(
                    kill_timeout,
                    _remaining_seconds(lifecycle_deadline, "child KILL wait"),
                )
            except TimeoutError as exc:
                errors.append(str(exc))
                kill_timeout = 0.0
        try:
            child.wait(timeout=kill_timeout)
        except BaseException as exc:
            errors.append(f"child reap failed: {type(exc).__name__}: {exc}")
    except BaseException as exc:
        errors.append(f"child TERM wait failed: {type(exc).__name__}: {exc}")

    for error in errors:
        _append_event_best_effort(job, "child_termination_error", error=error)
    return errors


def run_lease(
    *,
    command: list[str],
    job_id: str,
    lock_path: Path,
    ssh_target: str,
    jobs_dir: Path,
    temp_dir_parent: Path | None = None,
    extra_request: dict[str, Any] | None = None,
    child_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a GPU workload through the full r4 lease lifecycle.

    Args:
        command: argv for the child subprocess (no shell)
        job_id: unique job identifier matching JOB_ID_RE
        lock_path: canonical GPU lock file path
        ssh_target: SSH target for gate control (e.g. breedoon@host.docker.internal)
        jobs_dir: directory for per-job durable state
        temp_dir_parent: parent for per-job temp directory (default: inside job dir)
        extra_request: additional fields for the request.json record
        child_env: environment overrides for the workload subprocess

    Returns:
        result dict with lifecycle receipts
    """
    global _child, _watchdog, _watchdog_lifeline_write
    global _interrupted, _cleanup_in_progress

    started = _PROCESS_STARTED_MONOTONIC
    _child = None
    _watchdog = None
    _watchdog_lifeline_write = None
    _interrupted = None
    _cleanup_in_progress = False

    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"invalid job_id: {job_id}")
    _raise_if_production_disabled()
    owner_deadline, work_deadline, terminal_deadline = _lifecycle_deadlines(started)

    jobs_dir.mkdir(parents=True, exist_ok=True)
    job = jobs_dir / job_id
    job.mkdir(mode=0o700)
    temp_dir = (temp_dir_parent or job) / "tmp"
    temp_dir.mkdir(mode=0o700)

    request_record = {
        "job_id": job_id,
        "created_at": _now(),
        "command": command,
        "lock_path": str(lock_path),
        "ssh_target": ssh_target,
        **(extra_request or {}),
    }
    _atomic_json(job / "request.json", request_record)
    _append_event(job, "job_created")
    _write_state(job, "validated", demand_registered=False, gpu_lock_acquired=False)

    demand_registered = False
    begin_attempted = False
    begin_outcome_unknown = False
    cancel_attempted = False
    admission: Any | None = None
    admission_ticket: int | None = None
    fifo_release_attempted = False
    fifo_release_verified = False
    acquired = False
    cleanup_verified = False
    lock_released_at: str | None = None
    release_attempted = False
    child_returncode: int | None = None
    child_pid: int | None = None
    baseline: dict[str, Any] | None = None
    failure: str | None = None
    failure_kind: str | None = None

    handled_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_signal_handlers = {sig: signal.getsignal(sig) for sig in handled_signals}
    for sig in handled_signals:
        signal.signal(sig, _signal_handler)

    def control(
        action: str,
        *,
        lifecycle_deadline: float = work_deadline,
        **extra: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action, **extra}
        if action != "status" and action != "recover":
            payload["job_id"] = job_id
        try:
            return _gate_control(
                ssh_target,
                payload,
                lifecycle_deadline=lifecycle_deadline,
            )
        except GateControlRejected as exc:
            _append_event_best_effort(
                job,
                "gate_control_rejected",
                action=action,
                error=str(exc),
            )
            raise
        except GateControlTransportTimeout as exc:
            _append_event_best_effort(
                job,
                "gate_control_transport_timeout",
                action=action,
                error=str(exc),
            )
            raise
        except GateControlTransportError as exc:
            _append_event_best_effort(
                job,
                "gate_control_transport_failed",
                action=action,
                error=str(exc),
            )
            raise

    def release_fifo_once() -> None:
        nonlocal admission, admission_ticket
        nonlocal fifo_release_attempted, fifo_release_verified
        if admission is None or admission_ticket is None:
            return
        held_admission = admission
        held_ticket = admission_ticket
        admission = None
        admission_ticket = None
        fifo_release_attempted = True
        _release_fifo_admission(
            held_admission,
            job,
            job_id,
            held_ticket,
            lifecycle_deadline=terminal_deadline,
        )
        fifo_release_verified = True

    with lock_path.open("a+") as lock:
        lock_stat = os.fstat(lock.fileno())
        lock_dev = lock_stat.st_dev
        lock_ino = lock_stat.st_ino
        _write_state(job, "registering_demand", lock_dev=lock_dev, lock_ino=lock_ino)

        try:
            # Step 1: begin. A transport failure is ambiguous because the gate
            # may have persisted demand before the response path failed.
            waiting_since = _now()
            begin_attempted = True
            try:
                gate = control(
                    "begin",
                    worker_pid=os.getpid(),
                    waiting_since=waiting_since,
                    lock_dev=lock_dev,
                    lock_ino=lock_ino,
                )
            except GateControlTransportError:
                begin_outcome_unknown = True
                raise
            demand_registered = True
            _append_event(job, "begin_acknowledged", gate=gate)
            _write_state(
                job, "waiting_for_admission",
                demand_registered=True, waiting_since=waiting_since,
            )

            # Step 2: deterministic FIFO admission. Every waiter has already
            # registered semantic demand, so the gate remains draining across
            # request handoff and Qwen cannot barge between queued jobs.
            admission, admission_ticket = _acquire_fifo_admission(
                job,
                job_id,
                lifecycle_deadline=work_deadline,
            )
            _write_state(
                job,
                "waiting_for_gpu",
                demand_registered=True,
                fifo_ticket=admission_ticket,
            )

            # Step 3: bounded canonical flock wait until active Qwen finishes
            # and the coordinator releases its GPU ownership.
            wait_deadline = min(
                time.monotonic() + GPU_WAIT_TIMEOUT,
                work_deadline,
            )
            while True:
                _remaining_seconds(wait_deadline, "canonical GPU lock acquisition")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= wait_deadline:
                        raise TimeoutError(
                            "GPU lock unavailable before the finite lifecycle work deadline"
                        )
                    time.sleep(0.25)
            acquired = True
            acquired_at = _now()
            _append_event(
                job, "gpu_lock_acquired",
                acquired_at=acquired_at,
                lock_dev=lock_dev, lock_ino=lock_ino,
            )

            # Step 3: acknowledge acquisition once. The gate owns the bounded
            # wait for WAITING_EXTERNAL; the transport timeout exceeds that wait.
            gate = control(
                "acquired",
                worker_pid=os.getpid(),
                acquired_at=acquired_at,
                lock_dev=lock_dev,
                lock_ino=lock_ino,
            )
            _append_event(job, "acquired_acknowledged", gate=gate)
            _write_state(
                job, "running",
                gpu_lock_acquired=True, handoff_acquired=True, gate=gate,
            )

            # Step 4: capture GPU baseline
            baseline = _gpu_snapshot(lifecycle_deadline=terminal_deadline)
            _append_event(job, "gpu_baseline_captured", snapshot=baseline)

            # Step 5: launch child subprocess
            stdout_path = job / "child.stdout.log"
            stderr_path = job / "child.stderr.log"
            remaining_for_child = _remaining_seconds(work_deadline, "child start")
            required_child_budget = MAX_RUNTIME_SECONDS + CHILD_LAUNCH_RESERVE_SECONDS
            if remaining_for_child < required_child_budget:
                raise TimeoutError(
                    "insufficient lifecycle budget to preserve the full child runtime "
                    f"of {MAX_RUNTIME_SECONDS} seconds"
                )
            _append_event(job, "child_starting", command=command)
            parent_pid = os.getpid()

            with stdout_path.open("wb") as child_stdout, \
                 stderr_path.open("wb") as child_stderr:
                registration_write, acknowledgment_read = _start_child_watchdog()
                try:
                    _child = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=child_stdout,
                        stderr=child_stderr,
                        cwd=str(Path(command[0]).parent) if command else None,
                        env={**os.environ, **(child_env or {})},
                        pass_fds=(registration_write, acknowledgment_read),
                        preexec_fn=lambda: _prepare_child_process(
                            parent_pid,
                            registration_write,
                            acknowledgment_read,
                        ),
                    )
                finally:
                    os.close(registration_write)
                    os.close(acknowledgment_read)
                child_started = time.monotonic()
                child_deadline = child_started + MAX_RUNTIME_SECONDS
                if child_deadline > work_deadline:
                    raise TimeoutError(
                        "child launch consumed its reserved lifecycle margin; "
                        "the full runtime budget was not started"
                    )
                child_pid = _child.pid
                _append_event(job, "child_started", pid=child_pid)
                child_returncode = _wait_for_child_until(_child, child_deadline)
                watchdog_errors = _stop_child_watchdog(
                    job,
                    lifecycle_deadline=terminal_deadline,
                )
                if watchdog_errors:
                    raise RuntimeError(
                        f"decoder descendant watchdog cleanup failed: {watchdog_errors}"
                    )
            child_elapsed = round(time.monotonic() - child_started, 3)
            _append_event(
                job, "child_exited",
                pid=child_pid, returncode=child_returncode,
                elapsed_seconds=child_elapsed,
            )
            _child = None

            # Step 6: verify cleanup
            cleanup_snapshot = _verify_child_cleanup(
                job,
                child_pid,
                baseline,
                lifecycle_deadline=terminal_deadline,
            )
            cleanup_verified = True
            _append_event(job, "cleanup_verified", snapshot=cleanup_snapshot)

            # Remove temp before unlock
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=False)
                _append_event(job, "temp_removed", path=str(temp_dir))

            if child_returncode != 0:
                stderr_tail = ""
                try:
                    stderr_tail = stderr_path.read_text(
                        encoding="utf-8", errors="replace"
                    )[-2000:]
                except OSError:
                    pass
                raise RuntimeError(
                    f"child exited with code {child_returncode}: "
                    f"{stderr_tail.strip()}"
                )

            # Step 7: unlock
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock_released_at = _now()
            _append_event(job, "gpu_lock_released", released_at=lock_released_at)

            # Step 8: release exactly once. An ambiguous control response is
            # reconciled manually under fail-closed admission, never retried.
            release_attempted = True
            gate = control(
                "release",
                lifecycle_deadline=terminal_deadline,
                worker_pid=os.getpid(),
                released_at=lock_released_at,
                lock_dev=lock_dev,
                lock_ino=lock_ino,
            )
            demand_registered = False
            acquired = False
            _append_event(job, "release_acknowledged", gate=gate)

            # FIFO cleanup is part of the success predicate. Failure raises,
            # writes durable fail-closed evidence, and prevents publication.
            release_fifo_once()

            total_elapsed = round(time.monotonic() - started, 3)
            result = {
                "ok": True,
                "job_id": job_id,
                "child_returncode": child_returncode,
                "child_elapsed_seconds": child_elapsed,
                "total_elapsed_seconds": total_elapsed,
                "gpu_baseline": baseline,
                "gpu_cleanup": cleanup_snapshot,
                "lock_released_at": lock_released_at,
                "fifo_release_verified": fifo_release_verified,
                "gate": gate,
            }
            _atomic_json(job / "result.json", result)
            _write_state(
                job, "succeeded",
                demand_registered=False, gpu_lock_acquired=False,
                finished_at=_now(),
            )
            return result

        except BaseException as exc:
            _cleanup_in_progress = True
            failure = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, GateControlRejected):
                failure_kind = "gate_rejected"
            elif isinstance(exc, FifoReleaseError):
                failure_kind = "fifo_release_failed"
            elif isinstance(exc, ProductionTranscriptionDisabled):
                failure_kind = "production_disabled"
            elif isinstance(exc, GateControlTransportTimeout):
                failure_kind = "gate_transport_timeout"
            elif isinstance(exc, GateControlTransportError):
                failure_kind = "gate_transport_error"
            elif isinstance(exc, InterruptedError):
                failure_kind = "interrupted"
            elif isinstance(exc, TimeoutError):
                failure_kind = "lifecycle_timeout"
            else:
                failure_kind = "runtime_error"
            _append_event_best_effort(
                job,
                "job_failure",
                error=failure,
                failure_kind=failure_kind,
                gate_action=getattr(exc, "action", None),
                interrupted=_interrupted,
            )

            termination_errors = _terminate_child(
                job,
                lifecycle_deadline=terminal_deadline,
            )
            termination_errors.extend(
                _stop_child_watchdog(
                    job,
                    lifecycle_deadline=terminal_deadline,
                )
            )
            _child = None

            if acquired and not cleanup_verified:
                try:
                    if child_pid is None or baseline is None:
                        cleanup = _gpu_snapshot(lifecycle_deadline=terminal_deadline)
                    else:
                        cleanup = _verify_child_cleanup(
                            job,
                            child_pid,
                            baseline,
                            lifecycle_deadline=terminal_deadline,
                        )
                    cleanup_verified = True
                    _append_event_best_effort(
                        job,
                        "failure_cleanup_verified",
                        snapshot=cleanup,
                    )
                except BaseException as cleanup_exc:
                    _append_event_best_effort(
                        job,
                        "failure_cleanup_ambiguous",
                        error=f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                    )

            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    _append_event_best_effort(
                        job,
                        "temp_removed_after_failure",
                        path=str(temp_dir),
                    )
            except BaseException as temp_exc:
                _append_event_best_effort(
                    job,
                    "temp_cleanup_failed",
                    error=f"{type(temp_exc).__name__}: {temp_exc}",
                )

            if acquired:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    lock_released_at = _now()
                    _append_event_best_effort(
                        job,
                        "gpu_lock_released_after_failure",
                        released_at=lock_released_at,
                    )
                except BaseException as unlock_exc:
                    _append_event_best_effort(
                        job,
                        "gpu_lock_release_ambiguous",
                        error=f"{type(unlock_exc).__name__}: {unlock_exc}",
                    )

                if cleanup_verified and lock_released_at is not None:
                    if release_attempted:
                        release_error = f"release response ambiguous; not retried: {failure}"
                        _append_event_best_effort(
                            job,
                            "failure_release_not_retried",
                            error=release_error,
                        )
                        _mark_admission_failed_closed_best_effort(
                            job,
                            job_id,
                            release_error,
                        )
                    else:
                        try:
                            release_attempted = True
                            gate = control(
                                "release",
                                lifecycle_deadline=terminal_deadline,
                                worker_pid=os.getpid(),
                                released_at=lock_released_at,
                                lock_dev=lock_dev,
                                lock_ino=lock_ino,
                            )
                            demand_registered = False
                            acquired = False
                            _append_event_best_effort(
                                job,
                                "failure_release_acknowledged",
                                gate=gate,
                            )
                        except BaseException as release_exc:
                            release_error = (
                                f"{type(release_exc).__name__}: {release_exc}"
                            )
                            _append_event_best_effort(
                                job,
                                "failure_release_failed",
                                error=release_error,
                            )
                            _mark_admission_failed_closed_best_effort(
                                job,
                                job_id,
                                release_error,
                            )
                else:
                    failed_closed_reason = (
                        f"{failure}; cleanup_verified={cleanup_verified}; "
                        f"lock_released={lock_released_at is not None}; "
                        f"termination_errors={termination_errors}"
                    )
                    _mark_admission_failed_closed_best_effort(
                        job,
                        job_id,
                        failed_closed_reason,
                    )

                state_phase = "failed_released" if not demand_registered else "failed_closed"
                _write_state_best_effort(
                    job,
                    state_phase,
                    demand_registered=demand_registered,
                    gpu_lock_acquired=lock_released_at is None,
                    cleanup_verified=cleanup_verified,
                    local_recovery_required=demand_registered,
                    error=failure,
                    failure_kind=failure_kind,
                    child_returncode=child_returncode,
                    interrupted=_interrupted,
                    failed_at=_now(),
                )

            elif demand_registered:
                try:
                    if cancel_attempted:
                        raise RuntimeError("cancel was already attempted")
                    cancel_attempted = True
                    gate = control(
                        "cancel",
                        lifecycle_deadline=terminal_deadline,
                        worker_pid=os.getpid(),
                        released_at=_now(),
                        lock_dev=lock_dev,
                        lock_ino=lock_ino,
                    )
                    _append_event_best_effort(
                        job,
                        "unacquired_demand_cancelled",
                        gate=gate,
                    )
                    demand_registered = False
                except BaseException as cancel_exc:
                    cancel_error = f"{type(cancel_exc).__name__}: {cancel_exc}"
                    _append_event_best_effort(
                        job,
                        "unacquired_demand_cancel_failed",
                        error=cancel_error,
                    )
                    _mark_admission_failed_closed_best_effort(
                        job,
                        job_id,
                        cancel_error,
                    )
                _write_state_best_effort(
                    job,
                    "failed_before_acquisition" if not demand_registered else "failed_closed",
                    demand_registered=demand_registered,
                    gpu_lock_acquired=False,
                    local_recovery_required=demand_registered,
                    error=failure,
                    failure_kind=failure_kind,
                    interrupted=_interrupted,
                    failed_at=_now(),
                )

            elif isinstance(exc, FifoReleaseError):
                _write_state_best_effort(
                    job,
                    "failed_closed",
                    demand_registered=False,
                    gpu_lock_acquired=False,
                    fifo_release_attempted=fifo_release_attempted,
                    fifo_release_verified=False,
                    local_recovery_required=True,
                    error=failure,
                    failure_kind=failure_kind,
                    failed_at=_now(),
                )

            elif begin_attempted and begin_outcome_unknown:
                ambiguous_reason: str | None = None
                try:
                    gate_status = control(
                        "status",
                        lifecycle_deadline=terminal_deadline,
                    )
                except BaseException as status_exc:
                    ambiguous_reason = (
                        "ambiguous begin could not be reconciled by status: "
                        f"{type(status_exc).__name__}: {status_exc}"
                    )
                else:
                    demands = gate_status.get("demands")
                    if isinstance(demands, dict) and job_id in demands:
                        demand_registered = True
                        _append_event_best_effort(
                            job,
                            "ambiguous_begin_found",
                            gate=gate_status,
                        )
                        try:
                            cancel_attempted = True
                            gate = control(
                                "cancel",
                                lifecycle_deadline=terminal_deadline,
                                worker_pid=os.getpid(),
                                released_at=_now(),
                                lock_dev=lock_dev,
                                lock_ino=lock_ino,
                            )
                            demand_registered = False
                            _append_event_best_effort(
                                job,
                                "ambiguous_begin_cancelled",
                                gate=gate,
                            )
                        except BaseException as cancel_exc:
                            ambiguous_reason = (
                                "ambiguous begin demand cancel failed: "
                                f"{type(cancel_exc).__name__}: {cancel_exc}"
                            )
                    else:
                        ambiguous_reason = (
                            "ambiguous begin was not present in authoritative gate status"
                        )

                if ambiguous_reason is not None:
                    _append_event_best_effort(
                        job,
                        "ambiguous_begin_failed_closed",
                        error=ambiguous_reason,
                    )
                    _mark_admission_failed_closed_best_effort(
                        job,
                        job_id,
                        ambiguous_reason,
                    )
                _write_state_best_effort(
                    job,
                    "failed_before_acquisition"
                    if not demand_registered and ambiguous_reason is None
                    else "failed_closed",
                    demand_registered=demand_registered,
                    gpu_lock_acquired=False,
                    local_recovery_required=ambiguous_reason is not None,
                    begin_outcome_unknown=True,
                    cancel_attempted=cancel_attempted,
                    error=failure,
                    failure_kind=failure_kind,
                    failed_at=_now(),
                )

            else:
                _write_state_best_effort(
                    job,
                    "failed_before_registration",
                    demand_registered=False,
                    gpu_lock_acquired=False,
                    error=failure,
                    failure_kind=failure_kind,
                    failed_at=_now(),
                )

            result = {
                "ok": False,
                "job_id": job_id,
                "error": failure,
                "failure_kind": failure_kind,
                "gate_action": getattr(exc, "action", None),
                "child_returncode": child_returncode,
                "cleanup_verified": cleanup_verified,
                "demand_registered": demand_registered,
                "begin_attempted": begin_attempted,
                "begin_outcome_unknown": begin_outcome_unknown,
                "cancel_attempted": cancel_attempted,
                "release_attempted": release_attempted,
                "fifo_release_attempted": fifo_release_attempted,
                "fifo_release_verified": fifo_release_verified,
                "termination_errors": termination_errors,
            }
            try:
                _atomic_json(job / "result.json", result)
            except BaseException as result_exc:
                _append_event_best_effort(
                    job,
                    "result_write_failed",
                    error=f"{type(result_exc).__name__}: {result_exc}",
                )
            raise
        finally:
            _cleanup_in_progress = True
            try:
                try:
                    release_fifo_once()
                except BaseException as fifo_exc:
                    _append_event_best_effort(
                        job,
                        "fifo_release_failed_during_failure_cleanup",
                        error=f"{type(fifo_exc).__name__}: {fifo_exc}",
                    )
            finally:
                for sig, previous in previous_signal_handlers.items():
                    signal.signal(sig, previous)
                _cleanup_in_progress = False
