#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
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


def _strong_periodic_run(text: str) -> int:
    tokens = re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold())
    qualifying = 0
    for width in range(1, min(8, len(tokens)) + 1):
        runs = [1] * (len(tokens) + 1)
        for start in range(len(tokens) - width, -1, -1):
            if start + 2 * width <= len(tokens) and tokens[start:start + width] == tokens[start + width:start + 2 * width]:
                runs[start] = runs[start + width] + 1
            span = runs[start] * width
            if runs[start] >= 8 and span >= 16:
                qualifying = max(qualifying, span)
    return qualifying


REPEAT_CONTIGUITY_SECONDS = 2.0


def _segment_groups(segments: list) -> list[list]:
    groups = []
    previous_start = previous_end = None
    for segment in segments:
        start, end = float(segment.start), float(segment.end)
        if (not groups or start < previous_start or start - previous_end > REPEAT_CONTIGUITY_SECONDS):
            groups.append([])
        groups[-1].append(segment)
        previous_start, previous_end = start, end
    return groups


def _strong_periodic_segments(segments: list) -> int:
    return max((_strong_periodic_run(' '.join(seg.text for seg in group)) for group in _segment_groups(segments)), default=0)


def _original_word_spans(text: str):
    start = None
    for index, char in enumerate(text):
        is_word = char == '_' or char.isalnum() or (start is not None and unicodedata.category(char).startswith('M'))
        if is_word and start is None:
            start = index
        elif not is_word and start is not None:
            yield start, index
            start = None
    if start is not None:
        yield start, len(text)


def _collapse_periodic_segments(segments: list, *, limit: int = 100) -> tuple:
    from types import SimpleNamespace

    tokens = []
    per_segment = []
    group_index = -1
    previous_start = previous_end = None
    for segment_index, seg in enumerate(segments):
        if (previous_start is None or float(seg.start) < previous_start
                or float(seg.start) - previous_end > REPEAT_CONTIGUITY_SECONDS):
            group_index += 1
        previous_start, previous_end = float(seg.start), float(seg.end)
        indices = []
        for start, end in _original_word_spans(seg.text):
            indices.append(len(tokens))
            tokens.append({'segment': segment_index, 'group': group_index, 'start': start, 'end': end,
                           'value': unicodedata.normalize('NFKC', seg.text[start:end]).casefold()})
        per_segment.append(indices)
    active = list(range(len(tokens)))
    removed = set()
    markers = {}
    runs_collapsed = removed_words = 0
    while runs_collapsed < limit:
        values = [tokens[index]['value'] for index in active]
        best = None
        for width in range(1, min(8, len(values)) + 1):
            runs = [1] * (len(values) + 1)
            for start in range(len(values) - width, -1, -1):
                if (start + 2 * width <= len(values)
                        and tokens[active[start]]['group'] == tokens[active[start + 2 * width - 1]]['group']
                        and values[start:start + width] == values[start + width:start + 2 * width]):
                    runs[start] = runs[start + width] + 1
                span = runs[start] * width
                if runs[start] >= 8 and span >= 16:
                    score = (span, -width, -start)
                    if best is None or score > best[0]:
                        best = (score, start, width, runs[start])
        if best is None:
            break
        _, start, width, repeats = best
        retained = active[start + width - 1]
        discard = active[start + width:start + width * repeats]
        markers[retained] = markers.get(retained, 0) + repeats - 1
        removed.update(discard)
        removed_words += len(discard)
        active = active[:start + width] + active[start + width * repeats:]
        runs_collapsed += 1
    output = []
    for segment_index, seg in enumerate(segments):
        indices = per_segment[segment_index]
        edits = []
        pos = 0
        while pos < len(indices):
            token_index = indices[pos]
            if token_index not in removed:
                if token_index in markers:
                    end = tokens[token_index]['end']
                    edits.append((end, end, f' [repeat collapsed ×{markers[token_index]}]'))
                pos += 1
                continue
            first = pos
            while pos < len(indices) and indices[pos] in removed:
                pos += 1
            start = tokens[indices[first]]['start']
            end = tokens[indices[pos]]['start'] if pos < len(indices) else len(seg.text)
            edits.append((start, end, ''))
        value = seg.text
        for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
            value = value[:start] + replacement + value[end:]
        if value.strip():
            output.append(SimpleNamespace(start=seg.start, end=seg.end, text=value))
    unresolved = _strong_periodic_segments(output)
    return output, {'runs_collapsed': runs_collapsed, 'removed_words': removed_words,
                    'attempt_limit_hit': runs_collapsed == limit, 'remaining_periodic_words': unresolved}


def _collapse_untimed_items(items: list) -> tuple:
    from types import SimpleNamespace

    if not items:
        return [], []
    # This is a conservative scan of emission order, not a chronological assertion.
    segments = [SimpleNamespace(start=0.0, end=0.0, text=item['text']) for item in items]
    collapsed, summary = _collapse_periodic_segments(segments)
    details = [summary] if summary['runs_collapsed'] or summary['remaining_periodic_words'] else []
    return [{'text': seg.text} for seg in collapsed], details


def _decode_window_with_retry(model, core, options: dict, *, allow_retry: bool) -> tuple:
    import math

    def tokens(text):
        return re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold())

    def signals(segments):
        repeated = _strong_periodic_segments(segments)
        bad_compression = 0
        for seg in segments:
            try:
                ratio = float(getattr(seg, 'compression_ratio', 0.0))
                if not math.isfinite(ratio) or ratio < 0 or ratio > options['compression_ratio_threshold']:
                    bad_compression += 1
            except (TypeError, ValueError):
                bad_compression += 1
        return repeated, bad_compression

    original, _ = model.transcribe(core, **options)
    original = list(original)
    original_repeats, original_compression = signals(original)
    if not original_repeats and not original_compression:
        return original, None
    result = {'status': 'unresolved_preserve_original', 'attempts': 0,
              'original_periodic_words': original_repeats, 'original_high_compression_segments': original_compression,
              'retry_periodic_words': None, 'retry_high_compression_segments': None,
              'retry_segments': None, 'retry_tokens': None}
    if not allow_retry:
        result['status'] = 'retry_limit_skipped'
        return original, result
    result['attempts'] = 1
    try:
        retry_options = {**options, 'temperature': (0.2, 0.4, 0.6), 'condition_on_previous_text': False}
        retry, _ = model.transcribe(core, **retry_options)
        retry = list(retry)
        retry_repeats, retry_compression = signals(retry)
        result.update(retry_periodic_words=retry_repeats, retry_high_compression_segments=retry_compression,
                      retry_segments=len(retry), retry_tokens=sum(len(tokens(seg.text)) for seg in retry))
        span = len(core) / 16000
        valid = all(math.isfinite(float(seg.start)) and math.isfinite(float(seg.end))
                    and 0 <= float(seg.start) <= float(seg.end) <= span for seg in retry)
        if retry and valid and not retry_repeats and not retry_compression:
            result['status'] = 'alternative_not_selected'
    except Exception as exc:
        result.update(status='retry_error_preserve_original', error_type=type(exc).__name__)
    return original, result


def _transcribe_windows(model, audio_file: Path, options: dict) -> tuple:
    from types import SimpleNamespace
    import math
    import numpy as np
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    sample_rate = 16000
    audio = decode_audio(str(audio_file), sampling_rate=sample_rate)
    duration = len(audio) / sample_rate
    dropped, overruns, windows, segments, untimed = [], [], [], [], []
    diagnostics = {"segments_high_no_speech": 0, "segments_low_logprob": 0, "segments_high_compression": 0,
                   "untimed_segment_bounds": [],
                   "strong_repeat_segments": [], "strong_repeat_windows": [], "segments_bounds_adjusted": [],
                   "hard_seams_seconds": [float(x) for x in range(60, math.ceil(duration), 60)],
                   "hard_seam_policy": "no_overlap_coverage_unverified", "vad_empty_windows": [],
                   "exact_zero_windows": [], "repeat_retry_windows": [], "vad_empty_alternatives": [],
                   "extra_decode_limit": 8, "extra_decode_attempts": 0}
    vad_options = VadOptions(**options["vad_parameters"]) if options["vad_filter"] else None
    language_set = ("en", "pl", "ru", "uk")
    first_info = None
    for window_start in range(0, len(audio), 60 * sample_rate):
        window_end = min(len(audio), window_start + 60 * sample_rate)
        metadata = {"start": window_start / sample_rate, "end": window_end / sample_rate,
                    "language": None, "language_probability": None, "vote_chunks": []}
        window_audio = audio[window_start:window_end]
        window_rms = float(np.sqrt(np.mean(np.square(window_audio, dtype=np.float64))))
        if not math.isfinite(window_rms):
            raise RuntimeError("audio has invalid samples")
        if window_rms == 0:
            metadata["exact_digital_zero"] = True
            diagnostics["exact_zero_windows"].append({"start": metadata["start"], "end": metadata["end"]})
            windows.append(metadata)
            continue
        totals = {}
        votes = []
        for core_start in range(window_start, window_end, 30 * sample_rate):
            core_end = min(window_end, core_start + 30 * sample_rate)
            core = audio[core_start:core_end]
            rms = float(np.sqrt(np.mean(np.square(core, dtype=np.float64))))
            if not math.isfinite(rms):
                raise RuntimeError("audio has invalid samples")
            if rms == 0:
                continue
            if model.model.is_multilingual:
                top, top_probability, all_probabilities = model.detect_language(audio=core, vad_filter=False)
                if (not isinstance(top, str) or not top or not math.isfinite(float(top_probability))
                        or not 0 <= top_probability <= 1):
                    raise RuntimeError("window language detection returned an invalid result")
                scores = {}
                for code, value in all_probabilities:
                    if not isinstance(code, str) or not code or code in scores:
                        raise RuntimeError("window language probability codes are invalid")
                    scores[code] = float(value)
                if (top not in scores or any(code not in scores for code in language_set)
                        or any(not math.isfinite(value) or value < 0 or value > 1 for value in scores.values())):
                    raise RuntimeError("window language probabilities are invalid")
            else:
                top, top_probability, scores = "en", 1.0, {"en": 1.0}
            vote_mass = sum(scores.get(code, 0.0) for code in language_set)
            if vote_mass > 1.0 + 2 ** -10:
                raise RuntimeError("slice language set has invalid probability mass")
            vote = {"start": core_start / sample_rate, "end": core_end / sample_rate,
                    "rms": rms, "unrestricted_language": top,
                    "unrestricted_probability": round(float(top_probability), 6),
                    "restricted_probabilities": {code: scores.get(code, 0.0) for code in language_set},
                    "language_set_mass_raw": vote_mass, "language_set_mass_rounding_corrected": vote_mass > 1.0}
            metadata["vote_chunks"].append(vote)
            votes.append((vote, scores))
        if not votes:
            windows.append(metadata)
            continue
        rms_cap = 4 * float(np.median([vote["rms"] for vote, _ in votes]))
        weight_total = 0.0
        for vote, scores in votes:
            weight = math.sqrt(min(vote["rms"], rms_cap)) * (vote["end"] - vote["start"])
            vote["weight"] = weight
            for code, value in scores.items():
                totals[code] = totals.get(code, 0.0) + weight * value
            weight_total += weight
        metadata["rms_weight_cap"] = rms_cap
        selected = {code: totals.get(code, 0.0) / weight_total for code in language_set}
        set_mass = sum(selected.values())
        # Four independently rounded FP16 probabilities can sum slightly above one.
        if not 0 < set_mass <= 1.0 + 2 ** -10:
            raise RuntimeError("window language set has invalid probability mass")
        language = max(language_set, key=selected.get)
        probability = selected[language] / set_mass
        unrestricted_language = max(totals, key=totals.get)
        unrestricted_probability = totals[unrestricted_language] / weight_total
        if first_info is None:
            first_info = SimpleNamespace(language=language, language_probability=probability)
        metadata.update(language=language, language_probability=round(float(probability), 6),
                        language_set_mass=round(min(float(set_mass), 1.0), 6),
                        language_set_mass_raw=float(set_mass),
                        language_set_mass_rounding_corrected=set_mass > 1.0, unrestricted_language=unrestricted_language,
                        unrestricted_probability=round(float(unrestricted_probability), 6))
        windows.append(metadata)
        core = audio[window_start:window_end]
        if vad_options is not None and not get_speech_timestamps(core, vad_options):
            metadata["vad_empty"] = True
            diagnostics["vad_empty_windows"].append({"start": metadata["start"], "end": metadata["end"]})
            alternative = {"start": metadata["start"], "end": metadata["end"], "status": "limit_skipped"}
            if diagnostics["extra_decode_attempts"] < diagnostics["extra_decode_limit"]:
                diagnostics["extra_decode_attempts"] += 1
                try:
                    alternative_options = {**options, "language": language, "vad_filter": False}
                    alternate_segments, _ = model.transcribe(core, **alternative_options)
                    alternate_segments = list(alternate_segments)
                    valid = all(math.isfinite(float(seg.start)) and math.isfinite(float(seg.end))
                                and 0 <= float(seg.start) <= float(seg.end) <= len(core) / sample_rate
                                for seg in alternate_segments)
                    alternate_text = ' '.join(seg.text for seg in alternate_segments)
                    alternative.update(status="decoded_not_selected" if valid else "invalid_timestamps",
                                       segments=len(alternate_segments),
                                       tokens=len(re.findall(r'\w+', unicodedata.normalize('NFKC', alternate_text).casefold())),
                                       periodic_words=_strong_periodic_segments(alternate_segments))
                except Exception as exc:
                    alternative.update(status="error", error_type=type(exc).__name__)
            diagnostics["vad_empty_alternatives"].append(alternative)
            continue
        offset = window_start / sample_rate
        chunk_options = {**options, "language": language}
        chunk_segments, retry = _decode_window_with_retry(
            model, core, chunk_options,
            allow_retry=diagnostics["extra_decode_attempts"] < diagnostics["extra_decode_limit"])
        if retry is not None:
            diagnostics["extra_decode_attempts"] += retry["attempts"]
            retry.update(start=metadata["start"], end=metadata["end"])
            diagnostics["repeat_retry_windows"].append(retry)
        bounded = []
        core_duration = len(core) / sample_rate
        for seg in chunk_segments:
            raw_start, raw_end = float(seg.start), float(seg.end)
            if not math.isfinite(raw_start) or not math.isfinite(raw_end) or raw_end < raw_start:
                raise RuntimeError("windowed transcript segment has invalid timestamps")
            start = min(core_duration, max(0.0, raw_start))
            end = min(core_duration, max(0.0, raw_end))
            if start >= end:
                reason = ('entirely_before' if raw_end < 0 else 'at_or_before_start' if raw_end == 0 and raw_start < 0
                          else 'entirely_after' if raw_start > core_duration else 'start_at_end' if raw_start == core_duration
                          else 'zero_length_within')
                item = {"window_start": offset, "window_end": offset + core_duration, 'window_duration': core_duration,
                        'reason': reason, "raw_start": raw_start + offset, "raw_end": raw_end + offset, "text": seg.text}
                untimed.append(item)
                diagnostics["untimed_segment_bounds"].append({key: value for key, value in item.items() if key != "text"})
                continue
            if start != raw_start or end != raw_end:
                diagnostics["segments_bounds_adjusted"].append({"raw_start": raw_start + offset,
                    "raw_end": raw_end + offset, "start": start + offset, "end": end + offset})
            if raw_end > core_duration + END_OVERRUN_TOLERANCE_SECONDS:
                overruns.append({"start": round(raw_start + offset, 2), "end": round(raw_end + offset, 2)})
            bounded.append(SimpleNamespace(start=start, end=end, text=seg.text,
                no_speech_prob=getattr(seg, "no_speech_prob", 0.0), avg_logprob=getattr(seg, "avg_logprob", 0.0),
                compression_ratio=getattr(seg, "compression_ratio", 0.0)))
        window_repeat_words = _strong_periodic_segments(bounded)
        if window_repeat_words:
            diagnostics["strong_repeat_windows"].append({"start": metadata["start"], "end": metadata["end"],
                                                          "periodic_words": window_repeat_words})
        for seg in bounded:
            diagnostics["segments_high_no_speech"] += int(getattr(seg, "no_speech_prob", 0.0) > options["no_speech_threshold"])
            diagnostics["segments_low_logprob"] += int(getattr(seg, "avg_logprob", 0.0) < options["log_prob_threshold"])
            diagnostics["segments_high_compression"] += int(getattr(seg, "compression_ratio", 0.0) > options["compression_ratio_threshold"])
            repeat_words = _strong_periodic_run(seg.text)
            if repeat_words:
                diagnostics["strong_repeat_segments"].append({"start": round(float(seg.start) + offset, 3),
                                                               "end": round(float(seg.end) + offset, 3),
                                                               "periodic_words": repeat_words})
            segments.append(SimpleNamespace(start=float(seg.start) + offset, end=float(seg.end) + offset, text=seg.text))
    if first_info is None:
        first_info = SimpleNamespace(language=None, language_probability=0.0)
    return segments, first_info, duration, dropped, overruns, windows, diagnostics, untimed


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
        options = dict(
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
        if language is not None or os.environ.get("OBS_TRANSCRIPTION_WINDOWED", "0") != "1":
            segments, info = model.transcribe(str(audio_file), **options)
            audio_duration = float(getattr(info, "duration", 0.0) or 0.0)
            segment_list, dropped_past_end, end_overruns = _bound_segments(list(segments), audio_duration)
            language_windows = None
            diagnostics = None
            untimed = []
        else:
            segment_list, info, audio_duration, dropped_past_end, end_overruns, language_windows, diagnostics, untimed = _transcribe_windows(
                model, audio_file, options
            )
        raw_periodic = _strong_periodic_segments(segment_list) if language_windows is not None else 0
        raw_segments = [{'start': float(seg.start), 'end': float(seg.end), 'text': seg.text} for seg in segment_list]
        raw_payload = None
        collapse = None
        if raw_periodic:
            collapsed, collapse = _collapse_periodic_segments(segment_list)
            if collapse['runs_collapsed']:
                segment_list = collapsed
        raw_untimed = untimed
        untimed_raw_periodic = _strong_periodic_run(' '.join(item['text'] for item in raw_untimed))
        untimed, untimed_collapse = _collapse_untimed_items(raw_untimed)
        untimed_remaining_periodic = _strong_periodic_run(' '.join(item['text'] for item in untimed))
        if raw_untimed or (collapse is not None and collapse['runs_collapsed']):
            raw_payload = json.dumps({'schema': 'raw-asr-before-provisional-processing-v2',
                                      'segments': raw_segments, 'untimed_segments': raw_untimed,
                                      'display_untimed_text': [item['text'] for item in untimed]}, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')
            raw_sha256 = hashlib.sha256(raw_payload).hexdigest()
            if collapse is not None and collapse['runs_collapsed']:
                collapse['raw_sha256'] = raw_sha256
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
        if language_windows is not None:
            diagnostics["strong_repeat_full_transcript_words"] = raw_periodic
            diagnostics["strong_repeat_after_collapse_words"] = _strong_periodic_segments(segment_list)
            diagnostics['untimed_segment_count'] = len(raw_untimed)
            diagnostics['strong_repeat_untimed_before_collapse_words'] = untimed_raw_periodic
            diagnostics['strong_repeat_untimed_after_collapse_words'] = untimed_remaining_periodic
            diagnostics["repeat_contiguity_seconds"] = REPEAT_CONTIGUITY_SECONDS
            quality["decoder_diagnostics"] = diagnostics
            if raw_payload is not None:
                quality["raw_asr_capture"] = {'raw_sha256': raw_sha256, 'untimed_segments': len(raw_untimed)}
            if raw_untimed:
                status = 'degraded'
                quality['degraded_reason'] = 'untimed_asr_segments'
                notes.append('> [!warning] Untimed ASR text could not be placed within its decoded window; emission order is not chronological; verify against audio.')
                for item in untimed:
                    notes.extend('> ' + line for line in item['text'].splitlines() or [''])
            if untimed_collapse:
                quality['untimed_provisional_repeat_collapse'] = {'items': untimed_collapse, 'raw_sha256': raw_sha256}
                notes.append('> [!warning] Untimed repetition was provisionally collapsed; speech may be missing. The raw ASR artifact and audio must be retained.')
            if collapse is not None and collapse["runs_collapsed"]:
                quality["provisional_repeat_collapse"] = collapse
                status = "degraded"
                quality.setdefault("degraded_reason", "provisional_repeat_collapse")
                notes.append("> [!warning] Repetition was provisionally collapsed; speech may be missing. The raw ASR artifact and audio must be retained.")
            low_confidence = [window for window in language_windows if window["language_probability"] is not None
                              and (window["language_probability"] < 0.5 or window["language_set_mass"] < 0.5
                                   or window["unrestricted_language"] not in {"en", "pl", "ru", "uk"}
                                   or any(vote["language_set_mass_raw"] == 0 for vote in window["vote_chunks"]))]
            quality["low_confidence_language_windows"] = low_confidence
            if low_confidence:
                status = "degraded"
                quality.setdefault("degraded_reason", "low_confidence_language_detection")
                notes.append("> [!warning] A recording window has uncertain or out-of-set language detection; verify against audio.")
            if diagnostics["vad_empty_windows"]:
                status = "degraded"
                quality.setdefault("degraded_reason", "vad_empty_nonzero_window")
                notes.append("> [!warning] VAD found no speech in a non-silent window; speech may be missing.")
            if diagnostics["exact_zero_windows"]:
                notes.append("> [!info] Exact digital-zero windows were skipped.")
            if diagnostics["repeat_retry_windows"]:
                status = "degraded"
                quality.setdefault("degraded_reason", "repeat_retry_requires_review")
                notes.append("> [!warning] A window triggered repeat/compression review; the original remains primary. Verify against audio.")
            if (diagnostics["strong_repeat_segments"] or diagnostics["strong_repeat_windows"]
                    or diagnostics["strong_repeat_full_transcript_words"]):
                status = "degraded"
                quality.setdefault("degraded_reason", "strong_periodic_repetition")
                notes.append("> [!warning] The transcript contains strong periodic repetition; verify against audio.")
            material_bounds = [item for item in diagnostics["segments_bounds_adjusted"]
                               if max(abs(item["raw_start"] - item["start"]),
                                      abs(item["raw_end"] - item["end"])) > 0.500000001]
            if material_bounds:
                status = "degraded"
                quality.setdefault("degraded_reason", "segment_timing_outside_decoded_window")
                notes.append("> [!warning] Segment timing required material clipping to the decoded window; verify against audio.")
        if end_overruns:
            notes.append(
                ("> [!warning] Segment timing overran a decoded window; the overrunning "
                 if language_windows is not None else "> [!warning] Segment timing overran the end of the audio; the overrunning ")
                + "segment(s) may contain fabricated text. Verify against the audio before acting: "
                + ", ".join(f"{o['start']:.2f}-{o['end']:.2f}" for o in end_overruns)
            )
        if not text:
            activity = _audio_activity(audio_file)
            quality.update(activity)
            if raw_untimed:
                text = "[No reliably timed ASR segments were produced; see the untimed text below.]"
            elif language_windows is not None and (diagnostics["vad_empty_windows"] or diagnostics["exact_zero_windows"]):
                if activity["active_seconds"] >= MIN_ACTIVE_AUDIO_SECONDS:
                    status = "degraded"
                    quality.setdefault("degraded_reason", "no_segments_despite_audio_energy")
                text = "[No primary transcript was produced; exact-zero or VAD-empty windows were skipped.]"
            elif activity["active_seconds"] >= MIN_ACTIVE_AUDIO_SECONDS:
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
        if raw_payload is not None:
            encoded = base64.b64encode(raw_payload).decode('ascii')
            body_parts.extend(["", f"<!-- OBS_RAW_ASR_BASE64_V1:{encoded} -->"])
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
                "duration_seconds": round(audio_duration, 3),
                **({"language_windows": language_windows, "language_set": ["en", "pl", "ru", "uk"],
                    "language_probability_basis": "duration_sqrt_median_capped_rms_weighted_within_language_set"} if language_windows is not None else {}),
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
