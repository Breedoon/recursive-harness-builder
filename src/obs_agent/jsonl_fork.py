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
            handle.write(json.dumps(entry) + "\n")

    return fork_session_id
