"""Start-step check: a root agent's AgentTask launch should carry a registered task bead
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
* Only root agents (lineage length 1, or no lineage known) are checked; children get the brief
  path from their parent's prompt.
* A bead is "present" when an id that exists in the vault Beads store appears in the AgentTask
  prompt or prompt_file, or anywhere earlier in the caller's session transcript (the user's
  messages, register.py output, earlier tool inputs). Ids are confirmed against the store, so
  ordinary hyphenated words never count, and any project prefix works.
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


def transcript_has_bead(jsonl_path: Path | None, known: frozenset[str]) -> bool:
    if jsonl_path is None or not known:
        return False
    try:
        size = Path(jsonl_path).stat().st_size
        with open(jsonl_path, "rb") as fh:
            if size > _SCAN_LIMIT:
                fh.seek(size - _SCAN_LIMIT)
            for raw in fh:
                line = raw.decode("utf-8", errors="ignore")
                if find_bead_ids(line, known):
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
