from __future__ import annotations

import json
from pathlib import Path

import pytest

from obs_agent.jsonl_fork import fork_session_jsonl, resolve_session_source


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
        '{"type":"user","uuid":"u1","parentUuid":null,"message":{"role":"user","content":"start"}}',
        '{"type":"assistant","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":[{"type":"thinking","thinking":"keep exact","signature":"sig"},{"type":"text","text":"done"}],"stop_reason":"end_turn"},"extra":{"z":1,"a":2}}',
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
            "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}], "stop_reason": "end_turn"},
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
            "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}], "stop_reason": "end_turn"},
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
            "message": {"role": "assistant", "content": [{"type": "text", "text": "dead"}], "stop_reason": "end_turn"},
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


def test_resolve_session_source_accepts_explicit_jsonl_path(tmp_path: Path) -> None:
    source_path = tmp_path / "foreign.jsonl"
    _write_lines(
        source_path,
        [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "foreign-sid",
                "message": {"role": "user", "content": "start"},
            }
        ],
    )

    descriptor = resolve_session_source(str(source_path), cwd=tmp_path)

    assert descriptor.kind == "jsonl_path"
    assert descriptor.source_session_id == "foreign-sid"
    assert descriptor.source_jsonl_path == source_path.resolve(strict=False)


def test_fork_session_jsonl_uses_explicit_source_path(tmp_path: Path) -> None:
    source_path = tmp_path / "foreign.jsonl"
    _write_lines(
        source_path,
        [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "foreign-sid",
                "message": {"role": "user", "content": "start"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "sessionId": "foreign-sid",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
            },
        ],
    )

    fork_session_jsonl(
        session_id="foreign-sid",
        target_uuid="a1",
        cwd=tmp_path,
        source_path=source_path,
        new_session_id="seeded-child",
    )

    assert (tmp_path / "seeded-child.jsonl").exists()
    forked_entries = _read_lines(tmp_path / "seeded-child.jsonl")
    assert [entry.get("uuid") for entry in forked_entries] == ["u1", "a1"]


# Dead-topic regression (2026-10-12): a recovery fork must not reject inherited
# damage behind a confirmed complete boundary, and must copy it byte-for-byte.
def _dead_topic_session(tmp_path: Path, middle: dict) -> tuple[Path, list[str]]:
    def user(uid, parent, text, ts):
        return {"type": "user", "uuid": uid, "parentUuid": parent, "sessionId": "s", "timestamp": ts,
                "message": {"role": "user", "content": text}}

    def assistant(uid, parent, mid, content, ts, **extra):
        return {"type": "assistant", "uuid": uid, "parentUuid": parent, "sessionId": "s", "timestamp": ts,
                "message": {"id": mid, "role": "assistant", "model": extra.pop("model", "claude"),
                            "content": content, "stop_reason": "end_turn"}, **extra}

    entries = [
        user("u1", None, "start", "2026-10-08T00:00:01Z"),
        middle,
        user("u2", "a1", "next", "2026-10-08T00:00:03Z"),
        assistant("a2", "u2", "m2", [{"type": "text", "text": "ok"}], "2026-10-08T00:00:04Z"),
        user("u3", "a2", "again", "2026-10-12T00:00:05Z"),
        assistant("a3", "u3", "m3", [{"type": "text", "text": "API Error: 401 OAuth token revoked"}],
                  "2026-10-12T00:00:06Z", model="<synthetic>", isApiErrorMessage=True),
    ]
    entries[1].update({"uuid": "a1", "parentUuid": "u1", "sessionId": "s", "timestamp": "2026-10-08T00:00:02Z"})
    raw = [json.dumps(entry, separators=(",", ":")) for entry in entries]
    path = tmp_path / "projects" / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(raw) + "\n", encoding="utf-8")
    return path, raw


@pytest.mark.parametrize("middle", [
    pytest.param({"type": "assistant", "message": {"id": "m1", "role": "assistant", "model": "claude",
                  "content": [{"type": "tool_use", "id": "T1", "name": "context_info", "input": {}}],
                  "stop_reason": "tool_use"}}, id="unanswered-tool-use"),
    pytest.param({"type": "assistant",
                  "message": {"id": "m1", "role": "assistant", "model": "<synthetic>",
                              "content": [{"type": "text", "text": "No response requested."}]}},
                 id="synthetic-row"),
])
def test_recovery_fork_keeps_inherited_damage_verbatim(tmp_path: Path, monkeypatch, middle: dict) -> None:
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    monkeypatch.setenv("OBS_SESSION_REPLAY_PROVENANCE_DIR", str(tmp_path / "prov"))
    source, raw = _dead_topic_session(tmp_path, middle)
    target = resolve_safe_jsonl_target(session_id="s", cwd=tmp_path, source_path=source)
    assert target.health.needs_recovery and target.target_uuid == "u3"

    fork_session_jsonl(session_id="s", target_uuid=target.target_uuid, cwd=tmp_path,
                       source_path=source, new_session_id="f")

    # Byte-identical prefix: every original row up to the boundary, unchanged, no overlays.
    assert (source.parent / "f.jsonl").read_text(encoding="utf-8").splitlines() == raw[:5]
    receipt = json.loads((tmp_path / "prov" / "forks" / "f.json").read_text())
    assert receipt["replay"]["legacy_prefix"]["policy"] == "opaque_original_ancestry_preserved_complete_suffix_only"


def test_replay_explicit_target_must_be_complete_boundary(tmp_path: Path, monkeypatch) -> None:
    from obs_agent.jsonl_replay import prepare_session_replay

    monkeypatch.setenv("OBS_SESSION_REPLAY_PROVENANCE_DIR", str(tmp_path / "prov"))
    middle = {"type": "assistant", "message": {"id": "m1", "role": "assistant", "model": "claude",
              "content": [{"type": "tool_use", "id": "T1", "name": "x", "input": {}}], "stop_reason": "tool_use"}}
    source, _raw = _dead_topic_session(tmp_path, middle)
    source.write_text("\n".join(_raw[:2]) + "\n", encoding="utf-8")  # target's own round is incomplete
    with pytest.raises(ValueError, match="incomplete tool round"):
        prepare_session_replay(session_id="s", cwd=tmp_path, source_path=source, target_uuid="a1")


def _branch_entries() -> list[dict]:
    def row(kind: str, uid: str, parent: str | None, content, stop: str | None = None) -> dict:
        message: dict = {"role": kind, "content": content}
        if stop:
            message["stop_reason"] = stop
        return {"type": kind, "uuid": uid, "parentUuid": parent, "sessionId": "sid-root", "message": message}

    return [
        row("user", "u1", None, "start"),
        row("assistant", "a1", "u1", [{"type": "text", "text": "one"}], "end_turn"),
        row("user", "u2", "a1", "branch a"),
        row("assistant", "a2", "u2", [{"type": "text", "text": "two"}], "end_turn"),
        row("user", "u2b", "a2", "branch a again"),
        row("assistant", "a2b", "u2b", [{"type": "text", "text": "partial"}]),
        row("user", "u-dead", "a1", "later branch"),
        row("assistant", "a-dead", "u-dead", [{"type": "text", "text": "dead"}], "end_turn"),
    ]


def test_inactive_branch_target_resolves_on_its_own_branch(tmp_path: Path) -> None:
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    source_path = tmp_path / "sid-root.jsonl"
    _write_lines(source_path, _branch_entries())

    finished = resolve_safe_jsonl_target(session_id="sid-root", cwd=tmp_path, source_path=source_path,
                                         preferred_uuid="a2")
    assert finished is not None
    assert (finished.target_uuid, finished.changed, finished.reason) == ("a2", False, None)
    assert finished.branch_health is not None
    assert "a-dead" in finished.health.chain_uuids

    # An in-flight row on the inactive branch moves to that branch's own last
    # boundary (R1: u2b), never onto the active chain.
    in_flight = resolve_safe_jsonl_target(session_id="sid-root", cwd=tmp_path, source_path=source_path,
                                          preferred_uuid="a2b")
    assert in_flight is not None
    assert (in_flight.target_uuid, in_flight.reason) == ("u2b", "complete_message_boundary")

    fork_id = fork_session_jsonl(session_id="sid-root", target_uuid="a2b", cwd=tmp_path,
                                 source_path=source_path, new_session_id="sid-fork")
    assert [e["uuid"] for e in _read_lines(tmp_path / f"{fork_id}.jsonl")] == ["u1", "a1", "u2", "a2", "u2b"]


def test_active_chain_target_is_unchanged_by_branch_rule(tmp_path: Path) -> None:
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    source_path = tmp_path / "sid-root.jsonl"
    _write_lines(source_path, _branch_entries())

    target = resolve_safe_jsonl_target(session_id="sid-root", cwd=tmp_path, source_path=source_path,
                                       preferred_uuid="a-dead")
    assert target is not None
    assert (target.target_uuid, target.changed, target.branch_health) == ("a-dead", False, None)


def test_fork_session_jsonl_unknown_uuid_writes_no_fork(tmp_path: Path) -> None:
    source_path = tmp_path / "sid-root.jsonl"
    _write_lines(source_path, _branch_entries())
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(KeyError, match="UUID not found"):
        fork_session_jsonl(session_id="sid-root", target_uuid="missing", cwd=tmp_path,
                           source_path=source_path, new_session_id="sid-fork")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
