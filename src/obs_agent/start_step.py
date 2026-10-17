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
_SCAN_LIMIT = 48 * 1024 * 1024
_ids_cache: dict[str, tuple[float, frozenset[str]]] = {}
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
    """Only explicit user prompts or the result of this session's bead creation count.

    Fork-copied ancestor rows and arbitrary read/search/tool output are not registration.
    Decode JSON before stripping injected XML: raw JSON escapes its multiline contents.
    """
    if jsonl_path is None or not known:
        return False
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
                    if find_bead_ids(strip_injected_context(content), known):
                        return True
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
                        if find_bead_ids(strip_injected_context(text), known):
                            return True
    except OSError:
        return False
    return False


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
