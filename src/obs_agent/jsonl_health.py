"""Health checks for Claude Code session JSONL files.

The runtime treats JSONL as append-only.  When Claude Code appends synthetic
API-error assistant rows, resuming the same session id replays those rows too.
This module identifies that poisoned tail and picks a safe, verbatim-copyable
UUID for recovery/forking without rewriting the original file.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from obs_agent.context_jsonl import find_session_jsonl

LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class JsonlSessionHealth:
    """Summary of whether a session JSONL can be safely resumed as-is."""

    path: Path
    session_id: str
    total_entries: int
    last_uuid: str | None
    last_uuid_line: int | None
    last_real_assistant_uuid: str | None
    last_real_assistant_line: int | None
    first_poison_uuid: str | None
    first_poison_line: int | None
    last_poison_uuid: str | None
    last_poison_line: int | None
    safe_recovery_uuid: str | None
    safe_recovery_line: int | None
    uuid_line_numbers: Mapping[str, int]
    unsafe_uuids: frozenset[str]
    first_unsafe_tail_uuid: str | None = None
    first_unsafe_tail_line: int | None = None
    unsafe_tail_reason: str | None = None
    complete_message_targets: Mapping[str, str] = field(default_factory=dict)

    @property
    def needs_recovery(self) -> bool:
        return self.first_unsafe_tail_uuid is not None and self.safe_recovery_uuid is not None

    def has_uuid(self, uuid: str | None) -> bool:
        return bool(uuid) and str(uuid) in self.uuid_line_numbers

    def is_uuid_safe(self, uuid: str | None) -> bool:
        if not uuid:
            return False
        value = str(uuid)
        return value in self.uuid_line_numbers and value not in self.unsafe_uuids


@dataclass(frozen=True)
class SafeJsonlTarget:
    """Resolved target UUID for a resume/fork operation."""

    health: JsonlSessionHealth
    target_uuid: str | None
    changed: bool
    reason: str | None = None


@dataclass(frozen=True)
class _ParsedEntry:
    line_no: int
    uuid: str | None
    parent_uuid: str | None
    type: str | None
    role: str | None
    model: str | None
    text: str
    is_error: bool
    is_synthetic: bool
    has_text: bool
    has_tool_use: bool
    obj: dict[str, Any]


def _entry_text(message: dict[str, Any]) -> tuple[str, bool, bool]:
    content = message.get("content")
    texts: list[str] = []
    has_text = False
    has_tool_use = False
    if isinstance(content, str):
        return content, bool(content), False
    if not isinstance(content, list):
        return "", False, False
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "tool_use":
            has_tool_use = True
        if item_type == "text" and isinstance(item.get("text"), str):
            has_text = True
            texts.append(item["text"])
    return "\n".join(texts), has_text, has_tool_use


def _parse_entry(line_no: int, raw: str) -> _ParsedEntry | None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    message = obj.get("message")
    msg = message if isinstance(message, dict) else {}
    text, has_text, has_tool_use = _entry_text(msg)
    model = msg.get("model") if isinstance(msg.get("model"), str) else None
    error = msg.get("error")
    is_error = (
        bool(obj.get("isApiErrorMessage"))
        or (isinstance(error, str) and bool(error.strip()))
        or text.strip() == "Prompt is too long"
    )
    uuid = obj.get("uuid")
    parent_uuid = obj.get("parentUuid")
    entry_type = obj.get("type")
    role = msg.get("role")
    return _ParsedEntry(
        line_no=line_no,
        uuid=uuid if isinstance(uuid, str) and uuid else None,
        parent_uuid=parent_uuid if isinstance(parent_uuid, str) and parent_uuid else None,
        type=entry_type if isinstance(entry_type, str) else None,
        role=role if isinstance(role, str) else None,
        model=model,
        text=text,
        is_error=is_error,
        is_synthetic=model == "<synthetic>",
        has_text=has_text,
        has_tool_use=has_tool_use,
        obj=obj,
    )


def _is_real_assistant(entry: _ParsedEntry) -> bool:
    return (
        entry.type == "assistant"
        and entry.role == "assistant"
        and not entry.is_error
        and not entry.is_synthetic
        and bool(entry.uuid)
    )


def _completed_boundaries(
    entries: list[_ParsedEntry], targets: dict[str, str] | None = None,
) -> list[_ParsedEntry]:
    """Find complete API messages/tool rounds, not individual streamed rows."""
    groups: dict[str, list[_ParsedEntry]] = {}
    results: dict[str, _ParsedEntry] = {}
    for entry in entries:
        content = entry.obj.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if isinstance(tool_id, str):
                        results[tool_id] = entry
        if _is_real_assistant(entry):
            message_id = entry.obj.get("message", {}).get("id") or entry.uuid
            groups.setdefault(message_id, []).append(entry)
    # An already persisted plain user message belongs to the prior API prefix
    # even if its answer was interrupted. Do not replace that request on recovery.
    boundaries = [entry for entry in entries if entry.type == "user" and entry.uuid
                  and not any(isinstance(block, dict) and block.get("type") == "tool_result"
                              for block in entry.obj.get("message", {}).get("content", []))]
    if targets is not None:
        for entry in boundaries:
            targets[entry.uuid] = entry.uuid
    from obs_agent.jsonl_replay import has_terminal_completion

    latest_group_start = max((group[0].line_no for group in groups.values()), default=0)
    for group in groups.values():
        tool_ids = {
            block["id"]
            for entry in group
            for block in entry.obj.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
            and isinstance(block.get("id"), str)
        }
        boundary = None
        members = list(group)
        if tool_ids and tool_ids <= results.keys():
            members += [results[t] for t in tool_ids]
            boundary = max(members, key=lambda e: e.line_no)
        elif not tool_ids and any(entry.obj.get("message", {}).get("content") for entry in group) and (
            group[-1].line_no < latest_group_start
            or any(entry.obj.get("message", {}).get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens") for entry in group)
            or has_terminal_completion([entry.obj for entry in group])
        ):
            boundary = group[-1]
        if boundary is not None:
            boundaries.append(boundary)
            if targets is not None:
                for entry in members:
                    if entry.uuid and boundary.uuid:
                        targets[entry.uuid] = boundary.uuid
    return sorted(boundaries, key=lambda entry: entry.line_no)


def _is_poison(entry: _ParsedEntry) -> bool:
    if not entry.uuid:
        return False
    if entry.text.strip() == "Prompt is too long":
        return True
    return entry.is_synthetic and entry.is_error


def _choose_safe_recovery(entries: list[_ParsedEntry], before_index: int) -> _ParsedEntry | None:
    candidates = entries[:before_index]
    boundaries = _completed_boundaries(candidates)
    if boundaries:
        return boundaries[-1]
    return next((entry for entry in reversed(candidates)
                 if entry.type == "user" and entry.uuid and not entry.is_error), None)


def _tail_reason(entry: _ParsedEntry) -> str:
    if _is_poison(entry):
        return "poisoned_tail"
    if entry.type == "user" or entry.role == "user":
        return "dangling_user_tail"
    if _is_real_assistant(entry) and entry.has_tool_use:
        return "incomplete_tool_use_tail"
    if _is_real_assistant(entry) and not entry.has_text:
        return "empty_assistant_tail"
    return "incomplete_tail"


def _collect_chain_unsafe(
    *,
    by_uuid: dict[str, _ParsedEntry],
    poison_uuids: set[str],
) -> frozenset[str]:
    unsafe: set[str] = set(poison_uuids)
    for uuid_value in by_uuid:
        seen: set[str] = set()
        cursor: str | None = uuid_value
        while cursor:
            if cursor in unsafe:
                unsafe.add(uuid_value)
                break
            if cursor in seen:
                unsafe.add(uuid_value)
                break
            seen.add(cursor)
            entry = by_uuid.get(cursor)
            if entry is None:
                break
            cursor = entry.parent_uuid
    return frozenset(unsafe)


def analyze_session_jsonl(
    *,
    session_id: str,
    cwd: Path,
    projects_root: Path | None = None,
) -> JsonlSessionHealth | None:
    """Analyze a session JSONL and return recovery metadata if available."""
    path = find_session_jsonl(session_id=session_id, cwd=cwd, projects_root=projects_root)
    if path is None:
        return None
    return analyze_jsonl_path(path=path, session_id=session_id)


def analyze_jsonl_path(*, path: Path, session_id: str | None = None) -> JsonlSessionHealth:
    entries: list[_ParsedEntry] = []
    by_uuid: dict[str, _ParsedEntry] = {}
    uuid_lines: dict[str, int] = {}

    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            entry = _parse_entry(line_no, raw)
            if entry is None:
                continue
            entries.append(entry)
            if entry.uuid:
                by_uuid[entry.uuid] = entry
                uuid_lines[entry.uuid] = entry.line_no

    total_entries = len(entries)
    # CLI replay uses the latest copy of a UUID. Keep its original event position
    # for completion/order and original-line provenance, not append-overlay order.
    first_lines: dict[str, int] = {}
    effective: dict[str, _ParsedEntry] = {}
    for entry in entries:
        if entry.uuid:
            first_lines.setdefault(entry.uuid, entry.line_no)
            effective[entry.uuid] = replace(entry, line_no=first_lines[entry.uuid])
    from obs_agent.jsonl_replay import select_replay_entries

    selected = select_replay_entries([(entry.obj, "") for entry in effective.values()], require_complete=False)
    active_uuids = {obj["uuid"] for obj, _raw in selected}
    entries = [entry for uid, entry in effective.items() if uid in active_uuids]
    by_uuid = dict(effective)
    uuid_lines = dict(first_lines)
    last_uuid_entry = next((entry for entry in reversed(entries) if entry.uuid), None)
    real_assistant_entries = [entry for entry in entries if _is_real_assistant(entry)]
    last_real_assistant = real_assistant_entries[-1] if real_assistant_entries else None
    all_poison_uuids = {entry.uuid for entry in entries if _is_poison(entry) and entry.uuid}
    poison_descendants = _collect_chain_unsafe(by_uuid=by_uuid, poison_uuids=all_poison_uuids)
    complete_message_targets: dict[str, str] = {}
    complete_assistant_entries = _completed_boundaries(
        [entry for entry in entries if entry.uuid not in poison_descendants], complete_message_targets,
    )
    last_complete_assistant = (
        complete_assistant_entries[-1] if complete_assistant_entries else None
    )

    first_poison: _ParsedEntry | None = None
    last_poison: _ParsedEntry | None = None
    safe_recovery: _ParsedEntry | None = None
    poison_uuids: set[str] = set()
    first_unsafe_tail: _ParsedEntry | None = None
    unsafe_tail_reason: str | None = None

    pending_tool_groups = {entry.obj.get("message", {}).get("id") for entry in entries
                           if entry.has_tool_use and entry.uuid not in complete_message_targets}
    if last_complete_assistant is not None:
        last_complete_index = entries.index(last_complete_assistant)
        for entry in entries[last_complete_index + 1 :]:
            if not entry.uuid or entry.type not in ("assistant", "user"):
                continue
            # Unknown legacy terminal text is preserved, not falsely certified
            # complete or truncated. A declared pending tool group is different.
            if (_is_real_assistant(entry) and entry.has_text and not entry.has_tool_use
                    and entry.obj.get("message", {}).get("id") not in pending_tool_groups):
                continue
            first_unsafe_tail = entry
            unsafe_tail_reason = _tail_reason(entry)
            safe_recovery = last_complete_assistant
            break

    if last_complete_assistant is not None:
        last_complete_index = entries.index(last_complete_assistant)
        poison_search = entries[last_complete_index + 1 :]
    elif last_real_assistant is not None:
        poison_search = entries[entries.index(last_real_assistant) + 1 :]
    else:
        poison_search = entries
    for idx, entry in enumerate(poison_search):
        if not _is_poison(entry):
            continue
        first_poison = first_poison or entry
        last_poison = entry
        if entry.uuid:
            poison_uuids.add(entry.uuid)
        if first_unsafe_tail is None:
            first_unsafe_tail = entry
            unsafe_tail_reason = "poisoned_tail"
        if safe_recovery is None:
            safe_recovery = _choose_safe_recovery(
                entries,
                entries.index(entry) if entry in entries else idx,
            )
    if first_unsafe_tail is not None:
        # Once a run ends without a complete assistant answer, every later UUID
        # is part of a tail that should not be inherited by resumes or forks.
        for entry in entries:
            if entry.uuid and entry.line_no >= first_unsafe_tail.line_no:
                poison_uuids.add(entry.uuid)

    unsafe = _collect_chain_unsafe(by_uuid=by_uuid, poison_uuids=poison_uuids) | frozenset(
        all_poison_uuids
    )
    return JsonlSessionHealth(
        path=path,
        session_id=session_id or path.stem,
        total_entries=total_entries,
        last_uuid=last_uuid_entry.uuid if last_uuid_entry is not None else None,
        last_uuid_line=last_uuid_entry.line_no if last_uuid_entry is not None else None,
        last_real_assistant_uuid=(
            last_real_assistant.uuid if last_real_assistant is not None else None
        ),
        last_real_assistant_line=(
            last_real_assistant.line_no if last_real_assistant is not None else None
        ),
        first_poison_uuid=first_poison.uuid if first_poison is not None else None,
        first_poison_line=first_poison.line_no if first_poison is not None else None,
        last_poison_uuid=last_poison.uuid if last_poison is not None else None,
        last_poison_line=last_poison.line_no if last_poison is not None else None,
        safe_recovery_uuid=safe_recovery.uuid if safe_recovery is not None else None,
        safe_recovery_line=safe_recovery.line_no if safe_recovery is not None else None,
        uuid_line_numbers=MappingProxyType(dict(uuid_lines)),
        unsafe_uuids=unsafe,
        first_unsafe_tail_uuid=(
            first_unsafe_tail.uuid if first_unsafe_tail is not None else None
        ),
        first_unsafe_tail_line=(
            first_unsafe_tail.line_no if first_unsafe_tail is not None else None
        ),
        unsafe_tail_reason=unsafe_tail_reason,
        complete_message_targets=MappingProxyType(complete_message_targets),
    )


# PREFIX-STABILITY WARNING (M5): safe recovery means a complete API message,
# not merely its text row. Grouped tools/results must remain replay-equivalent,
# with side effects reconciled before any retry. Original histories stay intact.
# Live guard: prefix_repro.py --provider codex recovery recovery_multi.
def resolve_safe_jsonl_target(
    *,
    session_id: str,
    cwd: Path,
    preferred_uuid: str | None = None,
    projects_root: Path | None = None,
    source_path: Path | None = None,
) -> SafeJsonlTarget | None:
    """Resolve a preferred UUID to a safe JSONL target.

    If the preferred UUID is already safe, it is returned unchanged. If it is
    missing or belongs to a poisoned tail, the session's recovery boundary is
    returned instead.
    """
    if source_path is not None:
        health = analyze_jsonl_path(path=source_path, session_id=session_id)
    else:
        health = analyze_session_jsonl(
            session_id=session_id,
            cwd=cwd,
            projects_root=projects_root,
        )
    if health is None:
        return None

    if preferred_uuid and health.is_uuid_safe(preferred_uuid):
        completed_target = health.complete_message_targets.get(preferred_uuid)
        if completed_target is None:
            limit = health.uuid_line_numbers[preferred_uuid]
            candidates = {value for value in health.complete_message_targets.values()
                          if health.uuid_line_numbers[value] <= limit and health.is_uuid_safe(value)}
            completed_target = max(candidates, key=health.uuid_line_numbers.get, default=None)
        return SafeJsonlTarget(
            health=health,
            target_uuid=completed_target,
            changed=completed_target != preferred_uuid,
            reason="complete_message_boundary" if completed_target != preferred_uuid else None,
        )

    if health.needs_recovery:
        reason = health.unsafe_tail_reason or "unsafe_tail"
        if preferred_uuid and not health.has_uuid(preferred_uuid):
            reason = f"preferred_uuid_missing_{reason}"
        elif preferred_uuid:
            reason = "preferred_uuid_unsafe"
        return SafeJsonlTarget(
            health=health,
            target_uuid=health.safe_recovery_uuid,
            changed=preferred_uuid != health.safe_recovery_uuid,
            reason=reason,
        )

    candidates = {value for value in health.complete_message_targets.values() if health.is_uuid_safe(value)}
    fallback = max(candidates, key=health.uuid_line_numbers.get, default=None)
    reason = None
    if preferred_uuid and preferred_uuid in health.unsafe_uuids:
        reason = "preferred_uuid_unsafe"
    elif preferred_uuid and not health.has_uuid(preferred_uuid):
        reason = "preferred_uuid_missing"
    return SafeJsonlTarget(
        health=health,
        target_uuid=fallback,
        changed=preferred_uuid != fallback,
        reason=reason,
    )
