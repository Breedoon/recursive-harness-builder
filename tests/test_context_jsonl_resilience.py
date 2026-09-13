"""Offline regressions for untrusted and concurrently changing session files."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obs_agent.context_jsonl import (
    _as_int,
    _encode_project_path,
    find_session_jsonl,
    find_session_jsonl_index,
    load_jsonl_usage_snapshot,
)


def _write_session(projects_root: Path, project_name: str, session_id: str) -> Path:
    session_path = projects_root / project_name / f"{session_id}.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 37}},
        })
        + "\n",
        encoding="utf-8",
    )
    return session_path


@pytest.mark.parametrize(
    "record",
    [None, [], 12, True, "text", {"type": [], "message": {}}, {"type": {}, "message": {}}],
)
def test_non_event_json_does_not_hide_later_usage(tmp_path: Path, record: object) -> None:
    projects_root = tmp_path / "projects"
    session_path = _write_session(projects_root, "project", "session")
    valid_event = session_path.read_text(encoding="utf-8")
    session_path.write_text(json.dumps(record) + "\n" + valid_event, encoding="utf-8")

    snapshot = load_jsonl_usage_snapshot(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    )

    assert snapshot is not None
    assert snapshot.latest_input_tokens == 37
    assert snapshot.assistant_entries == 1


def test_invalid_utf8_does_not_hide_later_usage(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_path = _write_session(projects_root, "project", "session")
    session_path.write_bytes(b"\xffcorrupt record\n" + session_path.read_bytes())

    snapshot = load_jsonl_usage_snapshot(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    )

    assert snapshot is not None
    assert snapshot.latest_input_tokens == 37


@pytest.mark.parametrize("value", [True, False, -1, -10_000, 1.5, "20", None])
def test_usage_counters_reject_non_counts(value: object) -> None:
    assert _as_int(value) == 0


@pytest.mark.parametrize("value", [0, 1, 123_456])
def test_usage_counters_preserve_nonnegative_integers(value: int) -> None:
    assert _as_int(value) == value


def test_negative_counter_cannot_cancel_reported_context(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_path = _write_session(projects_root, "project", "session")
    session_path.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": -50_000,
                "cache_read_input_tokens": 50_000,
            }},
        }) + "\n",
        encoding="utf-8",
    )

    snapshot = load_jsonl_usage_snapshot(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    )

    assert snapshot is not None
    assert snapshot.latest_context_triplet_tokens == 50_000


@pytest.mark.parametrize("absolute", [False, True])
def test_session_id_cannot_escape_project_directory(tmp_path: Path, absolute: bool) -> None:
    projects_root = tmp_path / "projects"
    preferred = projects_root / _encode_project_path(tmp_path)
    preferred.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    session_id = str(outside.with_suffix("")) if absolute else "../../outside"

    assert find_session_jsonl(
        session_id=session_id, cwd=tmp_path, projects_root=projects_root
    ) is None


def test_invalid_identity_does_not_interfere_with_valid_lookup(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    valid_path = _write_session(projects_root, "project", "valid-session")
    identities = {"valid-session", "../outside", "..\\outside", "bad\x00name"}

    found = find_session_jsonl_index(
        session_ids=identities, cwd=tmp_path, projects_root=projects_root
    )

    assert found["valid-session"] == valid_path
    assert all(found[identity] is None for identity in identities - {"valid-session"})


def test_disappearing_duplicate_does_not_hide_readable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root = tmp_path / "projects"
    disappearing = _write_session(projects_root, "project-a", "session")
    readable = _write_session(projects_root, "project-b", "session")
    real_stat = Path.stat
    stat_calls = 0

    def concurrent_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal stat_calls
        if path == disappearing:
            stat_calls += 1
            if stat_calls > 1:
                raise FileNotFoundError("concurrent cleanup")
            info = real_stat(path, *args, **kwargs)
            # The readable copy wins even if this first observation is retained.
            fields = list(info)
            fields[8] = 0
            return os.stat_result(fields)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", concurrent_stat)

    assert find_session_jsonl(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    ) == readable


def test_unreadable_project_does_not_hide_other_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root = tmp_path / "projects"
    inaccessible = projects_root / "project-a"
    inaccessible.mkdir(parents=True)
    readable = _write_session(projects_root, "project-b", "session")
    real_is_dir = Path.is_dir
    real_iterdir = Path.iterdir

    def guarded_is_dir(path: Path) -> bool:
        if path == inaccessible:
            raise PermissionError("project is not readable")
        return real_is_dir(path)

    def ordered_iterdir(path: Path):
        if path == projects_root:
            return iter([inaccessible, readable.parent])
        return real_iterdir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    monkeypatch.setattr(Path, "iterdir", ordered_iterdir)

    assert find_session_jsonl(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    ) == readable


def test_preferred_workspace_still_wins_over_newer_fallback(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    preferred = _write_session(projects_root, _encode_project_path(tmp_path), "session")
    fallback = _write_session(projects_root, "other-project", "session")
    os.utime(preferred, (1, 1))
    os.utime(fallback, (100, 100))

    assert find_session_jsonl(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    ) == preferred


def test_equal_mtime_duplicates_have_deterministic_order(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    first = _write_session(projects_root, "project-a", "session")
    second = _write_session(projects_root, "project-b", "session")
    os.utime(first, (100, 100))
    os.utime(second, (100, 100))

    assert find_session_jsonl(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    ) == second


def test_trailing_zero_usage_retains_positive_copied_prefix(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_path = _write_session(projects_root, "project", "session")
    events = [
        {"type": "assistant", "sessionId": "parent", "message": {"usage": {
            "input_tokens": 100, "cache_read_input_tokens": 200,
        }}},
        {"type": "assistant", "sessionId": "session", "message": {"usage": {
            "input_tokens": 0,
        }}},
    ]
    session_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    snapshot = load_jsonl_usage_snapshot(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    )

    assert snapshot is not None
    assert snapshot.latest_context_triplet_tokens == 300
    assert snapshot.usage_entries == 2
    assert snapshot.context_estimate_source == "jsonl_latest_positive_triplet"


def test_text_only_transcript_still_has_context_estimate(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_path = _write_session(projects_root, "project", "session")
    session_path.write_text(
        json.dumps({"type": "user", "message": {"content": "x" * 400}}) + "\n",
        encoding="utf-8",
    )

    snapshot = load_jsonl_usage_snapshot(
        session_id="session", cwd=tmp_path, projects_root=projects_root
    )

    assert snapshot is not None
    assert snapshot.latest_context_triplet_tokens == 100
    assert snapshot.context_estimate_source == "jsonl_text_estimate"
