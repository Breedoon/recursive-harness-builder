"""Helpers for forking Claude session JSONL files at a specific message UUID."""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from obs_agent.context_jsonl import find_session_jsonl

LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                session_id = entry.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    return session_id
    except OSError as exc:
        raise SessionSourceError(
            f"Cannot launch AgentTask: session_source JSONL is not readable: {path}"
        ) from exc
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
        try:
            with path.open("r", encoding="utf-8"):
                pass
        except OSError as exc:
            raise SessionSourceError(
                f"Cannot launch AgentTask: session_source JSONL is not readable: {path}"
            ) from exc
        source_session_id = _session_id_from_entries(path) or path.stem
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


def _read_jsonl(source: bytes) -> list[tuple[dict[str, Any], str]]:
    entries: list[tuple[dict[str, Any], str]] = []
    for raw in source.decode("utf-8").split("\n"):
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if isinstance(obj, dict):
            entries.append((obj, raw))
    return entries


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


# PREFIX-STABILITY WARNING (M5/M6): parentUuid traversal alone can lose sibling
# tool results from a parallel batch. Recovery/fork must retain complete API
# messages and tool rounds verbatim; never select an in-flight text-only row.
# Live guard: prefix_repro.py --provider codex parallel_resume recovery_multi.
def fork_session_jsonl(
    *,
    session_id: str,
    target_uuid: str,
    cwd: Path,
    projects_root: Path | None = None,
    source_path: Path | None = None,
    new_session_id: str | None = None,
) -> str:
    """Copy complete active history verbatim, then append parent-only replay overlays.

    Source rows, payloads, IDs, signatures and timestamps stay unchanged. The
    CLI's chain-only reader needs provenance-tracked same-UUID ancestry overlays
    to retain parallel siblings; those copies change parentUuid only. Cross-model
    forking remains prohibited upstream by tools.py.
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

    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    resolved = resolve_safe_jsonl_target(session_id=session_id, cwd=cwd, source_path=source_path,
                                        preferred_uuid=target_uuid)
    if resolved is None or resolved.target_uuid is None:
        raise ValueError("Fork has no confirmed complete history boundary")
    requested_target_uuid = target_uuid
    target_uuid = resolved.target_uuid
    source_bytes = source_path.read_bytes()
    entries = _read_jsonl(source_bytes)
    if not entries:
        raise ValueError(f"Session JSONL is empty for {session_id}")

    from obs_agent.jsonl_replay import prepare_session_replay, select_replay_entries

    selected = select_replay_entries(entries, target_uuid)
    selected_uuids = {entry["uuid"] for entry, _raw in selected}
    first_chain_index = next(index for index, (entry, _raw) in enumerate(entries)
                             if entry.get("uuid") in selected_uuids)
    output_entries = _adjacent_metadata(entries, first_chain_index) + [
        item for item in entries if item[0].get("uuid") in selected_uuids
    ]

    fork_session_id = new_session_id or str(uuid.uuid4())
    dest_path = source_path.parent / f"{fork_session_id}.jsonl"
    with dest_path.open("w", encoding="utf-8") as handle:
        for _entry, raw in output_entries:
            handle.write(raw + "\n")

    replay = prepare_session_replay(session_id=fork_session_id, cwd=cwd, source_path=dest_path,
                                    target_uuid=target_uuid)
    with source_path.open("rb") as handle:
        source_prefix_unchanged = handle.read(len(source_bytes)) == source_bytes
    provenance = Path(os.environ.get("OBS_SESSION_REPLAY_PROVENANCE_DIR", "/workspace/runtime/state/session-replay")) / "forks"
    provenance.mkdir(parents=True, exist_ok=True)
    (provenance / f"{fork_session_id}.json").write_text(json.dumps({
        "fork_session_id": fork_session_id, "source_path": str(source_path),
        "requested_target_uuid": requested_target_uuid, "target_uuid": target_uuid,
        "source_prefix_bytes": len(source_bytes), "source_prefix_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_prefix_unchanged": source_prefix_unchanged, "replay": replay,
        "loaded_fork_source_sha256": LOADED_SOURCE_SHA256,
    }, indent=2) + "\n")
    return fork_session_id
