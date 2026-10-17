from __future__ import annotations

import json
from pathlib import Path

from obs_agent.jsonl_fork import fork_session_jsonl
from obs_agent.jsonl_health import analyze_jsonl_path, is_stale_session_head, resolve_safe_jsonl_target


def _write_raw(path: Path, raw_lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")


def _entry(
    entry_type: str,
    uuid: str,
    parent: str | None,
    *,
    role: str | None = None,
    content: object = "",
    model: str | None = None,
    is_api_error: bool | None = None,
    stop_reason: str | None = None,
) -> str:
    obj: dict[str, object] = {
        "type": entry_type,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "sid-root",
    }
    if role is not None:
        message: dict[str, object] = {"role": role, "content": content}
        if model is not None:
            message["model"] = model
        if stop_reason is not None:
            message["stop_reason"] = stop_reason
        obj["message"] = message
    if is_api_error is not None:
        obj["isApiErrorMessage"] = is_api_error
    return json.dumps(obj, separators=(",", ":"))


def _poison(uuid: str, parent: str) -> str:
    return _entry(
        "assistant",
        uuid,
        parent,
        role="assistant",
        model="<synthetic>",
        content=[{"type": "text", "text": "Prompt is too long"}],
        is_api_error=True,
    )


def _assistant_text(uuid: str, parent: str, text: str = "healthy") -> str:
    return _entry(
        "assistant",
        uuid,
        parent,
        role="assistant",
        model="claude-opus-4-7",
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
    )


def _empty_assistant(uuid: str, parent: str) -> str:
    return _entry(
        "assistant",
        uuid,
        parent,
        role="assistant",
        model="claude-opus-4-7",
        content=[],
    )


def _tool_only_assistant(uuid: str, parent: str) -> str:
    return _entry(
        "assistant",
        uuid,
        parent,
        role="assistant",
        model="claude-opus-4-7",
        content=[{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
        stop_reason="tool_use",
    )


# Recovery boundary rules since 3ae5ea1 (``jsonl_health._completed_boundaries``):
# - a persisted plain user message is a boundary ("Do not replace that request on
#   recovery"); 653c463's own regression (test_jsonl_fork
#   test_recovery_fork_keeps_inherited_damage_verbatim) recovers to such a row;
# - a tool round is a boundary once every tool_use has its tool_result
#   (docs/cache-proxy-safety.md: recovery_multi targets a tool result);
# - a text-only assistant group needs terminal evidence (stop_reason end_turn /
#   stop_sequence / max_tokens, a later assistant group, or a completion receipt).
# Fixtures therefore mark finished answers with stop_reason, and expectations that
# predate these rules were updated in obs-6 (each noted below).


def test_analyze_jsonl_path_detects_poison_tail_and_safe_recovery_boundary(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    raw_lines = [
        '{"type":"queue-operation","operation":"dequeue"}',
        _entry("user", "u1", None, role="user", content="start"),
        _entry(
            "assistant",
            "a1",
            "u1",
            role="assistant",
            model="gpt-5.5",
            content=[{"type": "text", "text": "healthy one"}],
            stop_reason="end_turn",
        ),
        _entry("user", "u2", "a1", role="user", content="launch"),
        _entry(
            "assistant",
            "a2",
            "u2",
            role="assistant",
            model="gpt-5.5",
            content=[{"type": "text", "text": "about to call a tool"}],
        ),
        _entry(
            "assistant",
            "a-tool",
            "a2",
            role="assistant",
            model="gpt-5.5",
            content=[{"type": "tool_use", "id": "call-1", "name": "AgentTask"}],
            stop_reason="tool_use",
        ),
        _entry(
            "user",
            "u-tool-result",
            "a-tool",
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
        ),
        _poison("err1", "u-tool-result"),
        _entry("user", "u3", "err1", role="user", content="are you alive?"),
        _poison("err2", "u3"),
    ]
    _write_raw(path, raw_lines)

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    # obs-6: the AgentTask round is complete (its tool_result is persisted), so it
    # is kept and the unsafe tail starts at the first poison row (was: a-tool tail,
    # recovery to a2, which dropped a completed tool effect).
    assert health.needs_recovery is True
    assert health.first_poison_uuid == "err1"
    assert health.first_unsafe_tail_uuid == "err1"
    assert health.unsafe_tail_reason == "poisoned_tail"
    assert health.last_poison_uuid == "err2"
    assert health.last_real_assistant_uuid == "a-tool"
    assert health.safe_recovery_uuid == "u-tool-result"
    assert health.is_uuid_safe("a1") is True
    assert health.is_uuid_safe("a-tool") is True
    assert health.is_uuid_safe("u-tool-result") is True
    assert health.is_uuid_safe("err1") is False
    assert health.is_uuid_safe("err2") is False
    assert health.is_uuid_safe("u3") is False


def test_analyze_jsonl_path_keeps_persisted_dangling_user_message(tmp_path: Path) -> None:
    # obs-6: was test_analyze_jsonl_path_recovers_dangling_user_tail. Since 3ae5ea1 a
    # persisted plain user message is a safe boundary, so it is no recovery case.
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    raw_lines = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy answer"),
        _entry("user", "u2", "a1", role="user", content="this never got an answer"),
    ]
    _write_raw(path, raw_lines)

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is False
    assert health.first_poison_uuid is None
    assert health.first_unsafe_tail_uuid is None
    assert health.unsafe_tail_reason is None
    assert health.safe_recovery_uuid is None
    assert health.is_uuid_safe("a1") is True
    assert health.is_uuid_safe("u2") is True

    target = resolve_safe_jsonl_target(
        session_id="sid-root", cwd=Path("/workspace"), projects_root=path.parents[1],
    )
    assert target is not None
    assert target.target_uuid == "u2"


def test_analyze_jsonl_path_recovers_empty_assistant_tail(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    raw_lines = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy answer"),
        _entry("user", "u2", "a1", role="user", content="please continue"),
        _empty_assistant("a-empty", "u2"),
    ]
    _write_raw(path, raw_lines)

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="a-empty",
        projects_root=path.parents[1],
    )

    # obs-6: the persisted request u2 is kept; only the empty answer is unsafe
    # (was: tail from u2, recovery to a1).
    assert target is not None
    assert target.health.needs_recovery is True
    assert target.health.first_unsafe_tail_uuid == "a-empty"
    assert target.health.unsafe_tail_reason == "empty_assistant_tail"
    assert target.target_uuid == "u2"
    assert target.changed is True
    assert target.reason == "preferred_uuid_unsafe"
    assert target.health.is_uuid_safe("a-empty") is False


def test_analyze_jsonl_path_recovers_unfinished_tool_tail(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    raw_lines = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy answer"),
        _entry("user", "u2", "a1", role="user", content="inspect file"),
        _tool_only_assistant("a-tool", "u2"),
    ]
    _write_raw(path, raw_lines)

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    # obs-6: the persisted request u2 is kept; the unanswered tool call is the
    # unsafe tail (was: tail from u2, recovery to a1).
    assert health.needs_recovery is True
    assert health.first_unsafe_tail_uuid == "a-tool"
    assert health.unsafe_tail_reason == "incomplete_tool_use_tail"
    assert health.safe_recovery_uuid == "u2"
    assert health.is_uuid_safe("u2") is True
    assert health.is_uuid_safe("a-tool") is False


def test_resolve_safe_jsonl_target_keeps_safe_reply_before_poison(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    path = projects_root / "-workspace" / "sid-root.jsonl"
    _write_raw(
        path,
        [
            _entry("user", "u1", None, role="user", content="start"),
            _entry(
                "assistant",
                "a1",
                "u1",
                role="assistant",
                model="gpt-5.5",
                content=[{"type": "text", "text": "healthy"}],
                stop_reason="end_turn",
            ),
            _entry("user", "u2", "a1", role="user", content="later"),
            _poison("err1", "u2"),
        ],
    )

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="a1",
        projects_root=projects_root,
    )

    assert target is not None
    assert target.target_uuid == "a1"
    assert target.changed is False


def test_resolve_safe_jsonl_target_rewrites_poisoned_preferred_uuid(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    path = projects_root / "-workspace" / "sid-root.jsonl"
    _write_raw(
        path,
        [
            _entry("user", "u1", None, role="user", content="start"),
            _entry(
                "assistant",
                "a1",
                "u1",
                role="assistant",
                model="gpt-5.5",
                content=[{"type": "text", "text": "healthy"}],
                stop_reason="end_turn",
            ),
            _entry("user", "u2", "a1", role="user", content="later"),
            _poison("err1", "u2"),
            _entry("user", "u3", "err1", role="user", content="still there?"),
            _poison("err2", "u3"),
        ],
    )

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="err2",
        projects_root=projects_root,
    )

    # obs-6: recovery keeps the persisted request before the first poison row
    # (was: a1). Nothing descending from err1 may be selected.
    assert target is not None
    assert target.target_uuid == "u2"
    assert target.changed is True
    assert target.reason == "preferred_uuid_unsafe"
    assert target.health.is_uuid_safe("u3") is False


def test_analyze_jsonl_path_ignores_recovered_errors_in_middle(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    path = projects_root / "-workspace" / "sid-root.jsonl"
    _write_raw(
        path,
        [
            _entry("user", "u1", None, role="user", content="start"),
            _entry(
                "assistant",
                "a1",
                "u1",
                role="assistant",
                model="gpt-5.5",
                content=[{"type": "text", "text": "healthy"}],
                stop_reason="end_turn",
            ),
            _entry("user", "u2", "a1", role="user", content="bad"),
            _poison("err1", "u2"),
            _entry("user", "u3", "err1", role="user", content="recovered"),
            _entry(
                "assistant",
                "a2",
                "u3",
                role="assistant",
                model="gpt-5.5",
                content=[{"type": "text", "text": "real recovery"}],
                stop_reason="end_turn",
            ),
        ],
    )

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is False
    assert health.safe_recovery_uuid is None
    assert health.last_real_assistant_uuid == "a2"
    assert health.is_uuid_safe("err1") is False

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="err1",
        projects_root=projects_root,
    )

    assert target is not None
    assert target.target_uuid == "a2"
    assert target.changed is True
    assert target.reason == "preferred_uuid_unsafe"


def test_stale_session_head_is_a_pin_before_a_recovered_poison_row(tmp_path: Path) -> None:
    # vault-1bc2 round 3: the pre-fix analyzer pinned session heads before err1
    # (prod f896fe4b: head line 661, poison 672, 80 rows of later work).
    path = tmp_path / "sid-root.jsonl"
    _write_raw(
        path,
        [
            _entry("user", "u1", None, role="user", content="start"),
            _assistant_text("a1", "u1"),
            _entry("user", "u2", "a1", role="user", content="bad"),
            _poison("err1", "u2"),
            _entry("user", "u3", "err1", role="user", content="recovered"),
            _assistant_text("a2", "u3", "real recovery"),
            _entry("user", "u4", "a2", role="user", content="next"),
            _poison("err2", "u4"),
        ],
    )
    health = analyze_jsonl_path(path=path, session_id="sid-root")
    assert health.recovered_poison_uuids == frozenset({"err1"})
    assert health.needs_recovery is True
    assert is_stale_session_head(health, "a1") is True
    # Heads at or after the recovered row, unsafe or unknown heads are not pins.
    assert is_stale_session_head(health, "a2") is False
    assert is_stale_session_head(health, "u4") is False
    assert is_stale_session_head(health, "err2") is False
    assert is_stale_session_head(health, "missing") is False
    assert is_stale_session_head(health, None) is False
    # The pin itself is safe, so resolving at it would roll back past a2.
    pinned = resolve_safe_jsonl_target(session_id="sid-root", cwd=tmp_path, source_path=path, preferred_uuid="a1")
    assert pinned is not None and pinned.target_uuid == "a1"
    unpinned = resolve_safe_jsonl_target(session_id="sid-root", cwd=tmp_path, source_path=path, preferred_uuid=None)
    assert unpinned is not None and unpinned.target_uuid == "u4"


def test_stale_session_head_ignores_unrecovered_poison(tmp_path: Path) -> None:
    path = tmp_path / "sid-root.jsonl"
    _write_raw(
        path,
        [
            _entry("user", "u1", None, role="user", content="start"),
            _assistant_text("a1", "u1"),
            _entry("user", "u2", "a1", role="user", content="bad"),
            _poison("err1", "u2"),
        ],
    )
    health = analyze_jsonl_path(path=path, session_id="sid-root")
    assert health.recovered_poison_uuids == frozenset()
    assert is_stale_session_head(health, "a1") is False


def test_resolved_recovery_target_forks_verbatim_without_poison_tail(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-workspace"
    path = project_dir / "sid-root.jsonl"
    raw_lines = [
        '{"type":"queue-operation","operation":"dequeue","weird_spacing":true}',
        _entry("user", "u1", None, role="user", content="start"),
        _entry(
            "assistant",
            "a1",
            "u1",
            role="assistant",
            model="gpt-5.5",
            content=[{"type": "text", "text": "healthy"}],
            stop_reason="end_turn",
        ),
        _entry("user", "u2", "a1", role="user", content="later"),
        _poison("err1", "u2"),
    ]
    _write_raw(path, raw_lines)

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="err1",
        projects_root=projects_root,
    )
    assert target is not None

    fork_session_jsonl(
        session_id="sid-root",
        target_uuid=target.target_uuid or "",
        cwd=Path("/workspace"),
        projects_root=projects_root,
        new_session_id="sid-recovered",
    )

    # obs-6: the boundary is the persisted request u2 (was a1, raw_lines[:3]).
    assert target.target_uuid == "u2"
    forked_lines = (project_dir / "sid-recovered.jsonl").read_text(encoding="utf-8").splitlines()
    assert forked_lines == raw_lines[:4]
    assert all("Prompt is too long" not in line for line in forked_lines)


def test_resolved_recovery_target_forks_verbatim_without_silent_tail(tmp_path: Path) -> None:
    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-workspace"
    path = project_dir / "sid-root.jsonl"
    raw_lines = [
        '{"type":"queue-operation","operation":"dequeue","weird_spacing":true}',
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy"),
        _entry("user", "u2", "a1", role="user", content="this completed silently"),
        _empty_assistant("a-empty", "u2"),
    ]
    _write_raw(path, raw_lines)

    target = resolve_safe_jsonl_target(
        session_id="sid-root",
        cwd=Path("/workspace"),
        preferred_uuid="a-empty",
        projects_root=projects_root,
    )
    # obs-6: the boundary is the persisted request u2 (was a1); the empty
    # assistant answer is still excluded.
    assert target is not None
    assert target.target_uuid == "u2"

    fork_session_jsonl(
        session_id="sid-root",
        target_uuid=target.target_uuid or "",
        cwd=Path("/workspace"),
        projects_root=projects_root,
        new_session_id="sid-recovered",
    )

    forked_lines = (project_dir / "sid-recovered.jsonl").read_text(encoding="utf-8").splitlines()
    assert forked_lines == raw_lines[:4]
    assert all("a-empty" not in line for line in forked_lines)


def test_in_flight_text_row_is_not_a_boundary_until_terminal_evidence(tmp_path: Path) -> None:
    # obs-6: cover the 3ae5ea1 rule the older fixtures predate (jsonl_fork
    # CACHE-PREFIX WARNING: "never select an in-flight text-only row"). A final
    # text-only assistant row with no stop_reason, no later assistant group and no
    # completion receipt may still be streaming, so a fork/recovery target must
    # fall back to the persisted request before it; terminal evidence makes it a
    # boundary. Without this test, treating every text row as complete passed.
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    in_flight = _entry(
        "assistant", "a2", "u2", role="assistant", model="claude-opus-4-7",
        content=[{"type": "text", "text": "partial"}],
    )
    finished = _entry(
        "assistant", "a2", "u2", role="assistant", model="claude-opus-4-7",
        content=[{"type": "text", "text": "partial"}], stop_reason="end_turn",
    )
    head = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy answer"),
        _entry("user", "u2", "a1", role="user", content="next"),
    ]

    _write_raw(path, head + [in_flight])
    target = resolve_safe_jsonl_target(
        session_id="sid-root", cwd=Path("/workspace"), preferred_uuid="a2",
        projects_root=path.parents[1],
    )
    assert target is not None
    assert target.target_uuid == "u2"
    assert target.changed is True
    assert target.reason == "complete_message_boundary"
    assert "a2" not in target.health.complete_message_targets

    _write_raw(path, head + [finished])
    target = resolve_safe_jsonl_target(
        session_id="sid-root", cwd=Path("/workspace"), preferred_uuid="a2",
        projects_root=path.parents[1],
    )
    assert target is not None
    assert target.target_uuid == "a2"
    assert target.changed is False


# vault-1bc2: the real CLI chains the next user row to a synthetic API-error
# row (session a00b512b line 79 -> 9142d3fe). A poison row is inherited history
# once a real complete answer descends from it; only unrecovered poison rows
# poison their descendants.
def _timeout(uuid: str, parent: str) -> str:
    obj = json.loads(_entry(
        "assistant", uuid, parent, role="assistant", model="<synthetic>",
        content=[{"type": "text", "text": "Request timed out"}], is_api_error=True,
        stop_reason="stop_sequence",
    ))
    obj["error"] = "unknown"
    return json.dumps(obj, separators=(",", ":"))


def test_recovered_poison_then_new_tail_poison_recovers_to_latest_boundary(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "-workspace" / "sid-root.jsonl"
    raw = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1"),
        _entry("user", "u2", "a1", role="user", content="ask"),
        _timeout("err1", "u2"),
        _entry("user", "u3", "err1", role="user", content="Continue"),
        _assistant_text("a2", "u3", "recovered"),
        _entry("user", "u4", "a2", role="user", content="next"),
        _assistant_text("a3", "u4", "more work"),
        _entry("user", "u5", "a3", role="user", content="last"),
        _timeout("err2", "u5"),
    ]
    _write_raw(path, raw)

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is True
    assert health.unsafe_tail_reason == "poisoned_tail"
    # R1: the persisted user row before the tail poison, not the row before err1.
    assert health.safe_recovery_uuid == "u5"
    assert health.first_poison_uuid == "err2"
    assert health.is_uuid_safe("a3") is True
    assert health.is_uuid_safe("err1") is False
    assert health.is_uuid_safe("err2") is False

    fork_id = fork_session_jsonl(session_id="sid-root", target_uuid=health.safe_recovery_uuid,
                                 cwd=Path("/workspace"), source_path=path, new_session_id="sid-fork")
    forked = (path.parent / f"{fork_id}.jsonl").read_text(encoding="utf-8").splitlines()
    # The recovered err1 stays verbatim (the CLI resumed and cached it); err2 is dropped.
    assert forked == raw[:9]


def test_poison_followed_only_by_dangling_user_is_not_recovered(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "-workspace" / "sid-root.jsonl"
    _write_raw(path, [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1"),
        _entry("user", "u2", "a1", role="user", content="ask"),
        _timeout("err1", "u2"),
        _entry("user", "u3", "err1", role="user", content="Continue"),
    ])

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is True
    assert health.safe_recovery_uuid == "u2"
    assert health.is_uuid_safe("u3") is False


def test_poison_followed_by_in_flight_answer_is_not_recovered(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "-workspace" / "sid-root.jsonl"
    _write_raw(path, [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1"),
        _entry("user", "u2", "a1", role="user", content="ask"),
        _timeout("err1", "u2"),
        _entry("user", "u3", "err1", role="user", content="Continue"),
        # Streaming text row: no stop_reason, no later group, no receipt (R2).
        _entry("assistant", "a2", "u3", role="assistant", model="claude-opus-4-7",
               content=[{"type": "text", "text": "partial"}]),
    ])

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is True
    assert health.safe_recovery_uuid == "u2"
    assert health.is_uuid_safe("a2") is False


def test_poison_recovered_by_completed_tool_round(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "-workspace" / "sid-root.jsonl"
    _write_raw(path, [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1"),
        _entry("user", "u2", "a1", role="user", content="ask"),
        _timeout("err1", "u2"),
        _entry("user", "u3", "err1", role="user", content="Continue"),
        _entry("assistant", "a2", "u3", role="assistant", model="claude-opus-4-7",
               content=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
               stop_reason="tool_use"),
        _entry("user", "r1", "a2", role="user",
               content=[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        _assistant_text("a3", "r1", "done"),
    ])

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is False
    assert health.is_uuid_safe("a3") is True
    assert health.is_uuid_safe("err1") is False
