from __future__ import annotations

import json
from pathlib import Path

import pytest

from obs_agent.jsonl_fork import fork_session_jsonl


def _write_lines(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fork_session_jsonl_preserves_raw_jsonl_lines(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-workspace-recursive-harness"
    source_path = project_dir / "sid-root.jsonl"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    raw_lines = [
        '{"type":"queue-operation","operation":"dequeue"}',
        '{"uuid":"u1","parentUuid":null,"message":{"role":"user","content":"start"}}',
        '{"uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":[{"type":"thinking","thinking":"keep exact","signature":"sig"},{"type":"text","text":"done"}]},"extra":{"z":1,"a":2}}',
    ]
    source_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    fork_session_jsonl(
        session_id="sid-root",
        target_uuid="a1",
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
        new_session_id="sid-fork",
    )

    assert (project_dir / "sid-fork.jsonl").read_text(encoding="utf-8").splitlines() == raw_lines


def test_fork_session_jsonl_copies_only_the_target_parent_chain(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-Users-breedoon-Documents-obs"
    source_path = project_dir / "sid-root.jsonl"

    entries = [
        {"type": "queue-operation", "operation": "dequeue"},
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "sid-root",
            "message": {"role": "user", "content": "start"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "sid-root",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
        },
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": "a1",
            "sessionId": "sid-root",
            "message": {"role": "user", "content": "branch a"},
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "u2",
            "sessionId": "sid-root",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
        },
        {
            "type": "user",
            "uuid": "u-dead",
            "parentUuid": "a1",
            "sessionId": "sid-root",
            "message": {"role": "user", "content": "dead fork"},
        },
        {
            "type": "assistant",
            "uuid": "a-dead",
            "parentUuid": "u-dead",
            "sessionId": "sid-root",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "dead"}]},
        },
    ]
    _write_lines(source_path, entries)

    fork_session_id = fork_session_jsonl(
        session_id="sid-root",
        target_uuid="a2",
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
        new_session_id="sid-fork",
    )

    assert fork_session_id == "sid-fork"
    forked_entries = _read_lines(project_dir / "sid-fork.jsonl")
    assert [entry.get("uuid") for entry in forked_entries if entry.get("uuid")] == [
        "u1",
        "a1",
        "u2",
        "a2",
    ]
    assert forked_entries[0]["type"] == "queue-operation"
    assert all(entry.get("uuid") != "a-dead" for entry in forked_entries)


def test_fork_session_jsonl_raises_for_unknown_uuid(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    source_path = projects_root / "-Users-breedoon-Documents-obs" / "sid-root.jsonl"
    _write_lines(
        source_path,
        [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "sid-root",
                "message": {"role": "user", "content": "start"},
            }
        ],
    )

    with pytest.raises(KeyError, match="UUID not found"):
        fork_session_jsonl(
            session_id="sid-root",
            target_uuid="missing",
            cwd=Path("/workspace/recursive-harness"),
            projects_root=projects_root,
        )
