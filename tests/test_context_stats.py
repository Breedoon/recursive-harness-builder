"""Tests for shared session/context snapshot formatting."""

import json
from pathlib import Path

import pytest

from obs_agent.context_probe import ContextProbe
from obs_agent.context_stats import (
    build_context_snapshot,
    format_context_snapshot_compact,
    format_context_snapshot_lines,
)
from obs_agent.context_stats import apply_context_probe


def test_build_context_snapshot_includes_billing_and_context_estimate() -> None:
    snapshot = build_context_snapshot(
        session_id="abc",
        data={
            "session_id": "abc",
            "num_turns": 4,
            "total_cost_usd": 1.25,
            "duration_ms": 3210,
            "usage": {
                "input_tokens": 6,
                "output_tokens": 820,
                "cache_creation_input_tokens": 1648,
                "cache_read_input_tokens": 449_996,
            },
        },
        context_window_estimate_tokens=500_000,
    )

    assert snapshot["total_tokens_billed"] == 452_470
    assert snapshot["estimated_context_used_tokens"] == 449_996
    assert snapshot["estimated_context_remaining_tokens"] == 50_004
    assert snapshot["estimated_context_remaining_pct"] == pytest.approx(10.0008, rel=1e-6)


def test_build_context_snapshot_normalizes_impossible_cache_read_by_turns() -> None:
    snapshot = build_context_snapshot(
        session_id="abc",
        data={
            "session_id": "abc",
            "num_turns": 8,
            "total_cost_usd": 1.25,
            "duration_ms": 3210,
            "usage": {
                "input_tokens": 4,
                "output_tokens": 322,
                "cache_creation_input_tokens": 992,
                "cache_read_input_tokens": 309_000,
            },
        },
        context_window_estimate_tokens=200_000,
    )

    # 309k over a 200k configured window is treated as multi-turn aggregate.
    # Normalized by num_turns (8) => 38_625.
    assert snapshot["estimated_context_used_tokens"] == 38_625
    assert snapshot["estimated_context_remaining_tokens"] == 161_375
    assert snapshot["estimated_context_remaining_pct"] == pytest.approx(80.6875, rel=1e-6)


def test_format_context_snapshot_lines_contains_expected_fields() -> None:
    snapshot = build_context_snapshot(
        session_id="sid-1",
        data={
            "num_turns": 2,
            "total_cost_usd": 0.12,
            "duration_ms": 1000,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            },
        },
        context_window_estimate_tokens=200_000,
    )
    text = "\n".join(format_context_snapshot_lines(snapshot))

    assert "session_id: sid-1" in text
    assert "input_tokens: 10" in text
    assert "output_tokens: 20" in text
    assert "cache_creation_input_tokens: 30" in text
    assert "cache_read_input_tokens: 40" in text
    assert "total_tokens_billed: 100" in text
    assert "estimated_context_window_tokens: 200000" in text
    assert "estimated_context_used_tokens: 40" in text
    assert "estimated_context_remaining_tokens: 199960" in text
    assert "context_estimate_source: usage_fallback" in text


def test_apply_context_probe_overrides_estimate_fields() -> None:
    snapshot = build_context_snapshot(
        session_id="sid-1",
        data={
            "num_turns": 2,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            },
        },
        context_window_estimate_tokens=200_000,
    )
    updated = apply_context_probe(
        snapshot,
        ContextProbe(used_tokens=37_000, window_tokens=200_000, used_pct=18.0),
    )
    text = "\n".join(format_context_snapshot_lines(updated))
    assert "estimated_context_used_tokens: 37000" in text
    assert "estimated_context_remaining_tokens: 163000" in text
    assert "context_estimate_source: claude_cli_context" in text


def test_apply_context_probe_none_preserves_existing_source() -> None:
    snapshot = {
        "context_estimate_source": "jsonl_latest_triplet",
        "estimated_context_window_tokens": 200_000,
        "estimated_context_used_tokens": 30_000,
    }
    updated = apply_context_probe(snapshot, None)
    assert updated["context_estimate_source"] == "jsonl_latest_triplet"


def test_format_context_snapshot_compact_rounds_for_telegram() -> None:
    snapshot = {
        "estimated_context_used_tokens": 24_825,
        "estimated_context_window_tokens": 200_000,
    }
    assert format_context_snapshot_compact(snapshot) == "context: 24k"


def test_format_context_snapshot_compact_shows_unavailable_for_zero_context() -> None:
    snapshot = {
        "estimated_context_used_tokens": 0,
        "estimated_context_window_tokens": 1_000_000,
    }
    assert format_context_snapshot_compact(snapshot) == "context: context unavailable"


def test_build_context_snapshot_prefers_jsonl_triplet_for_context_estimate(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    session_id = "sid-jsonl"
    session_file = projects_root / "-Users-breedoon-Documents-obs-fixture-vault" / f"{session_id}.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 3,
                    "cache_creation_input_tokens": 222,
                    "cache_read_input_tokens": 3333,
                }
            },
        }
    ]
    session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    snapshot = build_context_snapshot(
        session_id=session_id,
        data={
            "session_id": session_id,
            "num_turns": 7,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 99999,
            },
        },
        context_window_estimate_tokens=200_000,
        cwd=Path("/workspace/recursive-harness/fixture_project"),
        projects_root=projects_root,
    )

    assert snapshot["context_estimate_source"] == "jsonl_latest_triplet"
    assert snapshot["estimated_context_used_tokens"] == 3566
    assert snapshot["cache_read_input_tokens"] == 3333
    # Raw SDK usage is still preserved for direct telemetry visibility.
    assert snapshot["sdk_cache_read_input_tokens"] == 99999


def test_build_context_snapshot_falls_back_when_jsonl_has_no_usage(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    session_id = "sid-empty"
    session_file = projects_root / "-Users-breedoon-Documents-obs" / f"{session_id}.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps({"type": "assistant", "sessionId": session_id}) + "\n")

    snapshot = build_context_snapshot(
        session_id=session_id,
        data={
            "session_id": session_id,
            "num_turns": 1,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            },
        },
        context_window_estimate_tokens=200_000,
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
    )

    assert snapshot["context_estimate_source"] == "usage_fallback"
    assert snapshot["estimated_context_used_tokens"] == 40
