"""Complete active replay selection and append-only CLI ancestry repair.

Call preparation only after disconnect, under the session owner's lock. Original
rows remain untouched; CLI last-UUID-wins overlays change only parentUuid.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from obs_agent.context_jsonl import find_session_jsonl

LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _completion_path(message_id: str) -> Path:
    root = Path(os.environ.get("OBS_SESSION_REPLAY_PROVENANCE_DIR", "/workspace/runtime/state/session-replay"))
    return root / "completions" / (hashlib.sha256(message_id.encode()).hexdigest() + ".json")


def _group_digest(group: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps([(e["uuid"], e["message"]) for e in group],
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def has_terminal_completion(group: list[dict[str, Any]]) -> bool:
    message_id = group[0].get("message", {}).get("id")
    if not isinstance(message_id, str):
        return False
    path = _completion_path(message_id)
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return receipt.get("group_digest") == _group_digest(group)


def record_terminal_completion(
    *, session_id: str, cwd: Path, target_uuid: str | None = None,
    observer: str = "prefix_repro", observer_source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Record a caller-observed successful SDK Result after eager JSONL flush."""
    path = find_session_jsonl(session_id=session_id, cwd=cwd)
    if path is None:
        return None
    raw = path.read_bytes()
    entries = [(json.loads(line), line) for line in raw.decode().split("\n") if line.strip()]
    effective = {e["uuid"]: e for e, _line in entries if e.get("uuid")}
    if target_uuid is None:
        selected = select_replay_entries(entries, require_complete=False)
        target_uuid = next((e["uuid"] for e, _line in reversed(selected) if e.get("type") == "assistant"), None)
    target = effective.get(target_uuid)
    if target is None or target.get("type") != "assistant" or target.get("isApiErrorMessage"):
        return None
    message_id = target.get("message", {}).get("id")
    if not isinstance(message_id, str):
        return None
    group = [e for e in effective.values() if e.get("type") == "assistant" and e.get("message", {}).get("id") == message_id]
    if any(b.get("type") == "tool_use" for e in group for b in e["message"].get("content", []) if isinstance(b, dict)):
        return None
    receipt = {"session_id": session_id, "source_path": str(path), "target_uuid": target_uuid,
               "message_id": message_id, "group_digest": _group_digest(group),
               "prefix_bytes": len(raw), "prefix_sha256": hashlib.sha256(raw).hexdigest(),
               "observed": "successful_sdk_terminal_result_after_eager_flush",
               "observer": observer, "observer_source_sha256": observer_source_sha256,
               "replay_source_sha256": LOADED_SOURCE_SHA256}
    dest = _completion_path(message_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(f".{os.getpid()}.new")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(dest)
    return receipt


def select_replay_entries(
    entries: list[tuple[dict[str, Any], str]], target_uuid: str | None = None,
    *, require_complete: bool = True,
) -> list[tuple[dict[str, Any], str]]:
    # Dict replacement keeps the first occurrence's order and latest replay map.
    effective = {e["uuid"]: (e, raw) for e, raw in entries
                 if e.get("uuid")}
    if not effective:
        return []
    order = {uid: index for index, uid in enumerate(effective)}
    if target_uuid is None:
        leaves = [e for e, _raw in effective.values() if not e.get("isSidechain")
                  and e.get("type") in ("user", "assistant", "system", "progress", "attachment")]
        if not leaves:
            return []
        target_uuid = max(leaves, key=lambda e: (e.get("timestamp", ""), order[e["uuid"]]))["uuid"]
    if target_uuid not in effective:
        raise KeyError(f"Replay target missing: {target_uuid}")
    chosen: set[str] = set()
    cursor = target_uuid
    while cursor:
        if cursor in chosen:
            raise ValueError(f"Replay parent cycle: {cursor}")
        chosen.add(cursor)
        if cursor not in effective:
            raise KeyError(f"Replay ancestor missing: {cursor}")
        cursor = effective[cursor][0].get("parentUuid")
    cutoff = order[target_uuid]
    message_ids = {effective[u][0].get("message", {}).get("id") for u in chosen
                   if effective[u][0].get("type") == "assistant"}
    message_ids.discard(None)
    tool_ids: set[str] = set()
    for uid, (entry, _raw) in effective.items():
        if order[uid] > cutoff or entry.get("isSidechain"):
            continue
        if entry.get("type") == "assistant" and entry.get("message", {}).get("id") in message_ids:
            chosen.add(uid)
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_ids.add(block["id"])
    candidates: dict[str, list[str]] = {tool_id: [] for tool_id in tool_ids}
    for uid, (entry, _raw) in effective.items():
        content = entry.get("message", {}).get("content", [])
        if order[uid] <= cutoff and not entry.get("isSidechain") and entry.get("type") == "user" and isinstance(content, list):
            for block in content:
                tool_id = block.get("tool_use_id") if isinstance(block, dict) and block.get("type") == "tool_result" else None
                if tool_id in candidates:
                    candidates[tool_id].append(uid)
    for tool_id, matches in candidates.items():
        on_branch = [uid for uid in matches if uid in chosen]
        if len(on_branch) == 1:
            chosen.add(on_branch[0])
        elif len(on_branch) > 1 or len(matches) > 1:
            raise ValueError(f"Ambiguous replay tool-result branches: {tool_id}")
        elif matches:
            chosen.add(matches[0])
    selected = [item for uid, item in effective.items() if uid in chosen]
    result_ids = {block.get("tool_use_id") for entry, _raw in selected
                  for block in entry.get("message", {}).get("content", [])
                  if isinstance(block, dict) and block.get("type") == "tool_result"}
    if require_complete and not tool_ids <= result_ids:
        raise ValueError("Selected replay contains an incomplete tool round")
    if require_complete and any(entry.get("isApiErrorMessage") or entry.get("message", {}).get("model") == "<synthetic>"
           for entry, _raw in selected):
        raise ValueError("Selected replay contains a synthetic API-error row")
    return selected


def prepare_session_replay(
    *, session_id: str, cwd: Path, source_path: Path | None = None,
    target_uuid: str | None = None, projects_root: Path | None = None,
) -> dict[str, Any] | None:
    """Append minimal parent-only overlays; healthy maps are bytewise no-ops."""
    path = source_path or find_session_jsonl(session_id=session_id, cwd=cwd, projects_root=projects_root)
    if path is None:
        return None
    with path.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        original = handle.read()
        raw_lines = original.decode("utf-8").split("\n")
        entries = [(json.loads(raw), raw) for raw in raw_lines if raw.strip()]
        effective_lines = {}
        first_originals = {}
        for index, raw in enumerate(raw_lines, 1):
            if raw.strip():
                uid = json.loads(raw).get("uuid")
                effective_lines[uid] = index
                first_originals.setdefault(uid, (index, raw))
        selected = select_replay_entries(entries, target_uuid)
        overlays = []
        manifest = []
        parent = None
        for entry, raw in selected:
            if entry.get("parentUuid") != parent:
                patched = dict(entry)
                patched["parentUuid"] = parent
                overlays.append(json.dumps(patched, ensure_ascii=False, separators=(",", ":")))
                first_line, first_raw = first_originals[entry["uuid"]]
                manifest.append({"uuid": entry["uuid"], "original_event_line": first_line,
                                 "original_event_sha256": hashlib.sha256(first_raw.encode()).hexdigest(),
                                 "effective_prior_line": effective_lines[entry["uuid"]],
                                 "effective_prior_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                                 "old_parent": entry.get("parentUuid"), "new_parent": parent,
                                 "payload_sha256": hashlib.sha256(json.dumps(entry.get("message"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
            parent = entry["uuid"]
        prefix_sha = hashlib.sha256(original).hexdigest()
        loaded_sources = {"jsonl_replay": LOADED_SOURCE_SHA256}
        for name in ("jsonl_health", "jsonl_fork"):
            module = sys.modules.get(f"obs_agent.{name}")
            if module is not None:
                loaded_sources[name] = getattr(module, "LOADED_SOURCE_SHA256", "not-fingerprinted")
        result = {"session_id": session_id, "state": "no-op" if not overlays else "prepared_intent",
                  "loaded_sources": loaded_sources,
                  "path": str(path), "prefix_bytes": len(original),
                  "prefix_sha256": prefix_sha, "selected_uuids": [e["uuid"] for e, _raw in selected],
                  "overlays": manifest, "overlay_count": len(overlays)}
        if not overlays:
            return result
        provenance = Path(os.environ.get("OBS_SESSION_REPLAY_PROVENANCE_DIR", "/workspace/runtime/state/session-replay")) / session_id
        provenance.mkdir(parents=True, exist_ok=True)
        receipt = provenance / f"{prefix_sha}.json"
        receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        handle.seek(0)
        if handle.read() != original:
            raise RuntimeError("JSONL changed during disconnected replay preparation")
        handle.seek(0, os.SEEK_END)
        payload = (("" if original.endswith(b"\n") else "\n") + "\n".join(overlays) + "\n").encode()
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        if handle.read(len(original)) != original:
            raise RuntimeError("Original JSONL prefix changed after append-only preparation")
        result.update({"state": "applied", "appended_bytes": len(payload),
                       "appended_sha256": hashlib.sha256(payload).hexdigest(),
                       "prefix_verified_after": True, "manifest_path": str(receipt)})
        temporary = receipt.with_suffix(f".{os.getpid()}.new")
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(receipt)
        return result
