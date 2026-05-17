"""Helpers for forking Claude session JSONL files at a specific message UUID."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from obs_agent.context_jsonl import find_session_jsonl


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if isinstance(obj, dict):
                entries.append(obj)
    return entries


def _adjacent_metadata(entries: list[dict[str, Any]], first_chain_index: int) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    index = first_chain_index - 1
    while index >= 0:
        entry = entries[index]
        if entry.get("uuid"):
            break
        metadata.append(entry)
        index -= 1
    metadata.reverse()
    return metadata


def _sanitize_thinking_blocks(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``entry`` with thinking/redacted_thinking blocks removed entirely.

    Anthropic's API attaches a cryptographic ``signature`` to each ``thinking`` content
    block, bound to the original session's request context. The API enforces two rules:
    (1) signatures must be valid, and (2) thinking blocks cannot be modified. When a fork
    session resends inherited history, the signature context doesn't match, causing a 400.
    Simply stripping the signature field also fails — the API rejects thinking blocks
    without signatures as modified blocks. The only safe approach is to remove thinking
    and redacted_thinking blocks entirely from the forked conversation history.

    Non-thinking content blocks (text, tool_use, tool_result, etc.) and non-assistant
    entries are returned unchanged. The input ``entry`` is not mutated.
    """

    message = entry.get("message")
    if not isinstance(message, dict):
        return entry
    content = message.get("content")
    if not isinstance(content, list):
        return entry

    needs_change = any(
        isinstance(blk, dict) and blk.get("type") in ("thinking", "redacted_thinking")
        for blk in content
    )
    if not needs_change:
        return entry

    new_content: list[Any] = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") in ("thinking", "redacted_thinking"):
            continue
        new_content.append(blk)
    new_message = {**message, "content": new_content}
    return {**entry, "message": new_message}


def fork_session_jsonl(
    *,
    session_id: str,
    target_uuid: str,
    cwd: Path,
    projects_root: Path | None = None,
    new_session_id: str | None = None,
) -> str:
    """Copy the active parent chain ending at ``target_uuid`` into a new session file."""

    source_path = find_session_jsonl(
        session_id=session_id,
        cwd=cwd,
        projects_root=projects_root,
    )
    if source_path is None:
        raise FileNotFoundError(f"Session JSONL not found for {session_id}")

    entries = _read_jsonl(source_path)
    if not entries:
        raise ValueError(f"Session JSONL is empty for {session_id}")

    by_uuid = {
        entry["uuid"]: entry
        for entry in entries
        if isinstance(entry.get("uuid"), str) and entry["uuid"]
    }
    if target_uuid not in by_uuid:
        raise KeyError(f"UUID not found in session JSONL: {target_uuid}")

    seen: set[str] = set()
    chain: list[dict[str, Any]] = []
    cursor = target_uuid
    while cursor:
        if cursor in seen:
            raise ValueError(f"Cycle detected while traversing parentUuid chain at {cursor}")
        seen.add(cursor)

        entry = by_uuid.get(cursor)
        if entry is None:
            raise KeyError(f"Missing ancestor UUID in parentUuid chain: {cursor}")
        chain.append(entry)

        parent_uuid = entry.get("parentUuid")
        if not isinstance(parent_uuid, str) or not parent_uuid:
            break
        cursor = parent_uuid

    chain.reverse()
    first_chain_uuid = chain[0].get("uuid")
    first_chain_index = next(
        index for index, entry in enumerate(entries)
        if entry.get("uuid") == first_chain_uuid
    )
    output_entries = _adjacent_metadata(entries, first_chain_index) + chain

    fork_session_id = new_session_id or str(uuid.uuid4())
    dest_path = source_path.parent / f"{fork_session_id}.jsonl"
    with dest_path.open("w", encoding="utf-8") as handle:
        for entry in output_entries:
            sanitized = _sanitize_thinking_blocks(entry)
            handle.write(json.dumps(sanitized) + "\n")

    return fork_session_id
