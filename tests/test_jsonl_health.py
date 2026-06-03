from __future__ import annotations

import json
from pathlib import Path

from obs_agent.jsonl_fork import fork_session_jsonl
from obs_agent.jsonl_health import analyze_jsonl_path, resolve_safe_jsonl_target


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
    )


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

    assert health.needs_recovery is True
    assert health.first_poison_uuid == "err1"
    assert health.first_unsafe_tail_uuid == "a-tool"
    assert health.unsafe_tail_reason == "incomplete_tool_use_tail"
    assert health.last_poison_uuid == "err2"
    assert health.last_real_assistant_uuid == "a-tool"
    assert health.safe_recovery_uuid == "a2"
    assert health.is_uuid_safe("a1") is True
    assert health.is_uuid_safe("a-tool") is False
    assert health.is_uuid_safe("err2") is False
    assert health.is_uuid_safe("u3") is False


def test_analyze_jsonl_path_recovers_dangling_user_tail(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "-workspace" / "sid-root.jsonl"
    raw_lines = [
        _entry("user", "u1", None, role="user", content="start"),
        _assistant_text("a1", "u1", "healthy answer"),
        _entry("user", "u2", "a1", role="user", content="this never got an answer"),
    ]
    _write_raw(path, raw_lines)

    health = analyze_jsonl_path(path=path, session_id="sid-root")

    assert health.needs_recovery is True
    assert health.first_poison_uuid is None
    assert health.first_unsafe_tail_uuid == "u2"
    assert health.unsafe_tail_reason == "dangling_user_tail"
    assert health.safe_recovery_uuid == "a1"
    assert health.is_uuid_safe("a1") is True
    assert health.is_uuid_safe("u2") is False


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

    assert target is not None
    assert target.health.needs_recovery is True
    assert target.health.first_unsafe_tail_uuid == "u2"
    assert target.health.unsafe_tail_reason == "dangling_user_tail"
    assert target.target_uuid == "a1"
    assert target.changed is True
    assert target.reason == "preferred_uuid_unsafe"


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

    assert health.needs_recovery is True
    assert health.first_unsafe_tail_uuid == "u2"
    assert health.unsafe_tail_reason == "dangling_user_tail"
    assert health.safe_recovery_uuid == "a1"
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

    assert target is not None
    assert target.target_uuid == "a1"
    assert target.changed is True
    assert target.reason == "preferred_uuid_unsafe"


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

    forked_lines = (project_dir / "sid-recovered.jsonl").read_text(encoding="utf-8").splitlines()
    assert forked_lines == raw_lines[:3]
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
    assert target is not None
    assert target.target_uuid == "a1"

    fork_session_jsonl(
        session_id="sid-root",
        target_uuid=target.target_uuid or "",
        cwd=Path("/workspace"),
        projects_root=projects_root,
        new_session_id="sid-recovered",
    )

    forked_lines = (project_dir / "sid-recovered.jsonl").read_text(encoding="utf-8").splitlines()
    assert forked_lines == raw_lines[:3]
    assert all("a-empty" not in line and "u2" not in line for line in forked_lines)
