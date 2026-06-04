"""JSONL-backed context usage extraction for stable occupancy estimates."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class JsonlUsageSnapshot:
    """Latest usage snapshot from a session JSONL assistant entry."""

    source_path: Path
    assistant_entries: int
    usage_entries: int
    latest_input_tokens: int
    latest_output_tokens: int
    latest_cache_creation_input_tokens: int
    latest_cache_read_input_tokens: int
    latest_total_tokens_billed: int
    latest_context_triplet_tokens: int
    recent_peak_context_triplet_tokens: int
    session_peak_context_triplet_tokens: int
    text_estimate_tokens: int = 0
    context_estimate_source: str = "jsonl_latest_triplet"


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return 0


def _content_char_count(value: Any) -> int:
    """Approximate text-bearing transcript content length."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        total = 0
        for item in value:
            total += _content_char_count(item)
        return total
    if isinstance(value, dict):
        total = 0
        for key in ("text", "content", "thinking"):
            if key in value:
                total += _content_char_count(value.get(key))
        if total:
            return total
        try:
            return len(json.dumps(value, ensure_ascii=False))
        except TypeError:
            return 0
    return 0


def _estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, int(round(char_count / 4)))


def _encode_project_path(cwd: Path) -> str:
    """Encode a workspace path like Claude CLI project directory names."""
    resolved = str(cwd.expanduser().resolve(strict=False))
    slug = re.sub(r"[^A-Za-z0-9]", "-", resolved)
    return re.sub(r"-{2,}", "-", slug) or "-"


def _projects_root(projects_root: Path | None) -> Path:
    if projects_root is not None:
        return projects_root
    return Path.home() / ".claude" / "projects"


def find_session_jsonl(
    *,
    session_id: str,
    cwd: Path,
    projects_root: Path | None = None,
) -> Path | None:
    """Find session JSONL, preferring the current workspace project directory."""
    if not session_id:
        return None
    root = _projects_root(projects_root)
    if not root.is_dir():
        return None

    preferred = root / _encode_project_path(cwd) / f"{session_id}.jsonl"
    if preferred.is_file():
        return preferred

    matches: list[Path] = []
    try:
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                matches.append(candidate)
    except OSError:
        return None

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # If copied sessions exist across multiple projects, choose newest file.
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def load_jsonl_usage_snapshot(
    *,
    session_id: str | None,
    cwd: Path | None,
    projects_root: Path | None = None,
    recent_window: int = 8,
) -> JsonlUsageSnapshot | None:
    """Load latest same-session assistant usage metrics from JSONL."""
    if not session_id or cwd is None:
        return None

    path = find_session_jsonl(session_id=session_id, cwd=cwd, projects_root=projects_root)
    if path is None:
        return None

    assistant_entries = 0
    usage_totals: list[tuple[int, int, int, int, int]] = []
    # tuples: (input, output, cache_creation, cache_read, triplet_total)
    text_char_count = 0

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                if obj.get("type") in {"assistant", "user"}:
                    text_char_count += _content_char_count(message.get("content"))
                if obj.get("type") != "assistant":
                    continue
                assistant_entries += 1

                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                input_tokens = _as_int(usage.get("input_tokens"))
                output_tokens = _as_int(usage.get("output_tokens"))
                cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
                cache_read = _as_int(usage.get("cache_read_input_tokens"))
                triplet_total = input_tokens + cache_creation + cache_read
                usage_totals.append(
                    (
                        input_tokens,
                        output_tokens,
                        cache_creation,
                        cache_read,
                        triplet_total,
                    )
                )
    except OSError:
        return None

    text_estimate_tokens = _estimate_tokens_from_chars(text_char_count)
    informative_usage_totals = [total for total in usage_totals if total[4] > 0]
    if informative_usage_totals:
        latest = informative_usage_totals[-1]
        context_estimate_source = (
            "jsonl_latest_triplet"
            if usage_totals and usage_totals[-1] == latest
            else "jsonl_latest_positive_triplet"
        )
    elif text_estimate_tokens > 0:
        latest = (0, 0, 0, 0, text_estimate_tokens)
        context_estimate_source = "jsonl_text_estimate"
    else:
        return None

    recent_slice = usage_totals[-max(1, recent_window) :]
    recent_peak = max([total[4] for total in recent_slice] + [latest[4]])
    session_peak = max([total[4] for total in usage_totals] + [latest[4]])
    latest_total_billed = latest[0] + latest[1] + latest[2] + latest[3]

    return JsonlUsageSnapshot(
        source_path=path,
        assistant_entries=assistant_entries,
        usage_entries=len(usage_totals),
        latest_input_tokens=latest[0],
        latest_output_tokens=latest[1],
        latest_cache_creation_input_tokens=latest[2],
        latest_cache_read_input_tokens=latest[3],
        latest_total_tokens_billed=latest_total_billed,
        latest_context_triplet_tokens=latest[4],
        recent_peak_context_triplet_tokens=recent_peak,
        session_peak_context_triplet_tokens=session_peak,
        text_estimate_tokens=text_estimate_tokens,
        context_estimate_source=context_estimate_source,
    )
