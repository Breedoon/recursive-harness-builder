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


END_OVERRUN_TOLERANCE_SECONDS = 1.0
ACTIVE_FRAME_SECONDS = 0.1
ACTIVE_FRAME_DBFS = -40.0
MIN_ACTIVE_AUDIO_SECONDS = 1.0


def _bound_segments(segments: list, duration: float) -> tuple[list, list[dict], list[dict]]:
    """Drop segments that start at/after the decoded audio end; clamp and flag overruns.

    Whisper can emit segments timed past the real audio (vault-g7h). A segment starting
    at or after the end cannot describe audio, so it is dropped. A segment that starts
    inside the audio but ends well past it is kept (it may hold real final speech), its
    end is clamped to the audio end, and it is reported for review.
    """
    kept: list = []
    dropped: list[dict] = []
    overruns: list[dict] = []
    if duration <= 0:
        return list(segments), dropped, overruns
    for seg in segments:
        start = float(seg.start)
        end = float(seg.end)
        if start >= duration:
            dropped.append({"start": round(start, 2), "end": round(end, 2)})
            continue
        if end > duration + END_OVERRUN_TOLERANCE_SECONDS:
            overruns.append({"start": round(start, 2), "end": round(end, 2)})
        if end > duration:
            seg = _with_end(seg, duration)
        kept.append(seg)
    return kept, dropped, overruns


def _with_end(seg, end: float):
    try:
        return seg._replace(end=end)
    except AttributeError:
        from types import SimpleNamespace

        return SimpleNamespace(**{**vars(seg), "end": end})


def _audio_activity(audio_file: Path) -> dict[str, float]:
    """Seconds of 100 ms frames at or above ACTIVE_FRAME_DBFS RMS, on the decoded 16 kHz signal."""
    import numpy as np
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(audio_file), sampling_rate=16000)
    frame = int(16000 * ACTIVE_FRAME_SECONDS)
    count = len(audio) // frame
    if count == 0:
        return {"active_seconds": 0.0, "max_frame_dbfs": -120.0}
    frames = np.asarray(audio[: count * frame], dtype=np.float64).reshape(count, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    dbfs = 20.0 * np.log10(np.maximum(rms, 1e-6))
    return {
        "active_seconds": round(float(np.sum(dbfs >= ACTIVE_FRAME_DBFS)) * ACTIVE_FRAME_SECONDS, 1),
        "max_frame_dbfs": round(float(np.max(dbfs)), 1),
    }


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
    condition_on_previous_text = os.environ.get(
        "OBS_TRANSCRIPTION_CONDITION_ON_PREVIOUS_TEXT", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    temperatures = tuple(
        float(part)
        for part in os.environ.get("OBS_TRANSCRIPTION_TEMPERATURES", "0.0,0.2,0.4").split(",")
        if part.strip()
    ) or (0.0,)
    no_speech_threshold = float(os.environ.get("OBS_TRANSCRIPTION_NO_SPEECH_THRESHOLD", "0.6"))
    log_prob_threshold = float(os.environ.get("OBS_TRANSCRIPTION_LOG_PROB_THRESHOLD", "-1.0"))
    compression_ratio_threshold = float(
        os.environ.get("OBS_TRANSCRIPTION_COMPRESSION_RATIO_THRESHOLD", "2.4")
    )
    language = os.environ.get("OBS_TRANSCRIPTION_LANGUAGE", "").strip() or None
    vad_threshold = float(os.environ.get("OBS_TRANSCRIPTION_VAD_THRESHOLD", "0.5"))
    vad_min_speech_ms = int(os.environ.get("OBS_TRANSCRIPTION_VAD_MIN_SPEECH_MS", "250"))
    vad_min_silence_ms = int(os.environ.get("OBS_TRANSCRIPTION_VAD_MIN_SILENCE_MS", "2000"))
    vad_speech_pad_ms = int(os.environ.get("OBS_TRANSCRIPTION_VAD_SPEECH_PAD_MS", "200"))
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
        segments, info = model.transcribe(
            str(audio_file),
            beam_size=beam_size,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            temperature=temperatures,
            no_speech_threshold=no_speech_threshold,
            log_prob_threshold=log_prob_threshold,
            compression_ratio_threshold=compression_ratio_threshold,
            language=language,
            vad_parameters=dict(
                threshold=vad_threshold,
                min_speech_duration_ms=vad_min_speech_ms,
                min_silence_duration_ms=vad_min_silence_ms,
                speech_pad_ms=vad_speech_pad_ms,
            ),
        )
        audio_duration = float(getattr(info, "duration", 0.0) or 0.0)
        segment_list, dropped_past_end, end_overruns = _bound_segments(list(segments), audio_duration)
        text = " ".join(seg.text.strip() for seg in segment_list if seg.text.strip()).strip()
        segment_lines = []
        for seg in segment_list:
            clean = seg.text.strip()
            if not clean:
                continue
            segment_lines.append(f"- `{seg.start:.2f}-{seg.end:.2f}` {clean}")
        status = "ok"
        quality: dict[str, object] = {
            "segments_dropped_past_end": len(dropped_past_end),
            "segments_end_overrun": end_overruns,
        }
        notes = []
        if end_overruns:
            notes.append(
                "> [!warning] Segment timing overran the end of the audio; the overrunning "
                "segment(s) may contain fabricated text. Verify against the audio before acting: "
                + ", ".join(f"{o['start']:.2f}-{o['end']:.2f}" for o in end_overruns)
            )
        if not text:
            activity = _audio_activity(audio_file)
            quality.update(activity)
            if activity["active_seconds"] >= MIN_ACTIVE_AUDIO_SECONDS:
                status = "degraded"
                quality["degraded_reason"] = "no_segments_despite_audio_energy"
                text = (
                    "[Transcription degraded: the model produced no speech, but the audio has "
                    f"{activity['active_seconds']:.1f} s above {ACTIVE_FRAME_DBFS:.0f} dBFS. "
                    "Speech may be present and missing from this transcript.]"
                )
            else:
                text = "[No speech detected by the selected transcription model.]"
        elapsed = round(time.time() - started, 3)
        body_parts = ["# Transcript", "", text]
        if notes:
            body_parts.extend(["", *notes])
        if segment_lines:
            body_parts.extend(["", "## Segments", "", *segment_lines])
        _write_markdown(
            output_path,
            frontmatter={
                **frontmatter_base,
                "transcription_status": status,
                "transcription_quality": quality,
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
                "language": getattr(info, "language", None),
                "language_probability": round(float(getattr(info, "language_probability", 0.0)), 6),
                "duration_seconds": round(float(getattr(info, "duration", 0.0)), 3),
                "elapsed_seconds": elapsed,
                "segment_count": len(segment_list),
                "whisper_settings": {
                    "beam_size": beam_size,
                    "vad_filter": vad_filter,
                    "condition_on_previous_text": condition_on_previous_text,
                    "temperature": list(temperatures),
                    "no_speech_threshold": no_speech_threshold,
                    "log_prob_threshold": log_prob_threshold,
                    "compression_ratio_threshold": compression_ratio_threshold,
                    "vad_parameters": {
                        "threshold": vad_threshold,
                        "min_speech_duration_ms": vad_min_speech_ms,
                        "min_silence_duration_ms": vad_min_silence_ms,
                        "speech_pad_ms": vad_speech_pad_ms,
                    },
                },
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
