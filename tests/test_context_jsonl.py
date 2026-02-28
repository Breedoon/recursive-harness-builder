"""Tests for JSONL-backed context usage extraction."""

from __future__ import annotations

import json
from pathlib import Path

from obs_agent.context_jsonl import find_session_jsonl, load_jsonl_usage_snapshot


def _write_lines(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def test_find_session_jsonl_prefers_matching_workspace_dir(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    sid = "abc-session"
    preferred = projects_root / "-Users-breedoon-Documents-obs-fixture-vault" / f"{sid}.jsonl"
    other = projects_root / "-Users-breedoon-Documents-obs" / f"{sid}.jsonl"
    _write_lines(other, [])
    _write_lines(preferred, [])

    found = find_session_jsonl(
        session_id=sid,
        cwd=Path("/Users/breedoon/Documents/obs/fixture_vault"),
        projects_root=projects_root,
    )
    assert found == preferred


def test_load_jsonl_usage_snapshot_returns_none_when_file_missing(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    result = load_jsonl_usage_snapshot(
        session_id="missing",
        cwd=Path("/Users/breedoon/Documents/obs"),
        projects_root=projects_root,
    )
    assert result is None


def test_load_jsonl_usage_snapshot_returns_none_when_no_usage_entries(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    sid = "sid-1"
    session_file = projects_root / "-Users-breedoon-Documents-obs" / f"{sid}.jsonl"
    _write_lines(
        session_file,
        [
            {"type": "assistant", "sessionId": sid, "message": {"content": [{"type": "text", "text": "x"}]}},
            {"type": "assistant", "sessionId": sid, "message": {"usage": "bad-shape"}},
        ],
    )

    result = load_jsonl_usage_snapshot(
        session_id=sid,
        cwd=Path("/Users/breedoon/Documents/obs"),
        projects_root=projects_root,
    )
    assert result is None


def test_load_jsonl_usage_snapshot_ignores_other_sessions_and_returns_latest(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    sid = "sid-1"
    session_file = projects_root / "-Users-breedoon-Documents-obs" / f"{sid}.jsonl"
    _write_lines(
        session_file,
        [
            {
                "type": "assistant",
                "sessionId": "different-sid",
                "message": {
                    "usage": {
                        "input_tokens": 999,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 1,
                        "cache_read_input_tokens": 1,
                    }
                },
            },
            {
                "type": "assistant",
                "sessionId": sid,
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 1000,
                    }
                },
            },
            {
                "type": "assistant",
                "sessionId": sid,
                "message": {
                    "usage": {
                        "input_tokens": 15,
                        "output_tokens": 7,
                        "cache_creation_input_tokens": 120,
                        "cache_read_input_tokens": 1200,
                    }
                },
            },
        ],
    )

    result = load_jsonl_usage_snapshot(
        session_id=sid,
        cwd=Path("/Users/breedoon/Documents/obs"),
        projects_root=projects_root,
    )

    assert result is not None
    assert result.latest_input_tokens == 15
    assert result.latest_output_tokens == 7
    assert result.latest_cache_creation_input_tokens == 120
    assert result.latest_cache_read_input_tokens == 1200
    assert result.latest_context_triplet_tokens == 1335
    assert result.recent_peak_context_triplet_tokens == 1335
    assert result.session_peak_context_triplet_tokens == 1335
    assert result.usage_entries == 2
