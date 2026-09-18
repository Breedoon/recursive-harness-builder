#!/usr/bin/env python3
"""Authenticated, serialized streaming gate with demand-driven Qwen GPU handoff."""

import collections
import fcntl
import hmac
import http.client
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LISTEN_HOST = os.environ.get("GATE_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("GATE_LISTEN_PORT", "8080"))
BACKEND_HOST = os.environ.get("GATE_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("GATE_BACKEND_PORT", "8081"))
QUEUE_LIMIT = int(os.environ.get("GATE_QUEUE_LIMIT", "4"))
# L6: how many requests may be in flight at once. DEFAULT 1 == the historical
# serialized behaviour, byte-for-byte. The gate was not over-cautious: it faithfully
# modelled a llama.cpp backend running --parallel 1. Raise this ONLY to match a
# backend that genuinely supports concurrency (e.g. vLLM --max-num-seqs N).
CONCURRENCY_LIMIT = max(1, int(os.environ.get("GATE_CONCURRENCY_LIMIT", "1")))
SLOT_SAMPLE_INTERVAL = float(os.environ.get("GATE_SLOT_SAMPLE_INTERVAL", "3"))
API_KEY_PATH = Path(os.environ.get("GATE_API_KEY_PATH", "/run/secrets/llama_api_key"))
CONTROL_SOCKET = Path(os.environ.get("GATE_CONTROL_SOCKET", "/workspace/gpu-coordination/llm-gate.sock"))
STATE_PATH = Path(os.environ.get("GATE_STATE_PATH", "/state/handoff-gate.json"))
GPU_LOCK_PATH = Path(os.environ.get("GATE_GPU_LOCK_PATH", "/workspace/gpu-coordination/rtx3090.lock"))
QWEN_MODEL = "local-qwen3.8-27b"
QWEN_ALIAS = "local-qwen3.8-27b"
NVIDIA_SMI = os.environ.get("GATE_NVIDIA_SMI", "/usr/bin/nvidia-smi")
NVIDIA_SMI_TIMEOUT = float(os.environ.get("GATE_NVIDIA_SMI_TIMEOUT", "15"))
# How long to wait for llama-swap to complete an unload. A healthy vLLM TP=2 unload was
# measured at 0.9 s on 2026-09-07, so 130 s is already generous; it is named rather than
# raised because the timeout firing was correct behaviour - the defect was what the gate
# did afterwards, not how long it waited.
UNLOAD_TIMEOUT = float(os.environ.get("GATE_UNLOAD_TIMEOUT", "130"))
# How often a gate latched in RECOVERY_REQUIRED re-tests reality. See try_auto_recover().
AUTO_RECOVERY_INTERVAL = float(os.environ.get("GATE_AUTO_RECOVERY_INTERVAL", "30"))
DIAGNOSTIC_OUTPUT_LIMIT = 2000
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class QwenVramAllocated(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def load_key():
    value = API_KEY_PATH.read_text().strip()
    if not value:
        raise RuntimeError("empty gate API key")
    return value


API_KEY = load_key()


class GateState:
    def __init__(self):
        self.condition = threading.Condition()
        self.mode = "OPEN"
        self.active_ids = set()
        self.queue = collections.deque()
        self.demands = {}
        self.cycle_id = None
        self.phase = "OPEN"
        self.coordinator_running = False
        self.recovery_required = False
        self.unload_attempted = False
        self.unload_attempt_count = 0
        self.unload_attempted_at = None
        self.unload_completed_at = None
        self.unload_http_status = None
        self.qwen_pid = None
        self.qwen_port = None
        self.qwen_exited_at = None
        self.qwen_vram_released_at = None
        self.slot_samples = []
        self.last_error = None
        self.updated_at = now()
        self._load()

    def _load(self):
        if not STATE_PATH.exists():
            self._persist_unlocked()
            return
        try:
            saved = json.loads(STATE_PATH.read_text())
        except Exception as exc:
            self.mode = "DRAINING"
            self.phase = "RECOVERY_REQUIRED"
            self.recovery_required = True
            self.last_error = f"state load failed: {exc}"
            self._persist_unlocked()
            return
        demands = saved.get("demands")
        if isinstance(demands, dict):
            self.demands = demands
        previous_mode = saved.get("mode")
        interrupted_traffic = bool(saved.get("active")) or int(saved.get("queued") or 0) > 0
        if previous_mode != "OPEN" or self.demands or interrupted_traffic:
            self.mode = "DRAINING"
            self.phase = "RECOVERY_REQUIRED"
            self.recovery_required = True
            self.cycle_id = saved.get("cycle_id") or uuid.uuid4().hex
            self.unload_attempted = bool(saved.get("unload_attempted"))
            self.unload_attempt_count = int(saved.get("unload_attempt_count") or 0)
            self.unload_attempted_at = saved.get("unload_attempted_at")
            self.unload_completed_at = saved.get("unload_completed_at")
            self.unload_http_status = saved.get("unload_http_status")
            self.qwen_pid = saved.get("qwen_pid")
            self.qwen_port = saved.get("qwen_port")
            self.qwen_exited_at = saved.get("qwen_exited_at")
            self.qwen_vram_released_at = saved.get("qwen_vram_released_at")
            self.slot_samples = saved.get("slot_samples") or []
            self.last_error = saved.get("last_error")
        else:
            self.mode = "OPEN"
            self.phase = "OPEN"
            self.demands = {}
        self._persist_unlocked()

    def snapshot_unlocked(self):
        return {
            "version": 1,
            "mode": self.mode,
            "phase": self.phase,
            "active": bool(self.active_ids),
            "active_count": len(self.active_ids),
            "concurrency_limit": CONCURRENCY_LIMIT,
            "queued": len(self.queue),
            "queue_limit": QUEUE_LIMIT,
            "demands": self.demands,
            "cycle_id": self.cycle_id,
            "coordinator_running": self.coordinator_running,
            "recovery_required": self.recovery_required,
            "unload_attempted": self.unload_attempted,
            "unload_attempt_count": self.unload_attempt_count,
            "unload_attempted_at": self.unload_attempted_at,
            "unload_completed_at": self.unload_completed_at,
            "unload_http_status": self.unload_http_status,
            "qwen_pid": self.qwen_pid,
            "qwen_port": self.qwen_port,
            "qwen_exited_at": self.qwen_exited_at,
            "qwen_vram_released_at": self.qwen_vram_released_at,
            "slot_samples": self.slot_samples,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    def snapshot(self):
        with self.condition:
            return self.snapshot_unlocked()

    def _persist_unlocked(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = now()
        payload = json.dumps(self.snapshot_unlocked(), indent=2, sort_keys=True) + "\n"
        temp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, STATE_PATH)

    def persist(self):
        with self.condition:
            self._persist_unlocked()

    def _open_unlocked(self):
        self.mode = "OPEN"
        self.phase = "OPEN"
        self.cycle_id = None
        self.recovery_required = False
        self.unload_attempted = False
        self.unload_attempt_count = 0
        self.unload_attempted_at = None
        self.unload_completed_at = None
        self.unload_http_status = None
        self.qwen_pid = None
        self.qwen_port = None
        self.qwen_exited_at = None
        self.qwen_vram_released_at = None
        self.slot_samples = []
        self.last_error = None

    def admit(self):
        ticket = uuid.uuid4().hex
        with self.condition:
            if self.mode != "OPEN":
                return None, "draining"
            if len(self.active_ids) < CONCURRENCY_LIMIT and not self.queue:
                self.active_ids.add(ticket)
                self._persist_unlocked()
                return ticket, None
            if len(self.queue) >= QUEUE_LIMIT:
                return None, "queue_full"
            self.queue.append(ticket)
            self._persist_unlocked()
            while True:
                self.condition.wait()
                if self.mode != "OPEN":
                    try:
                        self.queue.remove(ticket)
                    except ValueError:
                        pass
                    self._persist_unlocked()
                    return None, "draining"
                if (
                    len(self.active_ids) < CONCURRENCY_LIMIT
                    and self.queue
                    and self.queue[0] == ticket
                ):
                    self.queue.popleft()
                    self.active_ids.add(ticket)
                    self._persist_unlocked()
                    return ticket, None

    def release_request(self, ticket):
        with self.condition:
            self.active_ids.discard(ticket)
            self._persist_unlocked()
            self.condition.notify_all()

    def begin_demand(self, message):
        job_id = message.get("job_id", "")
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid job_id")
        with self.condition:
            if self.last_error is not None or self.recovery_required:
                raise ValueError("gate recovery is required before accepting demand")
            if self.mode == "OPEN":
                self.mode = "DRAINING"
                self.phase = "DRAINING"
                self.cycle_id = uuid.uuid4().hex
                self.unload_attempted = False
                self.unload_attempt_count = 0
                self.unload_attempted_at = None
                self.unload_completed_at = None
                self.unload_http_status = None
                self.qwen_pid = None
                self.qwen_port = None
                self.qwen_exited_at = None
                self.qwen_vram_released_at = None
                self.slot_samples = []
                self.last_error = None
                self.recovery_required = False
            existing = self.demands.get(job_id, {})
            self.demands[job_id] = {
                "job_id": job_id,
                "worker_pid": message.get("worker_pid"),
                "waiting_since": message.get("waiting_since"),
                "lock_dev": message.get("lock_dev"),
                "lock_ino": message.get("lock_ino"),
                "phase": existing.get("phase", "waiting_for_gpu"),
                "demand_at": existing.get("demand_at", now()),
                "last_seen_at": now(),
            }
            start = (
                not self.coordinator_running
                and not self.unload_attempted
                and self.phase == "DRAINING"
            )
            if start:
                self.coordinator_running = True
            self._persist_unlocked()
            self.condition.notify_all()
            return start, self.snapshot_unlocked()

    def acquired(self, message):
        job_id = message.get("job_id", "")
        with self.condition:
            demand = self.demands.get(job_id)
            if not demand:
                raise ValueError("unknown demand")
            expected = GPU_LOCK_PATH.stat()
            if int(message.get("lock_dev", -1)) != expected.st_dev or int(message.get("lock_ino", -1)) != expected.st_ino:
                raise ValueError("canonical lock identity mismatch")
            if self.last_error is not None or self.recovery_required:
                raise ValueError("gate recovery is required before acknowledging acquisition")
            if demand.get("phase") == "holding_gpu":
                return self.snapshot_unlocked()
            deadline = time.monotonic() + 30
            while self.phase != "WAITING_EXTERNAL":
                if self.mode != "DRAINING" or self.last_error is not None or self.recovery_required:
                    raise ValueError("gate is not ready to acknowledge external acquisition")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("timed out waiting for Qwen VRAM release verification")
                self.condition.wait(timeout=min(1, remaining))
            if self.last_error is not None or self.recovery_required:
                raise ValueError("gate recovery is required before acknowledging acquisition")
            demand["phase"] = "holding_gpu"
            demand["acquired_at"] = message.get("acquired_at") or now()
            demand["acknowledged_at"] = now()
            demand["worker_pid"] = message.get("worker_pid", demand.get("worker_pid"))
            demand["last_seen_at"] = now()
            self.phase = "EXTERNAL_HOLD"
            self._persist_unlocked()
            self.condition.notify_all()
            return self.snapshot_unlocked()

    def release_demand(self, message, cancelled=False):
        job_id = message.get("job_id", "")
        verify_external_readiness = False
        with self.condition:
            demand = self.demands.get(job_id)
            if not demand:
                raise ValueError("unknown demand")
            self.demands.pop(job_id)
            if self.last_error is not None or self.recovery_required:
                self.mode = "DRAINING"
                self.phase = "FAILED_CLOSED" if self.last_error is not None else "RECOVERY_REQUIRED"
                self.recovery_required = True
            elif self.demands:
                if any(
                    item.get("phase") == "holding_gpu" for item in self.demands.values()
                ):
                    self.phase = "EXTERNAL_HOLD"
                elif self.phase == "EXTERNAL_HOLD":
                    self.phase = "VERIFYING_NVML"
                    verify_external_readiness = True
            elif self.coordinator_running:
                self.phase = "CANCELLING"
            elif self.phase != "VERIFYING_NVML":
                self._open_unlocked()
            self._persist_unlocked()
            self.condition.notify_all()

        if verify_external_readiness:
            try:
                require_no_qwen_nvml()
            except Exception as exc:
                with self.condition:
                    self.mode = "DRAINING"
                    self.phase = "FAILED_CLOSED"
                    self.last_error = str(exc)
                    self.recovery_required = True
                    self._persist_unlocked()
                    self.condition.notify_all()
                raise
            with self.condition:
                if self.last_error is not None or self.recovery_required:
                    raise ValueError("gate recovery is required before external readiness")
                if self.demands and not any(
                    item.get("phase") == "holding_gpu" for item in self.demands.values()
                ):
                    if find_qwen_process() is not None:
                        self.mode = "DRAINING"
                        self.phase = "FAILED_CLOSED"
                        self.last_error = "Qwen appeared during external-readiness verification"
                        self.recovery_required = True
                        self._persist_unlocked()
                        self.condition.notify_all()
                        raise ValueError(self.last_error)
                    self.phase = "WAITING_EXTERNAL"
                elif not self.demands:
                    self._open_unlocked()
                self._persist_unlocked()
                self.condition.notify_all()
                return self.snapshot_unlocked()

        with self.condition:
            return self.snapshot_unlocked()

    def recover(self):
        with self.condition:
            if self.mode != "DRAINING" or not self.recovery_required:
                raise ValueError("gate is not awaiting recovery")
            if self.active_ids or self.queue or self.demands or self.coordinator_running:
                raise ValueError("gate recovery requires no active, queued, or external work")

        qwen = find_qwen_process()
        if qwen is not None:
            pid, port = qwen
            first = slot_sample(port)
            if first["is_processing"]:
                raise ValueError("Qwen is processing during recovery")
            time.sleep(SLOT_SAMPLE_INTERVAL)
            if find_qwen_process() != (pid, port):
                raise ValueError("Qwen changed during recovery sampling")
            second = slot_sample(port)
            if second["is_processing"]:
                raise ValueError("Qwen became active during recovery")
            with open(GPU_LOCK_PATH, "a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    raise ValueError("Qwen does not hold the canonical GPU lease")
            require_clean_recovery_nvml(pid)
            with self.condition:
                if self.active_ids or self.queue or self.demands or self.coordinator_running:
                    raise ValueError("work appeared during gate recovery")
                if find_qwen_process() != (pid, port):
                    raise ValueError("Qwen changed before recovery completed")
                self._open_unlocked()
                self._persist_unlocked()
                self.condition.notify_all()
                return self.snapshot_unlocked()

        with open(GPU_LOCK_PATH, "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("external workload holds the canonical GPU lease") from None
            try:
                require_clean_recovery_nvml()
                with self.condition:
                    if self.active_ids or self.queue or self.demands or self.coordinator_running:
                        raise ValueError("work appeared during gate recovery")
                    if find_qwen_process() is not None:
                        raise ValueError("Qwen appeared during gate recovery")
                    self._open_unlocked()
                    self._persist_unlocked()
                    self.condition.notify_all()
                    return self.snapshot_unlocked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


STATE = GateState()


def authenticated(headers):
    authorization = headers.get("Authorization", "")
    if authorization.startswith("Bearer ") and hmac.compare_digest(authorization[7:], API_KEY):
        return True
    x_api_key = headers.get("x-api-key", "")
    return bool(x_api_key) and hmac.compare_digest(x_api_key, API_KEY)


def find_qwen_process():
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parts = (proc / "cmdline").read_bytes().split(b"\0")
            args = [item.decode(errors="replace") for item in parts if item]
        except (OSError, PermissionError):
            continue
        if not args or Path(args[0]).name != "llama-server":
            continue
        try:
            alias_index = args.index("--alias")
            port_index = args.index("--port")
        except ValueError:
            continue
        if alias_index + 1 >= len(args) or args[alias_index + 1] != QWEN_ALIAS:
            continue
        if port_index + 1 >= len(args):
            continue
        try:
            return int(proc.name), int(args[port_index + 1])
        except ValueError:
            continue
    return None


def authenticated_json(url, timeout=10):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return response.status, json.loads(body)


def slot_sample(port):
    status, slots = authenticated_json(f"http://127.0.0.1:{port}/slots", timeout=10)
    if status != 200 or not isinstance(slots, list) or len(slots) != 1:
        raise RuntimeError("authoritative slot response is not exactly one slot")
    slot = slots[0]
    if not isinstance(slot, dict) or not isinstance(slot.get("is_processing"), bool):
        raise RuntimeError("authoritative slot response lacks boolean is_processing")
    return {
        "sampled_at": now(),
        "id": slot.get("id"),
        "n_ctx": slot.get("n_ctx"),
        "is_processing": slot["is_processing"],
        "n_prompt_tokens": slot.get("n_prompt_tokens"),
        "n_prompt_tokens_processed": slot.get("n_prompt_tokens_processed"),
        "n_prompt_tokens_cache": slot.get("n_prompt_tokens_cache"),
    }


def _bounded_diagnostic(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    value = str(value).strip()
    if len(value) <= DIAGNOSTIC_OUTPUT_LIMIT:
        return value
    return f"...{value[-DIAGNOSTIC_OUTPUT_LIMIT:]}"


def query_compute_apps():
    command = [
        NVIDIA_SMI,
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=NVIDIA_SMI_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"nvidia-smi executable not found at {NVIDIA_SMI!r}") from exc
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded_diagnostic(exc.stdout)
        stderr = _bounded_diagnostic(exc.stderr)
        raise RuntimeError(
            f"nvidia-smi process query timed out after {NVIDIA_SMI_TIMEOUT}s: "
            f"stdout={stdout!r} stderr={stderr!r}"
        ) from exc
    if process.returncode != 0:
        stdout = _bounded_diagnostic(process.stdout)
        stderr = _bounded_diagnostic(process.stderr)
        raise RuntimeError(
            f"nvidia-smi process query failed rc={process.returncode}: "
            f"stdout={stdout!r} stderr={stderr!r}"
        )

    applications = []
    unqueryable_devices = 0
    for raw_line in process.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3:
            raise RuntimeError(f"nvidia-smi returned an invalid compute row: {line!r}")
        # L7: a GPU in a fault state (e.g. Xid 154 "GPU Reset Required") is reported by
        # nvidia-smi as an all-"[N/A]" sentinel row. That row describes a device that
        # cannot be queried - it is NOT a rogue process. Failing closed on it deadlocks
        # the handoff for every tenant AND blocks the gate's own recover() path, leaving
        # no operator-reachable escape short of physically power-cycling the host.
        # Skip the sentinel, count it, and surface it. Malformed rows still raise.
        if all(part == "[N/A]" for part in parts):
            unqueryable_devices += 1
            continue
        try:
            pid = int(parts[0])
        except ValueError as exc:
            raise RuntimeError(f"nvidia-smi returned an invalid compute PID: {line!r}") from exc
        applications.append(
            {
                "pid": pid,
                "process_name": parts[1],
                "used_memory_mib": parts[2],
            }
        )
    if unqueryable_devices:
        print(
            f"{now()} gate nvidia-smi unqueryable_devices={unqueryable_devices} "
            "compute-app visibility is PARTIAL; proceeding on queryable devices",
            flush=True,
        )
    return applications


def _is_llama_server_app(application):
    return "llama-server" in application.get("process_name", "").lower()


def require_expected_qwen_nvml(pid):
    applications = query_compute_apps()
    matches = [
        application
        for application in applications
        if application.get("pid") == pid and _is_llama_server_app(application)
    ]
    # L8: one PROCESS legitimately produces one NVML compute-apps row PER GPU it
    # allocates on. Requiring exactly one row conflated "one process" with "one GPU",
    # so a multi-GPU inference process (llama.cpp with layers split across both cards,
    # or a vLLM TP=2 worker set) fails this check and deadlocks the drain. The real
    # invariant - that no OTHER llama-server is present - is enforced immediately below
    # and is unchanged.
    if not matches:
        raise RuntimeError(
            "authoritative NVML process state disagrees with the expected Qwen process: "
            f"expected_pid={pid} applications={applications}"
        )
    unexpected_llama = [
        application
        for application in applications
        if _is_llama_server_app(application) and application.get("pid") != pid
    ]
    if unexpected_llama:
        raise RuntimeError(
            f"unexpected llama-server compute processes are present: {unexpected_llama}"
        )
    return applications


def require_no_qwen_nvml():
    applications = query_compute_apps()
    llama = [application for application in applications if _is_llama_server_app(application)]
    if llama:
        raise QwenVramAllocated(f"Qwen VRAM remains allocated: {llama}")
    return applications


def require_clean_recovery_nvml(qwen_pid=None):
    applications = (
        require_expected_qwen_nvml(qwen_pid)
        if qwen_pid is not None
        else require_no_qwen_nvml()
    )
    unexpected = [
        application
        for application in applications
        if qwen_pid is None or application.get("pid") != qwen_pid
    ]
    if unexpected:
        raise RuntimeError(
            f"unexpected GPU compute processes are present during recovery: {unexpected}"
        )
    return applications


def wait_qwen_vram_released(timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            require_no_qwen_nvml()
        except QwenVramAllocated:
            pass
        else:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def backend_running_models():
    """Which models llama-swap currently has loaded, whatever engine serves them.

    Engine-agnostic by construction: it reports what the BACKEND is running, not
    what a process is named. This is the question find_qwen_process() cannot
    answer, because that one matches argv[0] against "llama-server".
    """
    request = urllib.request.Request(
        f"http://{BACKEND_HOST}:{BACKEND_PORT}/running",
        headers={"Authorization": f"Bearer {API_KEY}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode())
    running = payload.get("running")
    if not running:
        return []
    return list(running)


def unload_backend_model():
    """Ask llama-swap to unload Qwen. Returns an HTTP status, or a diagnostic string.

    L11 (2026-09-07): this only caught HTTPError, so a socket timeout - which is what
    happens when llama-swap blocks trying to stop an engine whose process is already
    dead or wedged - escaped as TimeoutError BEFORE the caller could record the attempt.
    The persisted state was then unload_attempted=True with unload_http_status=null and
    unload_completed_at=null, which is indistinguishable from "the request was never
    sent". Measured at 2026-09-07T20:30:33Z, where a vLLM engine that had died at 18:34
    was still reported by /running; the unload hung, the 130 s timeout fired, and the
    only surviving evidence was last_error="timed out".
    Returning the failure instead of raising it lets the caller persist what actually
    happened. The caller still treats any non-200 as fatal, so drain behaviour is
    unchanged - only the evidence improves.
    """
    request = urllib.request.Request(
        f"http://{BACKEND_HOST}:{BACKEND_PORT}/unload?model={QWEN_MODEL}",
        headers={"Authorization": f"Bearer {API_KEY}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=UNLOAD_TIMEOUT) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"transport failure after {UNLOAD_TIMEOUT}s: {reason}"


def wait_backend_unloaded(timeout=120):
    """Poll until llama-swap reports nothing loaded. A real measurement of release."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not backend_running_models():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _finish_coordinator(cycle_id):
    restart = False
    with STATE.condition:
        STATE.coordinator_running = False
        if not STATE.demands and STATE.mode == "DRAINING":
            if STATE.last_error is None and not STATE.recovery_required:
                STATE._open_unlocked()
            else:
                STATE.phase = "RECOVERY_REQUIRED"
                STATE.recovery_required = True
        elif (
            STATE.demands
            and STATE.mode == "DRAINING"
            and STATE.cycle_id == cycle_id
            and STATE.phase == "CANCELLING"
            and STATE.last_error is None
            and not STATE.recovery_required
        ):
            STATE.phase = "DRAINING"
            STATE.coordinator_running = True
            restart = True
        STATE._persist_unlocked()
        STATE.condition.notify_all()
    return restart


def _launch_coordinator(cycle_id):
    try:
        threading.Thread(target=coordinator, args=(cycle_id,), daemon=True).start()
    except Exception as exc:
        with STATE.condition:
            STATE.coordinator_running = False
            STATE.mode = "DRAINING"
            STATE.phase = "FAILED_CLOSED"
            STATE.last_error = f"coordinator thread start failed: {exc}"
            STATE.recovery_required = True
            STATE._persist_unlocked()
            STATE.condition.notify_all()
        raise


def coordinator(cycle_id):
    try:
        with STATE.condition:
            while STATE.active_ids and STATE.mode == "DRAINING" and STATE.demands:
                STATE.phase = "WAITING_ACTIVE_REQUEST"
                STATE._persist_unlocked()
                STATE.condition.wait(timeout=1)
            if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                return
            STATE.phase = "SAMPLING_IDLE"
            STATE._persist_unlocked()

        qwen = find_qwen_process()
        if qwen is None:
            # L10 (2026-09-07): find_qwen_process() matches argv[0] basename against
            # "llama-server", and require_no_qwen_nvml() filters NVML rows by the same
            # string. Under any other engine llama-swap may be running - vLLM TP=2 in
            # particular, whose NVML rows are named "VLLM::Worker_TP0"/"TP1" - BOTH
            # checks come back empty, so the gate declared WAITING_EXTERNAL while the
            # backend still held both GPUs and the canonical lock, and never attempted
            # an unload (unload_attempted stayed False). Every transcription then
            # starved until worker timeout. Measured 2026-09-06 and again 2026-09-07.
            #
            # A backend that llama-swap is running is NOT external - it is ours, and it
            # must be unloaded whatever the engine is called. So ask the BACKEND what it
            # is running instead of scanning /proc for an engine name, and verify the
            # release by observing the model is actually gone rather than by an
            # engine-identity predicate that cannot return negative.
            #
            # WAITING_EXTERNAL remains correct for a genuinely external tenant (vgen):
            # that case leaves /running empty, so this block is skipped entirely.
            running = backend_running_models()
            if running:
                with STATE.condition:
                    if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                        return
                    if STATE.unload_attempted or STATE.unload_attempt_count != 0:
                        raise RuntimeError("backend unload was already attempted in this cycle")
                    STATE.phase = "UNLOADING_QWEN"
                    STATE.unload_attempted = True
                    STATE.unload_attempt_count = 1
                    STATE.unload_attempted_at = now()
                    STATE._persist_unlocked()
                unload_status = unload_backend_model()
                with STATE.condition:
                    STATE.unload_http_status = unload_status
                    STATE.unload_completed_at = now()
                    STATE._persist_unlocked()
                if unload_status != 200:
                    raise RuntimeError(f"backend unload failed: {unload_status}")
                if not wait_backend_unloaded():
                    raise RuntimeError("backend model remained loaded after unload")
                with STATE.condition:
                    STATE.qwen_exited_at = now()
                    STATE.qwen_vram_released_at = now()
                    STATE._persist_unlocked()
            require_no_qwen_nvml()
            with STATE.condition:
                if STATE.mode == "DRAINING" and STATE.demands and STATE.cycle_id == cycle_id:
                    if find_qwen_process() is not None:
                        raise RuntimeError("Qwen appeared during external-readiness verification")
                    STATE.phase = "WAITING_EXTERNAL"
                    STATE._persist_unlocked()
                    STATE.condition.notify_all()
            return

        pid, port = qwen
        with STATE.condition:
            STATE.qwen_pid = pid
            STATE.qwen_port = port
            STATE._persist_unlocked()

        while True:
            with STATE.condition:
                if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                    return
                if STATE.active_ids:
                    STATE.phase = "WAITING_ACTIVE_REQUEST"
                    STATE._persist_unlocked()
                    STATE.condition.wait(timeout=1)
                    continue
            first = slot_sample(port)
            if first["is_processing"]:
                with STATE.condition:
                    STATE.phase = "WAITING_ACTIVE_SLOT"
                    STATE.slot_samples = [first]
                    STATE._persist_unlocked()
                time.sleep(1)
                continue
            with STATE.condition:
                STATE.phase = "SAMPLING_IDLE"
                STATE.slot_samples = [first]
                STATE._persist_unlocked()
            time.sleep(SLOT_SAMPLE_INTERVAL)
            with STATE.condition:
                if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                    return
                if STATE.active_ids:
                    continue
            current = find_qwen_process()
            if current != (pid, port):
                raise RuntimeError("Qwen PID or port changed between idle samples")
            second = slot_sample(port)
            with STATE.condition:
                STATE.slot_samples = [first, second]
                STATE._persist_unlocked()
            if second["is_processing"]:
                continue
            break

        with STATE.condition:
            if STATE.active_ids:
                raise RuntimeError("gate active request appeared before unload")
            if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                return
            if find_qwen_process() != (pid, port):
                raise RuntimeError("Qwen process changed before unload")
            if STATE.unload_attempted or STATE.unload_attempt_count != 0:
                raise RuntimeError("Qwen unload was already attempted in this cycle")
            STATE.phase = "VERIFYING_NVML"
            STATE._persist_unlocked()

        require_expected_qwen_nvml(pid)

        with STATE.condition:
            if STATE.active_ids:
                raise RuntimeError("gate active request appeared after NVML preflight")
            if STATE.mode != "DRAINING" or not STATE.demands or STATE.cycle_id != cycle_id:
                return
            if find_qwen_process() != (pid, port):
                raise RuntimeError("Qwen process changed after NVML preflight")
            if STATE.unload_attempted or STATE.unload_attempt_count != 0:
                raise RuntimeError("Qwen unload was already attempted in this cycle")
            STATE.phase = "UNLOADING_QWEN"
            STATE.unload_attempted = True
            STATE.unload_attempt_count = 1
            STATE.unload_attempted_at = now()
            STATE._persist_unlocked()

        unload_status = unload_backend_model()
        with STATE.condition:
            STATE.unload_http_status = unload_status
            STATE.unload_completed_at = now()
            STATE._persist_unlocked()
        if unload_status != 200:
            raise RuntimeError(f"Qwen unload failed: {unload_status}")

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
            time.sleep(0.2)
        if Path(f"/proc/{pid}").exists():
            raise RuntimeError("Qwen process did not exit after unload")
        with STATE.condition:
            STATE.qwen_exited_at = now()
            STATE._persist_unlocked()
        if not wait_qwen_vram_released():
            raise RuntimeError("Qwen VRAM remained allocated after unload")
        with STATE.condition:
            if find_qwen_process() is not None:
                raise RuntimeError("Qwen appeared after VRAM release verification")
            STATE.qwen_vram_released_at = now()
            STATE.phase = "WAITING_EXTERNAL"
            STATE._persist_unlocked()
            STATE.condition.notify_all()
    except Exception as exc:
        with STATE.condition:
            STATE.phase = "FAILED_CLOSED"
            STATE.last_error = str(exc)
            STATE._persist_unlocked()
    finally:
        if _finish_coordinator(cycle_id):
            _launch_coordinator(cycle_id)


class GateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class GateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llm-gate/1"

    def log_message(self, fmt, *args):
        print(f"{now()} http {self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, status, value, extra_headers=None):
        body = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def require_auth(self):
        if authenticated(self.headers):
            return True
        self.close_connection = True
        self.send_json(
            401,
            {"error": {"type": "authentication_error", "message": "authentication required"}},
            {"Connection": "close"},
        )
        return False

    def reject_unavailable(self, reason):
        message = "local inference is draining for an external GPU workload"
        if reason == "queue_full":
            message = "local inference queue is full"
        self.close_connection = True
        self.send_json(
            503,
            {"error": {"type": "overloaded_error", "message": message}, "retryable": True},
            {"Retry-After": "2", "Connection": "close"},
        )

    def proxy(self):
        if not self.require_auth():
            return
        ticket, reason = STATE.admit()
        if ticket is None:
            self.reject_unavailable(reason)
            return
        connection = None
        response_started = False
        try:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                body = None
                if self.command in {"POST", "PUT", "PATCH"}:
                    self.close_connection = True
                    self.send_json(
                        411,
                        {"error": {"type": "invalid_request_error", "message": "Content-Length required"}},
                        {"Connection": "close"},
                    )
                    return
            else:
                try:
                    length = int(length_header)
                except ValueError:
                    self.close_connection = True
                    self.send_json(
                        400,
                        {"error": {"type": "invalid_request_error", "message": "invalid Content-Length"}},
                        {"Connection": "close"},
                    )
                    return
                if length < 0 or length > 32 * 1024 * 1024:
                    self.close_connection = True
                    self.send_json(
                        413,
                        {"error": {"type": "invalid_request_error", "message": "request body too large"}},
                        {"Connection": "close"},
                    )
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self.close_connection = True
                    self.send_json(
                        400,
                        {"error": {"type": "invalid_request_error", "message": "incomplete request body"}},
                        {"Connection": "close"},
                    )
                    return

            connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=3600)
            headers = {}
            for key, value in self.headers.items():
                lower = key.lower()
                if lower in HOP_HEADERS or lower == "host":
                    continue
                headers[key] = value
            headers["Host"] = f"{BACKEND_HOST}:{BACKEND_PORT}"
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            has_length = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_HEADERS:
                    continue
                if lower == "content-length":
                    has_length = True
                self.send_header(key, value)
            no_body = self.command == "HEAD" or response.status in {204, 304} or 100 <= response.status < 200
            chunked = not no_body and not has_length
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            response_started = True
            if no_body:
                return
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.close_connection = True
            if not response_started and not self.wfile.closed:
                try:
                    self.send_json(
                        502,
                        {"error": {"type": "api_error", "message": f"local backend error: {exc}"}},
                        {"Connection": "close"},
                    )
                except Exception:
                    pass
        finally:
            if connection is not None:
                connection.close()
            STATE.release_request(ticket)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            snapshot = STATE.snapshot()
            self.send_json(200, {"status": "ok", "gate": snapshot["mode"], "phase": snapshot["phase"]})
            return
        if path == "/gate/status":
            if self.require_auth():
                self.send_json(200, STATE.snapshot())
            return
        self.proxy()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()

    def do_PATCH(self):
        self.proxy()

    def do_DELETE(self):
        self.proxy()

    def do_OPTIONS(self):
        self.proxy()


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        if uid != 0:
            self.respond({"ok": False, "error": "unauthorized peer"})
            return
        raw = self.rfile.readline(65537)
        if not raw or len(raw) > 65536:
            self.respond({"ok": False, "error": "invalid control request"})
            return
        try:
            message = json.loads(raw)
            token = message.pop("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, API_KEY):
                raise PermissionError("control authentication failed")
            action = message.get("action")
            if action == "status":
                snapshot = STATE.snapshot()
            elif action == "begin":
                start, snapshot = STATE.begin_demand(message)
                if start:
                    _launch_coordinator(snapshot["cycle_id"])
            elif action == "acquired":
                snapshot = STATE.acquired(message)
            elif action == "release":
                snapshot = STATE.release_demand(message)
            elif action == "cancel":
                snapshot = STATE.release_demand(message, cancelled=True)
            elif action == "recover":
                snapshot = STATE.recover()
            else:
                raise ValueError("unknown control action")
            self.respond({"ok": True, "state": snapshot})
        except Exception as exc:
            self.respond({"ok": False, "error": str(exc)})

    def respond(self, value):
        self.wfile.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        self.wfile.flush()


_LAST_AUTO_RECOVERY_DECLINE = None


def try_auto_recover(reason):
    """Re-test reality and reopen the gate iff the full recovery proof still passes.

    L12 (2026-09-07): RECOVERY_REQUIRED was a terminal state that only a human could
    leave. Any drain failure - including a transient one, such as the unload timeout in
    L11 - stopped ALL local inference with a 503 and, via begin_demand(), ALSO refused
    every external GPU workload. _load() re-latched it from disk on every boot, so
    restarting the stack (the first thing anyone tries) did not help. Observed
    2026-09-07: the gate bricked at 20:30, a full restart at 21:00 came back up still
    latched, and service was only restored by a manual control-socket call at 21:11.

    This does NOT weaken any safety invariant. It calls STATE.recover() - the exact same
    predicate the operator-triggered "recover" action uses - which requires all of: no
    active, queued or demanded work; no coordinator running; no Qwen process; the
    canonical GPU lease free (proved by a non-blocking flock, so a live vgen or
    transcription tenant blocks recovery); and NVML reporting zero GPU compute processes
    on any card. If the GPU is genuinely busy or unprovable, recover() raises and the
    gate stays closed exactly as before. The only thing removed is the requirement that
    a human be present to run a check the gate can run itself.
    """
    global _LAST_AUTO_RECOVERY_DECLINE
    with STATE.condition:
        if STATE.mode != "DRAINING" or not STATE.recovery_required:
            return False
        if STATE.active_ids or STATE.queue or STATE.demands or STATE.coordinator_running:
            return False
    try:
        STATE.recover()
    except Exception as exc:
        detail = f"{reason}: {exc}"
        if detail != _LAST_AUTO_RECOVERY_DECLINE:
            _LAST_AUTO_RECOVERY_DECLINE = detail
            print(f"{now()} gate auto-recovery declined reason={detail}", flush=True)
        return False
    _LAST_AUTO_RECOVERY_DECLINE = None
    print(f"{now()} gate auto-recovery succeeded reason={reason} mode=OPEN", flush=True)
    return True


def serve_auto_recovery():
    while True:
        time.sleep(AUTO_RECOVERY_INTERVAL)
        try:
            try_auto_recover("watchdog")
        except Exception as exc:
            print(f"{now()} gate auto-recovery watchdog error={exc}", flush=True)


def serve_control():
    CONTROL_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        CONTROL_SOCKET.unlink()
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o177)
    try:
        server = ControlServer(str(CONTROL_SOCKET), ControlHandler)
    finally:
        os.umask(old_umask)
    os.chmod(CONTROL_SOCKET, 0o600)
    server.serve_forever(poll_interval=0.2)


def main():
    control = threading.Thread(target=serve_control, daemon=True)
    control.start()
    # A gate restored from disk into RECOVERY_REQUIRED gets one immediate chance to prove
    # the GPU is free before it starts refusing traffic, so a restart is once again a
    # meaningful repair action rather than a no-op.
    try_auto_recover("startup")
    threading.Thread(target=serve_auto_recovery, daemon=True).start()
    server = GateHTTPServer((LISTEN_HOST, LISTEN_PORT), GateHandler)
    print(
        f"{now()} gate listening={LISTEN_HOST}:{LISTEN_PORT} backend={BACKEND_HOST}:{BACKEND_PORT} "
        f"queue_limit={QUEUE_LIMIT} initial_mode={STATE.mode} phase={STATE.phase}",
        flush=True,
    )
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
