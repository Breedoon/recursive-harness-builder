#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


SAFE_CORRELATION_RE = re.compile(r"^[a-f0-9]{32}$")


def _safe_title(title: str) -> str:
    safe = Path(title).name
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", safe).strip(" .")
    return safe or "transcript"


def _write_markdown(path: Path, *, frontmatter: dict[str, object], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, str):
            value = value.replace("\n", " ")
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", body.strip(), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _device_default() -> str:
    requested = os.environ.get("OBS_TRANSCRIPTION_DEVICE", "").strip().lower()
    if requested:
        return requested
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            subprocess.run([nvidia_smi], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5)
            return "cuda"
        except Exception:
            pass
    return "cpu"


def _compute_type_default(device: str) -> str:
    requested = os.environ.get("OBS_TRANSCRIPTION_COMPUTE_TYPE", "").strip()
    if requested:
        return requested
    return "float16" if device == "cuda" else "int8"


def _gpu_free_memory_mib() -> int | None:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        value = result.stdout.strip().splitlines()[0]
        return int(float(value))
    except Exception:
        return None


def _release_idle_video_gpu(min_free_mib: int) -> None:
    free_before = _gpu_free_memory_mib()
    if free_before is None or free_before >= min_free_mib:
        return

    target = os.environ.get(
        "OBS_TRANSCRIPTION_VIDEO_RELEASE_HOST",
        "breedoon@host.docker.internal",
    ).strip()
    if not target:
        raise RuntimeError(
            f"GPU has only {free_before} MiB free and no video-release host is configured"
        )

    release = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            target,
            "sudo /usr/local/sbin/obs-release-idle-video-gpu",
        ],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if release.returncode != 0:
        detail = (release.stderr or release.stdout).strip()
        raise RuntimeError(
            f"could not release idle video-generation GPU memory: {detail or f'exit {release.returncode}'}"
        )

    deadline = time.time() + 30
    while time.time() < deadline:
        free_after = _gpu_free_memory_mib()
        if free_after is not None and free_after >= min_free_mib:
            return
        time.sleep(1)
    free_after = _gpu_free_memory_mib()
    raise RuntimeError(
        f"GPU memory did not recover after video-generation cleanup: "
        f"free={free_after} MiB required={min_free_mib} MiB"
    )


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: transcribe_faster_whisper.py AUDIO_FILE TITLE DEST_DIR", file=sys.stderr)
        return 64

    audio_file = Path(sys.argv[1]).resolve()
    title = _safe_title(sys.argv[2])
    dest_dir = Path(sys.argv[3]).resolve()
    output_path = dest_dir / f"{title}.md"
    correlation_id = os.environ.get("OBS_TRANSCRIPTION_CORRELATION_ID", "")
    if not SAFE_CORRELATION_RE.fullmatch(correlation_id):
        print("invalid transcription correlation ID", file=sys.stderr)
        return 64
    started = time.time()

    frontmatter_base = {
        "transcription_engine": "faster-whisper",
        "source_audio": str(audio_file),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "correlation_id": correlation_id,
    }

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        _write_markdown(
            output_path,
            frontmatter={**frontmatter_base, "transcription_status": "failed"},
            body=f"# Transcription unavailable\n\n`faster-whisper` is not installed or could not import: {type(exc).__name__}: {exc}",
        )
        print(f"faster-whisper unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78

    if not audio_file.is_file():
        _write_markdown(
            output_path,
            frontmatter={**frontmatter_base, "transcription_status": "failed"},
            body=f"# Transcription unavailable\n\nAudio file not found: `{audio_file}`",
        )
        print(f"audio file not found: {audio_file}", file=sys.stderr)
        return 66

    model_name = os.environ.get("OBS_TRANSCRIPTION_MODEL", "large-v3-turbo").strip() or "tiny"
    model_dir = Path(os.environ.get("OBS_TRANSCRIPTION_MODEL_DIR", "/workspace/runtime/transcription/models")).resolve()
    device = _device_default()
    compute_type = _compute_type_default(device)
    beam_size = int(os.environ.get("OBS_TRANSCRIPTION_BEAM_SIZE", "5"))
    vad_filter = os.environ.get("OBS_TRANSCRIPTION_VAD_FILTER", "0").strip().lower() in {"1", "true", "yes", "on"}
    lock_path = Path(os.environ.get("OBS_TRANSCRIPTION_GPU_LOCK", "/workspace/gpu-coordination/rtx3090.lock"))
    min_free_mib = int(os.environ.get("OBS_TRANSCRIPTION_GPU_MIN_FREE_MIB", "4096"))
    lock_already_held = os.environ.get("OBS_TRANSCRIPTION_LOCK_ALREADY_HELD", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
    lock_timeout = int(os.environ.get("OBS_TRANSCRIPTION_GPU_LOCK_TIMEOUT_SECONDS", "120"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    try:
        lock_file = None
        if device == "cuda":
            if not lock_already_held:
                lock_file = lock_path.open("a+")
                deadline = time.monotonic() + lock_timeout
                while True:
                    try:
                        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"GPU lock unavailable after {lock_timeout} seconds"
                            )
                        time.sleep(0.25)
            _release_idle_video_gpu(min_free_mib)
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(model_dir),
        )
        segments, info = model.transcribe(str(audio_file), beam_size=beam_size, vad_filter=vad_filter)
        segment_list = list(segments)
        text = " ".join(seg.text.strip() for seg in segment_list if seg.text.strip()).strip()
        segment_lines = []
        for seg in segment_list:
            clean = seg.text.strip()
            if not clean:
                continue
            segment_lines.append(f"- `{seg.start:.2f}-{seg.end:.2f}` {clean}")
        if not text:
            text = "[No speech detected by the selected transcription model.]"
        elapsed = round(time.time() - started, 3)
        body_parts = ["# Transcript", "", text]
        if segment_lines:
            body_parts.extend(["", "## Segments", "", *segment_lines])
        _write_markdown(
            output_path,
            frontmatter={
                **frontmatter_base,
                "transcription_status": "ok",
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
                "language": getattr(info, "language", None),
                "language_probability": round(float(getattr(info, "language_probability", 0.0)), 6),
                "duration_seconds": round(float(getattr(info, "duration", 0.0)), 3),
                "elapsed_seconds": elapsed,
                "segment_count": len(segment_list),
            },
            body="\n".join(body_parts),
        )
        return 0
    except Exception as exc:
        _write_markdown(
            output_path,
            frontmatter={
                **frontmatter_base,
                "transcription_status": "failed",
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
            },
            body=f"# Transcription failed\n\n{type(exc).__name__}: {exc}",
        )
        print(f"transcription failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 80
    finally:
        try:
            if 'lock_file' in locals() and lock_file is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
