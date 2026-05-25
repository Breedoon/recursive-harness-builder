"""Helpers for forking Claude session JSONL files at a specific message UUID."""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from obs_agent.context_jsonl import find_session_jsonl


class SessionSourceError(ValueError):
    """Raised when an AgentTask session_source cannot be resolved."""


@dataclass(frozen=True)
class SessionSourceDescriptor:
    kind: Literal["session_id", "jsonl_path"]
    source_session_id: str
    located_jsonl_path: Path
    source_jsonl_path: Path | None
    input_value: str
    resolved_from: Literal["session_id_lookup", "explicit_path"]


def _read_jsonl(path: Path) -> list[tuple[dict[str, Any], str]]:
    entries: list[tuple[dict[str, Any], str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            obj = json.loads(raw)
            if isinstance(obj, dict):
                entries.append((obj, raw))
    return entries


def _looks_like_jsonl_path(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.endswith(".jsonl"):
        return True
    if candidate.startswith(("/", "~/", "./", "../")):
        return True
    return "/" in candidate


def _candidate_path(value: str, *, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def _session_id_from_entries(path: Path) -> str | None:
    try:
        entries = _read_jsonl(path)
    except OSError as exc:
        raise SessionSourceError(
            f"Cannot launch AgentTask: session_source JSONL is not readable: {path}"
        ) from exc
    for entry, _raw in entries:
        session_id = entry.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def resolve_session_source(
    session_source: str,
    *,
    cwd: Path,
    projects_root: Path | None = None,
) -> SessionSourceDescriptor:
    """Resolve an AgentTask session_source string to a concrete JSONL source."""

    value = str(session_source or "").strip()
    if not value:
        raise SessionSourceError("Cannot launch AgentTask: session_source is empty")

    candidate_path = _candidate_path(value, cwd=cwd)
    if candidate_path.exists() or _looks_like_jsonl_path(value):
        path = candidate_path
        if not path.exists():
            raise SessionSourceError(
                f"Cannot launch AgentTask: session_source JSONL not found: {path}"
            )
        if not path.is_file():
            raise SessionSourceError(
                f"Cannot launch AgentTask: session_source is not a JSONL file: {path}"
            )
        if path.suffix != ".jsonl":
            raise SessionSourceError(
                f"Cannot launch AgentTask: session_source path must end with .jsonl: {path}"
            )
        source_session_id = path.stem or _session_id_from_entries(path)
        if not source_session_id:
            raise SessionSourceError(
                f"Cannot launch AgentTask: session_source JSONL has no session id: {path}"
            )
        return SessionSourceDescriptor(
            kind="jsonl_path",
            source_session_id=source_session_id,
            located_jsonl_path=path,
            source_jsonl_path=path,
            input_value=value,
            resolved_from="explicit_path",
        )

    source_path = find_session_jsonl(
        session_id=value,
        cwd=cwd,
        projects_root=projects_root,
    )
    if source_path is None:
        raise SessionSourceError(
            f"Cannot launch AgentTask: session_source session JSONL not found for session id: {value}"
        )
    return SessionSourceDescriptor(
        kind="session_id",
        source_session_id=value,
        located_jsonl_path=source_path,
        source_jsonl_path=None,
        input_value=value,
        resolved_from="session_id_lookup",
    )


def _adjacent_metadata(
    entries: list[tuple[dict[str, Any], str]], first_chain_index: int
) -> list[tuple[dict[str, Any], str]]:
    metadata: list[tuple[dict[str, Any], str]] = []
    index = first_chain_index - 1
    while index >= 0:
        entry, _raw = entries[index]
        if entry.get("uuid"):
            break
        metadata.append(entries[index])
        index -= 1
    metadata.reverse()
    return metadata


def fork_session_jsonl(
    *,
    session_id: str,
    target_uuid: str,
    cwd: Path,
    projects_root: Path | None = None,
    source_path: Path | None = None,
    new_session_id: str | None = None,
) -> str:
    """Copy the active parent chain ending at ``target_uuid`` into a new session file.

    CRITICAL: entries must be written verbatim — no transformation, no stripping fields.
    The forked JSONL must be byte-identical to the parent's subset so that the Anthropic
    API prompt cache prefix matches exactly. Any modification (including stripping thinking
    block signatures) breaks the cache hit AND causes API 400 errors. Cross-model forking
    is prevented upstream at the schema level (tools.py rejects fork=true with model!=inherit).
    """

    if source_path is None:
        source_path = find_session_jsonl(
            session_id=session_id,
            cwd=cwd,
            projects_root=projects_root,
        )
    else:
        source_path = source_path.expanduser().resolve(strict=False)
    if source_path is None:
        raise FileNotFoundError(f"Session JSONL not found for {session_id}")

    entries = _read_jsonl(source_path)
    if not entries:
        raise ValueError(f"Session JSONL is empty for {session_id}")

    by_uuid = {
        entry["uuid"]: (entry, raw)
        for entry, raw in entries
        if isinstance(entry.get("uuid"), str) and entry["uuid"]
    }
    if target_uuid not in by_uuid:
        raise KeyError(f"UUID not found in session JSONL: {target_uuid}")

    seen: set[str] = set()
    chain: list[tuple[dict[str, Any], str]] = []
    cursor = target_uuid
    while cursor:
        if cursor in seen:
            raise ValueError(f"Cycle detected while traversing parentUuid chain at {cursor}")
        seen.add(cursor)

        item = by_uuid.get(cursor)
        if item is None:
            raise KeyError(f"Missing ancestor UUID in parentUuid chain: {cursor}")
        entry, _raw = item
        chain.append(item)

        parent_uuid = entry.get("parentUuid")
        if not isinstance(parent_uuid, str) or not parent_uuid:
            break
        cursor = parent_uuid

    chain.reverse()
    first_chain_uuid = chain[0][0].get("uuid")
    first_chain_index = next(
        index for index, (entry, _raw) in enumerate(entries)
        if entry.get("uuid") == first_chain_uuid
    )
    output_entries = _adjacent_metadata(entries, first_chain_index) + chain

    fork_session_id = new_session_id or str(uuid.uuid4())
    dest_path = source_path.parent / f"{fork_session_id}.jsonl"
    with dest_path.open("w", encoding="utf-8") as handle:
        for _entry, raw in output_entries:
            handle.write(raw + "\n")

    return fork_session_id
