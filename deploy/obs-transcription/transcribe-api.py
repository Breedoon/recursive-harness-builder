#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("OBS_TRANSCRIPTION_BASE", "/workspace/runtime/transcription")).resolve()
RUNTIME_ROOT = Path("/workspace/runtime").resolve()
SCRIPT = BASE / "transcribe_worker.sh"
DEVICE = os.environ.get("OBS_TRANSCRIPTION_DEVICE", "").strip().lower()
COMPUTE_TYPE = os.environ.get("OBS_TRANSCRIPTION_COMPUTE_TYPE", "").strip().lower()
LOCK = Path(os.environ.get("OBS_TRANSCRIPTION_GPU_LOCK", ""))
MIN_FREE_MIB = int(os.environ.get("OBS_TRANSCRIPTION_GPU_MIN_FREE_MIB", "0"))
RELEASE_HOST = os.environ.get("OBS_TRANSCRIPTION_VIDEO_RELEASE_HOST", "").strip()
CPU_FALLBACK = os.environ.get("OBS_TRANSCRIPTION_ALLOW_CPU_FALLBACK", "").strip().lower()
WORKER_TIMEOUT_SECONDS = 5670
WORKER_TERM_SECONDS = 120
WORKER_KILL_SECONDS = 15
MAX_REQUEST_BYTES = 65536
READINESS_BUDGET_SECONDS = 4.0
POLL_SECONDS = 0.25
SAFE_CORRELATION_RE = re.compile(r"^[a-f0-9]{32}$")
ACTIVE_LOCK = threading.Lock()
ACTIVE_WORKERS: dict[int, subprocess.Popen[str]] = {}

if DEVICE != "cuda":
    raise RuntimeError(
        f"production transcription requires OBS_TRANSCRIPTION_DEVICE=cuda, got {DEVICE!r}"
    )
if COMPUTE_TYPE != "float16":
    raise RuntimeError(
        "production transcription requires "
        f"OBS_TRANSCRIPTION_COMPUTE_TYPE=float16, got {COMPUTE_TYPE!r}"
    )
if LOCK != Path("/workspace/gpu-coordination/rtx3090.lock"):
    raise RuntimeError(f"unexpected production GPU lock path: {LOCK}")
if MIN_FREE_MIB <= 0:
    raise RuntimeError("OBS_TRANSCRIPTION_GPU_MIN_FREE_MIB must be positive")
if not RELEASE_HOST:
    raise RuntimeError("OBS_TRANSCRIPTION_VIDEO_RELEASE_HOST is required")
if CPU_FALLBACK not in {"0", "false", "no", "off"}:
    raise RuntimeError("OBS_TRANSCRIPTION_ALLOW_CPU_FALLBACK must be disabled")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _remaining_seconds(deadline: float | None, operation: str) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"readiness deadline exhausted before {operation}")
    return remaining


def _private_diagnostic(context: str, exc: BaseException) -> None:
    print(f"{context}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


_GATE_FAILURE_MESSAGES = {
    "transport": "gate transport unavailable",
    "timeout": "gate control timed out",
    "rejected": "gate rejected status request",
    "invalid": "gate returned invalid status",
}


def _safe_gate_message(failure_kind: str) -> str:
    return _GATE_FAILURE_MESSAGES.get(failure_kind, "gate status unavailable")


_WORKER_FAILURE_MESSAGES = {
    "gate_unavailable": "GPU transcription gate unavailable; retry later",
    "admission_unavailable": "GPU transcription admission unavailable; retry later",
    "runtime_unavailable": "GPU transcription runtime unavailable; retry later",
    "worker_timeout": "GPU transcription exceeded its finite worker bound; retry later",
    "completion_invalid": "GPU transcription returned no complete transcript; retry later",
    "worker_failure": "GPU transcription worker failed; retry later",
    "invalid_request": "GPU transcription request was invalid; retry later",
    "destination_exists": "GPU transcription destination already exists; retry later",
    "internal_error": "GPU transcription request failed; retry later",
}


def _classify_worker_failure(
    returncode: int | None,
    stdout: str,
    stderr: str,
    *,
    output_exists: bool,
) -> str:
    text = f"{stdout}\n{stderr}".lower()
    if "gate control" in text or "gate socket" in text or "gate unavailable" in text:
        return "gate_unavailable"
    if "admission" in text or "demand" in text or "fifo" in text:
        return "admission_unavailable"
    if "runtime is incomplete" in text or "runtime unavailable" in text:
        return "runtime_unavailable"
    if "no complete transcript" in text or not output_exists:
        return "completion_invalid"
    if returncode == 75:
        return "worker_failure"
    return "worker_failure"


def _safe_worker_message(error_class: str) -> str:
    return _WORKER_FAILURE_MESSAGES.get(error_class, _WORKER_FAILURE_MESSAGES["worker_failure"])


def gpu_snapshot(*, lifecycle_deadline: float | None = None) -> dict[str, Any]:
    try:
        timeout = _remaining_seconds(lifecycle_deadline, "GPU snapshot")
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,power.draw,power.limit,fan.speed,"
                "memory.used,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=10.0 if timeout is None else min(10.0, timeout),
        ).strip()
        parts = [part.strip() for part in out.split(",")]
        free_mib = int(float(parts[6])) if len(parts) > 6 else None
        return {
            "ok": True,
            "name": parts[0] if len(parts) > 0 else None,
            "temperature_c": parts[1] if len(parts) > 1 else None,
            "power_draw_w": parts[2] if len(parts) > 2 else None,
            "power_limit_w": parts[3] if len(parts) > 3 else None,
            "fan_percent": parts[4] if len(parts) > 4 else None,
            "memory_used_mib": parts[5] if len(parts) > 5 else None,
            "memory_free_mib": free_mib,
            "memory_total_mib": parts[7] if len(parts) > 7 else None,
            "ready_now": free_mib is not None and free_mib >= MIN_FREE_MIB,
            "minimum_free_mib": MIN_FREE_MIB,
        }
    except TimeoutError as exc:
        _private_diagnostic("GPU snapshot deadline exhausted", exc)
        return {
            "ok": False,
            "failure_kind": "timeout",
            "error": "GPU snapshot timed out",
        }
    except subprocess.TimeoutExpired as exc:
        _private_diagnostic("GPU snapshot timed out", exc)
        return {
            "ok": False,
            "failure_kind": "timeout",
            "error": "GPU snapshot timed out",
        }
    except Exception as exc:
        _private_diagnostic("GPU snapshot failed", exc)
        return {
            "ok": False,
            "failure_kind": "transport",
            "error": "GPU snapshot unavailable",
        }


def gpu_lock_snapshot() -> dict[str, Any]:
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"path": str(LOCK), "available_now": False}
            fcntl.flock(lock, fcntl.LOCK_UN)
            return {"path": str(LOCK), "available_now": True}
    except Exception as exc:
        _private_diagnostic("GPU lock snapshot failed", exc)
        return {
            "path": str(LOCK),
            "available_now": False,
            "failure_kind": "transport",
            "error": "GPU lock status unavailable",
        }


def gate_snapshot(*, lifecycle_deadline: float | None = None) -> dict[str, Any]:
    try:
        base = str(BASE)
        if base not in sys.path:
            sys.path.insert(0, base)
        from gpu_lease_runner import (
            GateControlRejected,
            GateControlTransportError,
            GateControlTransportTimeout,
            _gate_control,
        )
    except Exception as exc:
        _private_diagnostic("gate client unavailable", exc)
        return {
            "ok": False,
            "failure_kind": "transport",
            "error": _safe_gate_message("transport"),
        }

    try:
        state = _gate_control(
            RELEASE_HOST,
            {"action": "status"},
            lifecycle_deadline=lifecycle_deadline,
        )
    except GateControlTransportTimeout as exc:
        _private_diagnostic("gate status timed out", exc)
        return {
            "ok": False,
            "failure_kind": "timeout",
            "error": _safe_gate_message("timeout"),
        }
    except GateControlRejected as exc:
        _private_diagnostic("gate status rejected", exc)
        return {
            "ok": False,
            "failure_kind": "rejected",
            "error": _safe_gate_message("rejected"),
        }
    except GateControlTransportError as exc:
        _private_diagnostic("gate status transport failed", exc)
        return {
            "ok": False,
            "failure_kind": "transport",
            "error": _safe_gate_message("transport"),
        }
    except Exception as exc:
        _private_diagnostic("gate status failed", exc)
        return {
            "ok": False,
            "failure_kind": "transport",
            "error": _safe_gate_message("transport"),
        }

    if not isinstance(state, dict):
        return {
            "ok": False,
            "failure_kind": "invalid",
            "error": _safe_gate_message("invalid"),
        }
    demands = state.get("demands")
    mode = state.get("mode")
    phase = state.get("phase")
    active_count = state.get("active_count")
    queued = state.get("queued")
    if (
        not isinstance(demands, dict)
        or not isinstance(mode, str)
        or not isinstance(phase, str)
        or type(active_count) is not int
        or active_count < 0
        or type(queued) is not int
        or queued < 0
    ):
        return {
            "ok": False,
            "failure_kind": "invalid",
            "error": _safe_gate_message("invalid"),
        }
    return {
        "ok": True,
        "phase": phase,
        "mode": mode,
        "active_count": active_count,
        "queued": queued,
        "demand_count": len(demands),
        "admission_open": (
            mode == "OPEN"
            and phase == "OPEN"
            and active_count == 0
            and queued == 0
            and not demands
        ),
    }


def _signal_worker_group(proc: subprocess.Popen[str], signal_number: int) -> None:
    try:
        os.killpg(proc.pid, signal_number)
    except ProcessLookupError:
        return
    except OSError:
        proc.send_signal(signal_number)


def _terminate_worker(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    _signal_worker_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=WORKER_TERM_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_worker_group(proc, signal.SIGKILL)
        proc.wait(timeout=WORKER_KILL_SECONDS)


def _client_disconnected(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def _regular_output_identity(path: Path) -> tuple[int, int] | None:
    try:
        output_stat = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(output_stat.st_mode):
        return None
    return output_stat.st_dev, output_stat.st_ino


def _unlink_owned_output(path: Path | None, identity: tuple[int, int] | None) -> None:
    if path is None or identity is None:
        return
    if _regular_output_identity(path) != identity:
        return
    path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self) -> None:
        if self.path not in {"/health", "/ready"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        readiness_deadline = (
            time.monotonic() + READINESS_BUDGET_SECONDS
            if self.path == "/ready"
            else None
        )
        gpu = gpu_snapshot(lifecycle_deadline=readiness_deadline)
        gpu_lock = gpu_lock_snapshot()
        script_exists = SCRIPT.is_file()
        liveness_ok = script_exists and gpu.get("ok") is True and not gpu_lock.get("error")
        gpu_ready = gpu.get("ready_now") is True and gpu_lock.get("available_now") is True
        with ACTIVE_LOCK:
            active_workers = sorted(ACTIVE_WORKERS)
        payload = {
            "ok": liveness_ok,
            "ready_now": liveness_ok and gpu_ready,
            "queue_supported": liveness_ok,
            "script_exists": script_exists,
            "script": str(SCRIPT),
            "base": str(BASE),
            "active_worker_pids": active_workers,
            "gpu": gpu,
            "gpu_lock": gpu_lock,
            "runtime_contract": {
                "device": DEVICE,
                "compute_type": COMPUTE_TYPE,
                "gpu_lock": str(LOCK),
                "minimum_free_mib": MIN_FREE_MIB,
                "release_host_configured": bool(RELEASE_HOST),
                "cpu_fallback_enabled": CPU_FALLBACK
                not in {"0", "false", "no", "off"},
                "correlation_id_required": True,
                "worker_timeout_seconds": WORKER_TIMEOUT_SECONDS,
                "readiness_budget_seconds": READINESS_BUDGET_SECONDS,
            },
        }
        if self.path == "/health":
            self._send(200 if liveness_ok else 503, payload)
            return

        gate = gate_snapshot(lifecycle_deadline=readiness_deadline)
        queue_supported = liveness_ok and gate.get("ok") is True
        ready_now = queue_supported and gpu_ready and gate.get("admission_open") is True
        if not liveness_ok:
            readiness_reason = "transcription_resources_unavailable"
        elif not queue_supported:
            readiness_reason = "gate_unavailable"
        elif ready_now:
            readiness_reason = None
        elif gate.get("admission_open") is not True:
            readiness_reason = "gate_busy"
        else:
            readiness_reason = "waiting_for_gpu"
        payload.update(
            {
                "ok": queue_supported,
                "ready_now": ready_now,
                "queue_supported": queue_supported,
                "liveness_ok": liveness_ok,
                "gate": gate,
                "readiness_reason": readiness_reason,
            }
        )
        self._send(200 if queue_supported else 503, payload)

    def do_POST(self) -> None:
        if self.path != "/transcribe":
            self._send(404, {"ok": False, "error": "not_found"})
            return

        proc: subprocess.Popen[str] | None = None
        output_path: Path | None = None
        output_identity: tuple[int, int] | None = None
        try:
            content_length = int(self.headers.get("content-length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            data = json.loads(self.rfile.read(content_length))
            audio_file = Path(str(data["audio_file"]))
            dest_dir = Path(str(data["dest_dir"]))
            title = str(data["title"])
            correlation_id = str(data["correlation_id"])
            timeout_seconds = int(data.get("timeout_seconds", 0))
            if not SAFE_CORRELATION_RE.fullmatch(correlation_id):
                raise ValueError("invalid correlation ID")
            if timeout_seconds != WORKER_TIMEOUT_SECONDS:
                raise ValueError("unexpected worker timeout")

            audio = audio_file.resolve()
            dest = dest_dir.resolve()
            if audio_file.is_symlink() or not audio.is_file() or not _within(audio, RUNTIME_ROOT):
                raise ValueError("invalid audio path")
            if dest_dir.is_symlink() or not dest.is_dir() or not _within(dest, RUNTIME_ROOT):
                raise ValueError("invalid destination path")
            output_path = dest / f"{Path(title).name}.md"
            if output_path.exists() or output_path.is_symlink():
                raise FileExistsError("transcript destination already exists")

            before = gpu_snapshot()
            started = time.monotonic()
            proc = subprocess.Popen(
                [str(SCRIPT), str(audio), title, str(dest)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(BASE),
                env={**os.environ, "OBS_TRANSCRIPTION_CORRELATION_ID": correlation_id},
                start_new_session=True,
            )
            with ACTIVE_LOCK:
                ACTIVE_WORKERS[proc.pid] = proc

            deadline = started + WORKER_TIMEOUT_SECONDS
            disconnected = False
            timed_out = False
            while proc.poll() is None:
                if _client_disconnected(self.connection):
                    disconnected = True
                    _terminate_worker(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_worker(proc)
                    break
                time.sleep(POLL_SECONDS)

            stdout, stderr = proc.communicate(timeout=1)
            elapsed = round(time.monotonic() - started, 3)
            with ACTIVE_LOCK:
                ACTIVE_WORKERS.pop(proc.pid, None)

            output_exists = (
                output_path is not None
                and output_path.is_file()
                and not output_path.is_symlink()
            )
            output_created = output_exists
            if output_exists and output_path is not None:
                output_identity = _regular_output_identity(output_path)

            if disconnected:
                _unlink_owned_output(output_path, output_identity)
                return
            if timed_out:
                _unlink_owned_output(output_path, output_identity)
                output_exists = False
                self._send(
                    504,
                    {
                        "ok": False,
                        "error_class": "worker_timeout",
                        "correlation_id": correlation_id,
                        "error": _safe_worker_message("worker_timeout"),
                        "returncode": proc.returncode,
                        "elapsed_seconds": elapsed,
                        "output_created": output_created,
                        "output_exists": output_exists,
                        "stdout_tail": stdout[-2000:],
                        "stderr_tail": stderr[-2000:],
                    },
                )
                return

            ok = proc.returncode == 0 and output_identity is not None
            error_class = None if ok else _classify_worker_failure(
                proc.returncode,
                stdout,
                stderr,
                output_exists=output_exists,
            )
            if error_class is not None:
                _unlink_owned_output(output_path, output_identity)
                output_exists = False
            after = gpu_snapshot()
            response = {
                "ok": ok,
                "returncode": proc.returncode,
                "correlation_id": correlation_id,
                "elapsed_seconds": elapsed,
                "output_path": str(output_path) if output_path is not None else None,
                "output_created": output_created,
                "output_exists": output_exists,
                "output_dev": output_identity[0] if output_identity is not None else None,
                "output_ino": output_identity[1] if output_identity is not None else None,
                "stdout_tail": stdout[-2000:],
                "stderr_tail": stderr[-2000:],
                "gpu_before": before,
                "gpu_after": after,
            }
            if error_class is not None:
                response.update(
                    {
                        "error_class": error_class,
                        "error": _safe_worker_message(error_class),
                    }
                )
            self._send(200 if ok else 500, response)
        except (BrokenPipeError, ConnectionResetError):
            if proc is not None:
                _terminate_worker(proc)
                with ACTIVE_LOCK:
                    ACTIVE_WORKERS.pop(proc.pid, None)
            _unlink_owned_output(output_path, output_identity)
        except Exception as exc:
            if proc is not None:
                _terminate_worker(proc)
                with ACTIVE_LOCK:
                    ACTIVE_WORKERS.pop(proc.pid, None)
            _unlink_owned_output(output_path, output_identity)
            _private_diagnostic("transcription request failed", exc)
            if isinstance(exc, FileExistsError):
                error_class = "destination_exists"
                status = 409
            elif isinstance(exc, ValueError):
                error_class = "invalid_request"
                status = 400
            else:
                error_class = "internal_error"
                status = 500
            try:
                self._send(
                    status,
                    {
                        "ok": False,
                        "error_class": error_class,
                        "error": _safe_worker_message(error_class),
                    },
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


if __name__ == "__main__":
    BASE.joinpath("logs").mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
