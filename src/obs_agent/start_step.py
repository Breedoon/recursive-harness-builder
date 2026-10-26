"""Start-step check: every agent's AgentTask launch should carry a registered task bead
(bead vault-a6qm, Agent Operations "Task start").

Daniel 2026-10-13: even small tasks should be logged, "and there's some tasks seem like they're
small but they're actually big"; 2026-10-15: rules agents keep skipping "has to be ... enforced
potentially by like hooks". The Agent Operations start step (`register.py task`) creates the task
bead and a brief of prior attempts; this check makes skipping it visible.

Where it runs and why there: inside the AgentTask tool (every route, no per-agent ``hooks=``),
not as a native PreToolUse hook. Native hook output (additionalContext) is not persisted in the
session JSONL and the cache proxy strips it, so a hook-delivered warning would make the live
and replayed conversation differ. The warning is appended to the AgentTask tool result instead,
which is persisted like any tool output (block mode returns a normal tool error). Both keep
fork/resume prefixes byte-identical.

Rules:
* Every AgentTask launch is checked, including forks and child agents.
* A bead is present when an existing id is explicitly named in the launch/user prompt or
  prompt_file, or returned by a bead-creation command in the caller's own session. Unrelated
  tool output and fork-copied ancestor rows do not count. Ids are confirmed against the store.
* ``OBS_START_STEP_CHECK``: ``warn`` (default: launch proceeds, warning appended and logged),
  ``block`` (launch refused with the same text), ``off``.
* Every warning is logged to ``/workspace/runtime/state/start-step-warnings.jsonl``
  (``OBS_START_STEP_LOG``) so reflections can measure how often the start step is skipped.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_.-])([a-z][a-z0-9]*-[a-z0-9]{1,8}(?:\.[0-9]+)*)(?![A-Za-z0-9_-])")
_BRIEF_HEADER = re.compile(
    r"^# Task brief — ([a-z][a-z0-9]*-[a-z0-9]{1,8}(?:\.[0-9]+)*) / attempt "
    r"([a-z][a-z0-9]*-[a-z0-9]{1,8}(?:\.[0-9]+)*)$",
    re.MULTILINE,
)
_BRIEF_PATH = re.compile(
    r"(/workspace/runtime/git/obs-artifacts/Drafts/Artifacts/briefs/[A-Za-z0-9_./-]+\.md)"
)
_BRIEF_ROOT = Path("/workspace/runtime/git/obs-artifacts/Drafts/Artifacts/briefs")
_SCAN_LIMIT = 48 * 1024 * 1024
_ids_cache: dict[str, tuple[float, frozenset[str]]] = {}
_rows_cache: dict[str, tuple[float, dict[str, dict]]] = {}
START_PROCEDURE = "Projects/Personal Projects/Agentic/Agent Operations/procedures/task/start.md"
REGISTER = 'python3 "Projects/Personal Projects/Agentic/Agent Operations/scripts/ops/register.py" task'


def mode() -> str:
    value = (os.environ.get("OBS_START_STEP_CHECK") or "warn").strip().lower()
    return value if value in ("off", "warn", "block") else "warn"


def log_path() -> Path:
    return Path(os.environ.get("OBS_START_STEP_LOG") or "/workspace/runtime/state/start-step-warnings.jsonl")


def known_bead_ids(vault_path: Path | None) -> frozenset[str]:
    if vault_path is None:
        return frozenset()
    path = Path(os.environ.get("OBS_BEADS_ISSUES_FILE") or (Path(vault_path) / ".beads" / "issues.jsonl"))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset()
    cached = _ids_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    ids: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                match = re.search(r'"id"\s*:\s*"([^"]+)"', line)
                if match:
                    ids.add(match.group(1))
    except OSError:
        return frozenset()
    frozen = frozenset(ids)
    _ids_cache[str(path)] = (mtime, frozen)
    return frozen


def find_bead_ids(text: str | None, known: frozenset[str]) -> set[str]:
    if not text or not known:
        return set()
    return {m.group(1) for m in _CANDIDATE.finditer(text) if m.group(1) in known}


def known_bead_rows(vault_path: Path | None) -> dict[str, dict]:
    """Load current content-free Beads metadata on the launch path, never per request."""
    if vault_path is None:
        return {}
    path = Path(os.environ.get("OBS_BEADS_ISSUES_FILE") or (Path(vault_path) / ".beads" / "issues.jsonl"))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _rows_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    rows: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    rows[row["id"]] = row
    except OSError:
        return {}
    _rows_cache[str(path)] = (mtime, rows)
    _ids_cache[str(path)] = (mtime, frozenset(rows))
    return rows


def _bead_category(bead_id: str, rows: dict[str, dict]) -> str:
    row = rows.get(bead_id) or {}
    task_id = bead_id.rsplit(".", 1)[0] if "attempt" in (row.get("labels") or []) else bead_id
    task = rows.get(task_id) or row
    labels = set(row.get("labels") or []) | set(task.get("labels") or [])
    explicit = next((label.split(":", 1)[1] for label in labels if label.startswith("category:")), None)
    if explicit:
        return explicit
    prefix = task_id.split("-", 1)[0]
    if prefix == "obs" or labels & {"system:obs", "system:cache-proxy", "system:caching", "system:local-llm", "domain:obs-platform"}:
        return "obs"
    if prefix == "ops" or labels & {"system:agent-operations", "agent-operations", "domain:agentic", "domain:stewardship"}:
        return "agent-ops"
    return "projects" if prefix == "vault" else "ad-hoc"


def _select_nearest(ids: set[str], rows: dict[str, dict]) -> str | None:
    if not ids:
        return None
    # A registered attempt is nearer than its parent task; deeper child ids win.
    return max(ids, key=lambda bead_id: (bead_id.count("."), len(bead_id), bead_id))


def _binding_from_brief(text: str, rows: dict[str, dict]) -> dict[str, str | None] | None:
    match = _BRIEF_HEADER.search(text)
    if match is None:
        return None
    task_id, attempt_id = match.groups()
    attempt = rows.get(attempt_id) or {}
    if task_id not in rows or "attempt" not in (attempt.get("labels") or []):
        return None
    if attempt_id.rsplit(".", 1)[0] != task_id:
        return None
    labels = set(attempt.get("labels") or []) | set((rows.get(task_id) or {}).get("labels") or [])
    lane = next((label.split(":", 1)[1] for label in labels if label.startswith("lane:")), None)
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "category": _bead_category(attempt_id, rows),
        "lane": lane,
        "source": "registration_receipt",
    }


def resolve_attribution_binding(
    *,
    texts=(),
    receipt_texts=(),
    rows: dict[str, dict],
    jsonl_path: Path | None = None,
    session_id: str | None = None,
) -> dict[str, str | None] | None:
    """Resolve only an authoritative registration brief; ambiguous prose stays unassigned."""
    candidates: list[dict[str, str | None]] = []
    for text in receipt_texts:
        if text:
            binding = _binding_from_brief(str(text), rows)
            if binding:
                candidates.append(binding)
    for text in texts:
        if not text:
            continue
        for raw_path in _BRIEF_PATH.findall(str(text)):
            path = Path(raw_path).resolve()
            try:
                path.relative_to(_BRIEF_ROOT.resolve())
                content = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            binding = _binding_from_brief(content, rows)
            if binding:
                candidates.append(binding)
    unique = {
        (binding["task_id"], binding["attempt_id"]): binding
        for binding in candidates
    }
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


# Platform-injected context that every root transcript carries (CLAUDE.md and the OBS platform
# notes). CLAUDE.md names real bead ids (e.g. vault-x5tk, vault-0xs8), so counting this text made
# every root look registered and the warning never fired (found live 2026-10-16, ops-1.12).
_INJECTED = re.compile(
    r"<(obs-agent-entry-file-context|obs-platform-context)\b.*?</\1>", re.DOTALL
)


def strip_injected_context(text: str) -> str:
    return _INJECTED.sub("", text)


def transcript_has_bead(jsonl_path: Path | None, known: frozenset[str],
                        session_id: str | None = None) -> bool:
    return any(find_bead_ids(text, known)
               for text in transcript_bead_contexts(jsonl_path, session_id=session_id))


def transcript_bead_contexts(jsonl_path: Path | None, session_id: str | None = None):
    """Yield explicit prompts and this session's creation results, never arbitrary tool reads.

    Decode JSON before stripping injected XML; copied ancestor rows do not qualify.
    """
    if jsonl_path is None:
        return
    creation_calls: set[str] = set()
    try:
        size = Path(jsonl_path).stat().st_size
        with open(jsonl_path, "rb") as fh:
            if size > _SCAN_LIMIT:
                fh.seek(size - _SCAN_LIMIT)
            for raw in fh:
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if session_id and row.get("sessionId") not in (None, session_id):
                    continue
                message = row.get("message") or {}
                content = message.get("content") if isinstance(message, dict) else None
                if row.get("type") == "assistant" and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        command = str((block.get("input") or {}).get("command") or "")
                        if re.search(r"(?:register\.py[\"']?\s+task\b|bd(?:-safe)?[\"']?\s+create\b)", command):
                            creation_calls.add(block.get("id"))
                if row.get("type") != "user":
                    continue
                if isinstance(content, str):
                    yield strip_injected_context(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text") or ""
                        elif (block.get("type") == "tool_result"
                              and block.get("tool_use_id") in creation_calls and not block.get("is_error")):
                            text = json.dumps(block.get("content"), ensure_ascii=False)
                        else:
                            continue
                        yield strip_injected_context(text)
    except OSError:
        return


def is_root(lineage) -> bool:
    return not lineage or len(tuple(lineage)) <= 1


def warning_text(*, blocked: bool) -> str:
    head = "start-step check: launch refused" if blocked else "start-step check (launch went ahead)"
    return (
        f"{head} — no registered task bead in this task's prompt or this session. Register the "
        f"work first so it is logged and you get the brief of prior attempts: {REGISTER} "
        f"--title ... --requirement ... (see {START_PROCEDURE}); then pass the brief path and "
        "bead id to the child."
    )


def log_warning(*, session_id: str | None, agent_name: str | None, team_name: str | None,
                display_name: str | None, blocked: bool) -> None:
    entry = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session_id": session_id,
        "agent_name": agent_name,
        "team_name": team_name,
        "child": display_name,
        "mode": "block" if blocked else "warn",
    }
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
