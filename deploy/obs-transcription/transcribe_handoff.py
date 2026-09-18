#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from gpu_lease_runner import (
    _gate_control,
    reconcile_admission_fail_closed,
    run_lease,
)

BASE = Path(os.environ.get("OBS_TRANSCRIPTION_BASE", "/workspace/runtime/transcription")).resolve()
RUNTIME_ROOT = Path("/workspace/runtime").resolve()
LOCK_PATH = Path(
    os.environ.get("OBS_TRANSCRIPTION_GPU_LOCK", "/workspace/gpu-coordination/rtx3090.lock")
)
SSH_TARGET = os.environ.get(
    "OBS_TRANSCRIPTION_VIDEO_RELEASE_HOST", "breedoon@host.docker.internal"
).strip()
PYTHON = Path(os.environ.get("OBS_TRANSCRIPTION_PYTHON", str(BASE / "venv/bin/python")))
HELPER = Path(
    os.environ.get(
        "OBS_TRANSCRIPTION_HELPER", str(BASE / "transcribe_faster_whisper.py")
    )
)
MODEL_SNAPSHOT = Path(
    "/workspace/runtime/transcription/models/"
    "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/"
    "snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
)
JOBS_DIR = BASE / "jobs"
JOB_PREFIX = "obs-transcribe-"
SAFE_TITLE_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
SAFE_CORRELATION_RE = re.compile(r"^[a-f0-9]{32}$")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lock_is_free() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock, fcntl.LOCK_UN)
        return True


def reconcile_stale_demands() -> dict[str, Any]:
    gate = _gate_control(SSH_TARGET, {"action": "status"})
    recovered: list[str] = []
    blocked: list[str] = []
    for job_id, demand in list(gate.get("demands", {}).items()):
        if not job_id.startswith(JOB_PREFIX):
            continue
        worker_pid = demand.get("worker_pid")
        if isinstance(worker_pid, int) and Path(f"/proc/{worker_pid}").exists():
            continue
        if not _lock_is_free():
            blocked.append(job_id)
            continue
        lock_stat = LOCK_PATH.stat()
        gate = _gate_control(
            SSH_TARGET,
            {
                "action": "cancel",
                "job_id": job_id,
                "worker_pid": worker_pid,
                "released_at": _now(),
                "lock_dev": lock_stat.st_dev,
                "lock_ino": lock_stat.st_ino,
            },
        )
        recovered.append(job_id)
    return {"gate": gate, "recovered": recovered, "blocked": blocked}


def _validate_transcript(path: Path, correlation_id: str) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise RuntimeError("GPU child produced no complete transcript")
    expected = {
        'transcription_status: "ok"',
        f'correlation_id: "{correlation_id}"',
        f'model: "{MODEL_SNAPSHOT}"',
        'device: "cuda"',
        'compute_type: "float16"',
        "# Transcript",
    }
    found: set[str] = set()
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            value = line.rstrip("\r\n")
            if value in expected:
                found.add(value)
    missing = expected - found
    if missing:
        raise RuntimeError(f"GPU child transcript is incomplete: {sorted(missing)}")


def _publish_no_replace(source: Path, output: Path) -> None:
    source.chmod(0o600)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"transcript destination already exists: {output}")
    os.link(source, output)
    with output.open("rb") as handle:
        os.fsync(handle.fileno())
    directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def transcribe(audio_arg: str, title_arg: str, dest_arg: str) -> dict[str, Any]:
    audio_source = Path(audio_arg)
    dest_source = Path(dest_arg)
    audio = audio_source.resolve()
    dest = dest_source.resolve()
    title = title_arg
    correlation_id = os.environ.get("OBS_TRANSCRIPTION_CORRELATION_ID", "")

    if not SAFE_CORRELATION_RE.fullmatch(correlation_id):
        raise ValueError("invalid transcription correlation ID")
    if (
        not title
        or not SAFE_TITLE_RE.fullmatch(title)
        or title[0] in {".", " "}
        or title[-1] in {".", " "}
        or Path(title).name != title
    ):
        raise ValueError("invalid transcript title")
    if audio_source.is_symlink() or not audio.is_file() or not _within(audio, RUNTIME_ROOT):
        raise ValueError(f"invalid audio path: {audio}")
    if dest_source.is_symlink() or not dest.is_dir() or not _within(dest, RUNTIME_ROOT):
        raise ValueError(f"invalid destination path: {dest}")
    if not PYTHON.is_file() or not HELPER.is_file() or not MODEL_SNAPSHOT.is_dir():
        raise RuntimeError("GPU transcription runtime is incomplete")

    output = dest / f"{title}.md"
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"transcript destination already exists: {output}")

    reconciliation = reconcile_stale_demands()
    if reconciliation["blocked"]:
        raise RuntimeError(
            f"stale transcription demand still owns the GPU: {reconciliation['blocked']}"
        )

    audio_sha256 = _sha256(audio)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    job_id = f"{JOB_PREFIX}{stamp}-{audio_sha256[:10]}-{uuid.uuid4().hex[:8]}"
    request_temp = dest / f".obs-gpu-{job_id}"
    request_temp.mkdir(mode=0o700)
    temp_output = request_temp / f"{title}.md"

    try:
        result = run_lease(
            command=[str(PYTHON), str(HELPER), str(audio), title, str(request_temp)],
            job_id=job_id,
            lock_path=LOCK_PATH,
            ssh_target=SSH_TARGET,
            jobs_dir=JOBS_DIR,
            extra_request={
                "audio_sha256": audio_sha256,
                "correlation_id": correlation_id,
                "output_path": str(output),
                "request_temp": str(request_temp),
                "model_snapshot": str(MODEL_SNAPSHOT),
                "device": "cuda",
                "compute_type": "float16",
            },
            child_env={
                "OBS_TRANSCRIPTION_LOCK_ALREADY_HELD": "1",
                "OBS_TRANSCRIPTION_CORRELATION_ID": correlation_id,
                "OBS_TRANSCRIPTION_MODEL": str(MODEL_SNAPSHOT),
                "OBS_TRANSCRIPTION_MODEL_DIR": str(BASE / "models"),
                "OBS_TRANSCRIPTION_DEVICE": "cuda",
                "OBS_TRANSCRIPTION_COMPUTE_TYPE": "float16",
                "OBS_TRANSCRIPTION_BEAM_SIZE": "1",
                "OBS_TRANSCRIPTION_VAD_FILTER": "1",
                "OBS_TRANSCRIPTION_GPU_LOCK": str(LOCK_PATH),
                "OBS_TRANSCRIPTION_ALLOW_CPU_FALLBACK": "0",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "CUDA_VISIBLE_DEVICES": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        _validate_transcript(temp_output, correlation_id)
        _publish_no_replace(temp_output, output)
        return {
            **result,
            "audio_sha256": audio_sha256,
            "correlation_id": correlation_id,
            "output_path": str(output),
            "model_snapshot": str(MODEL_SNAPSHOT),
            "device": "cuda",
            "compute_type": "float16",
            "reconciliation": reconciliation,
        }
    finally:
        shutil.rmtree(request_temp, ignore_errors=True)


def main() -> int:
    if sys.argv[1:] == ["--reconcile-only"]:
        result = reconcile_stale_demands()
        print(json.dumps(result, sort_keys=True))
        return 75 if result["blocked"] else 0
    if len(sys.argv) == 3 and sys.argv[1] == "--reconcile-fail-closed":
        result = reconcile_admission_fail_closed(
            expected_job_id=sys.argv[2],
            lock_path=LOCK_PATH,
            ssh_target=SSH_TARGET,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} AUDIO_FILE TITLE DEST_DIR", file=sys.stderr)
        return 64
    try:
        result = transcribe(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as exc:
        print(f"transcription handoff failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 80
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
