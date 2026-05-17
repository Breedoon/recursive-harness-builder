"""Tests for sanitization of Anthropic thinking-block signatures during JSONL fork.

Context: Anthropic's API attaches a cryptographic ``signature`` to each ``thinking``
content block. The signature is bound to the original session's request context. The API
enforces: (1) signatures must be valid, and (2) thinking blocks cannot be modified. When
a fork session copies the parent JSONL, the signature context doesn't match, and stripping
just the signature field also fails (modified block). The fix in ``fork_session_jsonl``
removes thinking/redacted_thinking blocks entirely from the forked JSONL.

Canonical reproducer reference: session ``cfbdfc0b-f271-483f-8517-9c8f0e98f59c`` (line
1200 synthesized API 400 after parent chain copy on Opus fork=true).
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_fork_removes_thinking_blocks_entirely(tmp_path: Path) -> None:
    """Forked JSONL must not contain any thinking/redacted_thinking blocks.

    The API requires thinking blocks to be sent back exactly as received (with valid
    signatures). Since forked sessions have a different request context, signatures are
    invalid. Stripping just the signature also fails (API rejects modified blocks).
    The only safe approach is removing thinking blocks entirely.
    """

    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-Users-breedoon-Documents-obs"
    source_path = project_dir / "sid-thinking.jsonl"

    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "sid-thinking",
            "message": {"role": "user", "content": "start"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "sid-thinking",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "deliberating about the answer",
                        "signature": "OPAQUE_SIGNATURE_BOUND_TO_PARENT_SESSION_AAAAAAAA",
                    },
                    {"type": "text", "text": "hello world"},
                ],
            },
        },
    ]
    _write_lines(source_path, entries)

    fork_session_jsonl(
        session_id="sid-thinking",
        target_uuid="a1",
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
        new_session_id="sid-thinking-fork",
    )

    forked = _read_lines(project_dir / "sid-thinking-fork.jsonl")
    assistant = next(e for e in forked if e.get("uuid") == "a1")
    content = assistant["message"]["content"]

    thinking_blocks = [blk for blk in content if isinstance(blk, dict) and blk.get("type") in ("thinking", "redacted_thinking")]
    text_blocks = [blk for blk in content if isinstance(blk, dict) and blk.get("type") == "text"]

    # Thinking blocks must be removed entirely — cannot be sent without valid signatures.
    assert len(thinking_blocks) == 0, "thinking blocks must be removed entirely from forked JSONL"

    # Non-thinking content must be unchanged.
    assert text_blocks == [{"type": "text", "text": "hello world"}]


def test_fork_leaves_non_thinking_blocks_untouched(tmp_path: Path) -> None:
    """Sanitization must not affect tool_use, text, or other non-thinking blocks.

    Regression guard: ensure the strip is narrowly scoped to ``type == "thinking"``.
    """

    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-Users-breedoon-Documents-obs"
    source_path = project_dir / "sid-mixed.jsonl"

    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "sid-mixed",
            "message": {"role": "user", "content": "do something"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "sid-mixed",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "plain text"},
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    },
                ],
            },
        },
    ]
    _write_lines(source_path, entries)

    fork_session_jsonl(
        session_id="sid-mixed",
        target_uuid="a1",
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
        new_session_id="sid-mixed-fork",
    )

    forked = _read_lines(project_dir / "sid-mixed-fork.jsonl")
    assistant = next(e for e in forked if e.get("uuid") == "a1")
    content = assistant["message"]["content"]

    # Both blocks should be unchanged.
    assert content[0] == {"type": "text", "text": "plain text"}
    assert content[1] == {
        "type": "tool_use",
        "id": "toolu_xyz",
        "name": "Read",
        "input": {"file_path": "/tmp/x"},
    }


def test_fork_preserves_non_assistant_turns_with_thinking_lookalike(tmp_path: Path) -> None:
    """The sanitizer keys off content-block type, not the outer entry type.

    Confirm that an entry without a ``message`` dict (e.g. queue-operation metadata)
    is preserved unchanged, and that user turns are unchanged.
    """

    projects_root = tmp_path / ".claude" / "projects"
    project_dir = projects_root / "-Users-breedoon-Documents-obs"
    source_path = project_dir / "sid-meta.jsonl"

    entries = [
        {"type": "queue-operation", "operation": "dequeue"},
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "sid-meta",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "sid-meta",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "x",
                        "signature": "SIG",
                    },
                    {"type": "text", "text": "response"},
                ],
            },
        },
    ]
    _write_lines(source_path, entries)

    fork_session_jsonl(
        session_id="sid-meta",
        target_uuid="a1",
        cwd=Path("/workspace/recursive-harness"),
        projects_root=projects_root,
        new_session_id="sid-meta-fork",
    )

    forked = _read_lines(project_dir / "sid-meta-fork.jsonl")
    # queue-operation preserved at head
    assert forked[0] == {"type": "queue-operation", "operation": "dequeue"}
    # user turn preserved unchanged
    user_turn = next(e for e in forked if e.get("uuid") == "u1")
    assert user_turn["message"] == {"role": "user", "content": "go"}
    # assistant thinking block removed entirely
    assistant = next(e for e in forked if e.get("uuid") == "a1")
    assert assistant["message"]["content"] == [{"type": "text", "text": "response"}]
