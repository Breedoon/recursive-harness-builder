"""Tests for sanitization of Anthropic thinking-block signatures during JSONL fork.

Context: Anthropic's API attaches a cryptographic ``signature`` to each ``thinking``
content block. The signature is bound to the original session's request context. When
a fork session copies the parent JSONL verbatim and re-sends the inherited message
history on its first API call, the API rejects with HTTP 400 ``Invalid signature in
thinking block``. The fix in ``fork_session_jsonl`` strips the ``signature`` field from
thinking content blocks before writing the forked JSONL, so the first child API call
no longer presents an invalid signature.

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


def test_fork_strips_signature_from_thinking_blocks(tmp_path: Path) -> None:
    """Forked JSONL must not contain ``signature`` on any thinking content block.

    Anthropic's signature binds to the original session; carrying it into a forked
    session causes a 400 on the first child API call.
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
        cwd=Path("/Users/breedoon/Documents/obs"),
        projects_root=projects_root,
        new_session_id="sid-thinking-fork",
    )

    forked = _read_lines(project_dir / "sid-thinking-fork.jsonl")
    # Locate the assistant turn in the fork
    assistant = next(e for e in forked if e.get("uuid") == "a1")
    content = assistant["message"]["content"]

    thinking_blocks = [blk for blk in content if isinstance(blk, dict) and blk.get("type") == "thinking"]
    text_blocks = [blk for blk in content if isinstance(blk, dict) and blk.get("type") == "text"]

    # Thinking block must still exist (preserve assistant reasoning), but the
    # provider-signed ``signature`` must be stripped.
    assert len(thinking_blocks) == 1, "thinking block should be preserved (not whole-block stripped)"
    assert "signature" not in thinking_blocks[0], (
        "signature must be stripped from thinking block to avoid Anthropic 400 on fork resume"
    )
    # The reasoning content itself must be preserved.
    assert thinking_blocks[0].get("thinking") == "deliberating about the answer"
    assert thinking_blocks[0].get("type") == "thinking"

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
        cwd=Path("/Users/breedoon/Documents/obs"),
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
                    }
                ],
            },
        },
    ]
    _write_lines(source_path, entries)

    fork_session_jsonl(
        session_id="sid-meta",
        target_uuid="a1",
        cwd=Path("/Users/breedoon/Documents/obs"),
        projects_root=projects_root,
        new_session_id="sid-meta-fork",
    )

    forked = _read_lines(project_dir / "sid-meta-fork.jsonl")
    # queue-operation preserved at head
    assert forked[0] == {"type": "queue-operation", "operation": "dequeue"}
    # user turn preserved unchanged
    user_turn = next(e for e in forked if e.get("uuid") == "u1")
    assert user_turn["message"] == {"role": "user", "content": "go"}
    # assistant thinking stripped
    assistant = next(e for e in forked if e.get("uuid") == "a1")
    assert "signature" not in assistant["message"]["content"][0]
