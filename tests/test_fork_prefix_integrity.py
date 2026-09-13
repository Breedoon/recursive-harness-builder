"""Byte-level fork regressions; analytics must never mutate fork input files."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from obs_agent.context_jsonl import _encode_project_path, load_jsonl_usage_snapshot
from obs_agent.jsonl_fork import fork_session_jsonl, resolve_session_source

# Deliberately noncanonical JSON: whitespace, key order, escaped unicode,
# nested signatures and unknown fields must survive without reserialization.
METADATA = b' { "type": "summary", "summary": "keep \\u0041 verbatim" }\n'
ROOT = b'{"sessionId":"parent","uuid":"root","parentUuid":null,"type":"user","message":{"content":"hello"}}\n'
THINKING = (
    b' {"type":"assistant", "uuid":"thinking", "parentUuid":"root", '
    b'"sessionId":"parent","message":{"content":[{"type":"thinking",'
    b'"thinking":"private reasoning", "signature":"sig/ABC+=\\u003d"},'
    b'{"type":"redacted_thinking","data":"opaque+/=="},'
    b'{"type":"text","text":"Za\xc5\xbc\xc3\xb3\xc5\x82\xc4\x87"}],'
    b'"usage":{"input_tokens":37,"cache_read_input_tokens":101}},'
    b'"future_unknown":{"keep":true}}  \n'
)
TOOL = b'{ "uuid":"tool", "parentUuid":"thinking", "sessionId":"parent", "type":"assistant", "message":{"content":[{"type":"tool_use","id":"call-1","name":"Read","input":{"path":"x"}}]}}\n'
RESULT = b'{"uuid":"result","parentUuid":"tool","sessionId":"parent","type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"call-1","content":"ok"}]}}\n'
OFF_BRANCH = b'{"uuid":"side","parentUuid":"root","sessionId":"parent","type":"assistant","message":{"content":"unselected"}}\n'


def source_file(tmp_path: Path, *, preferred: bool = True) -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    project = _encode_project_path(tmp_path) if preferred else "other-workspace"
    source = projects / project / "parent.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(METADATA + ROOT + THINKING + OFF_BRANCH + TOOL + RESULT)
    return projects, source


@pytest.mark.parametrize("preferred", [True, False])
@pytest.mark.parametrize("analytics_first", [True, False])
@pytest.mark.parametrize("target,expected", [
    ("root", METADATA + ROOT),
    ("thinking", METADATA + ROOT + THINKING),
    ("result", METADATA + ROOT + THINKING + TOOL + RESULT),
])
def test_fork_preserves_selected_record_bytes(
    tmp_path: Path, preferred: bool, analytics_first: bool, target: str, expected: bytes
) -> None:
    projects, source = source_file(tmp_path, preferred=preferred)
    original_bytes = source.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    if analytics_first:
        snapshot = load_jsonl_usage_snapshot(
            session_id="parent", cwd=tmp_path, projects_root=projects
        )
        assert snapshot is not None
        assert snapshot.latest_context_triplet_tokens == 138

    fork_session_jsonl(
        session_id="parent", target_uuid=target, cwd=tmp_path,
        projects_root=projects, new_session_id="child",
    )

    assert (source.parent / "child.jsonl").read_bytes() == expected
    assert source.read_bytes() == original_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_digest


def test_fork_of_fork_keeps_original_parent_prefix(tmp_path: Path) -> None:
    projects, source = source_file(tmp_path)
    fork_session_jsonl(
        session_id="parent", target_uuid="result", cwd=tmp_path,
        projects_root=projects, new_session_id="child",
    )
    child = source.parent / "child.jsonl"
    prefix = child.read_bytes()
    child_event = b'{"uuid":"child-event","parentUuid":"result","sessionId":"child","type":"assistant","message":{"content":"child"}}\n'
    with child.open("ab") as handle:
        handle.write(child_event)
    fork_session_jsonl(
        session_id="child", target_uuid="child-event", cwd=tmp_path,
        projects_root=projects, new_session_id="grandchild",
    )
    assert (source.parent / "grandchild.jsonl").read_bytes() == prefix + child_event
    assert child.read_bytes().startswith(prefix)


@pytest.mark.parametrize("relative", [True, False])
def test_explicit_source_paths_still_work(tmp_path: Path, relative: bool) -> None:
    source = tmp_path / "external path" / "export.jsonl"
    source.parent.mkdir()
    source.write_bytes(METADATA + ROOT + THINKING)
    supplied_path = str(source.relative_to(tmp_path) if relative else source)
    descriptor = resolve_session_source(supplied_path, cwd=tmp_path)
    assert descriptor.source_session_id == "parent"
    assert descriptor.source_jsonl_path == source
    fork_session_jsonl(
        session_id=descriptor.source_session_id, target_uuid="thinking",
        cwd=tmp_path, source_path=descriptor.source_jsonl_path, new_session_id="child",
    )
    assert (source.parent / "child.jsonl").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("broken", [
    b'{"uuid":"root","parentUuid":"missing"}\n',
    b'{"uuid":"root","parentUuid":"root"}\n',
    b'{"uuid":"root","parentUuid":null}\nnot-json\n',
])
def test_invalid_chain_never_creates_child_or_changes_source(tmp_path: Path, broken: bytes) -> None:
    source = tmp_path / "parent.jsonl"
    source.write_bytes(broken)
    with pytest.raises((KeyError, ValueError)):
        fork_session_jsonl(
            session_id="parent", target_uuid="root", cwd=tmp_path,
            source_path=source, new_session_id="child",
        )
    assert source.read_bytes() == broken
    assert not (tmp_path / "child.jsonl").exists()
