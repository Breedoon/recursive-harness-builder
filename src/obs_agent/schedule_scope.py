"""Scope selection and responsibility protection for /unschedule and /stop (vault-maw0).

Pure helpers (no Telegram objects) so the scope rules are unit-testable:

* ``parse_scope_args`` reads ``/unschedule [all|tree|branch|chat] [--include-protected]`` and
  ``/stop [all|chat]``.
* ``ScheduleOwnership`` answers "is this schedule owned by an ongoing responsibility?" from the
  Agent Operations conventions: a ``[bead-id]`` prefix on the schedule description, else the
  adoption map ``schedule-owners.json``; the owner bead is read from the vault Beads store. A
  schedule is *protected* when its owner bead is open and either carries the ``responsibility``
  label or names an ``owner_team`` other than the tree being swept. That mirrors
  ``scripts/ops/schedules.py --tree`` in the vault, so the Scheduler's inventory and this command
  agree on what a sweep spares.
* ``lineage_in_branch`` is the branch rule shared with ``/stop_branch``.

Scopes (Daniel, 2026-10-15, bead vault-maw0):

* ``branch`` — the agent of this topic and its recursive descendants.
* ``tree`` — the whole root-team tree, trunk included.
* ``all`` — every schedule of every agent in every tree (``/stop all``: every running agent).
* ``chat`` — the pre-2026-10-15 meaning of ``all``: every topic of this Telegram chat.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile

from obs_agent import start_step
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

SCOPES = ("all", "tree", "branch", "chat")
_PROTECTED_FLAGS = {"include-protected", "include_protected", "protected", "force"}
_PREFIX = re.compile(r"^\s*\[([a-z][a-z0-9]*-[a-z0-9]+(?:\.[0-9]+)*)\]")
OWNERS_RELATIVE_PATH = Path("Projects/Personal Projects/Agentic/Agent Operations/scripts/ops/schedule-owners.json")


@dataclass(frozen=True)
class ScopeArgs:
    scope: str | None  # None = the command's per-topic default
    include_protected: bool = False
    rest: tuple[str, ...] = ()


def parse_scope_args(args: Iterable[str] | None) -> ScopeArgs:
    """Split command args into a scope keyword, the include-protected flag and the rest.

    Accepts ``all@botname`` (Telegram mention suffix) and dash variants of the flag, because phone
    keyboards turn ``--`` into an em dash.
    """
    scope: str | None = None
    include_protected = False
    rest: list[str] = []
    for raw in args or ():
        token = (raw or "").strip()
        if not token:
            continue
        lowered = token.lower()
        if "@" in lowered:
            lowered = lowered.split("@", 1)[0].strip()
        bare = lowered.lstrip("-\u2014\u2013+")
        if bare in _PROTECTED_FLAGS and lowered != bare:
            include_protected = True
            continue
        if bare in ("include-protected", "include_protected"):
            include_protected = True
            continue
        if scope is None and lowered in SCOPES:
            scope = lowered
            continue
        rest.append(token)
    return ScopeArgs(scope=scope, include_protected=include_protected, rest=tuple(rest))


def lineage_in_branch(candidate: tuple[str, ...], branch: tuple[str, ...]) -> bool:
    return bool(branch) and tuple(candidate[: len(branch)]) == tuple(branch)


@dataclass
class Protection:
    protected: bool
    owner_bead: str | None = None
    reason: str = ""


@dataclass
class ScheduleOwnership:
    """Read-once view of schedule owners for one command invocation."""

    beads: dict[str, dict] = field(default_factory=dict)
    owners: dict[str, dict] = field(default_factory=dict)
    load_errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, vault_path: Path | None) -> "ScheduleOwnership":
        result = cls()
        if vault_path is None:
            result.load_errors.append("no vault path configured")
            return result
        owners_path = Path(os.environ.get("OBS_SCHEDULE_OWNERS_FILE") or (Path(vault_path) / OWNERS_RELATIVE_PATH))
        beads_path = Path(os.environ.get("OBS_BEADS_ISSUES_FILE") or (Path(vault_path) / ".beads" / "issues.jsonl"))
        try:
            if owners_path.exists():
                data = json.loads(owners_path.read_text(encoding="utf-8"))
                result.owners = dict(data.get("schedules") or {})
        except Exception as exc:  # malformed map must not break the command
            result.load_errors.append(f"owners map unreadable: {exc}")
            logger.warning("schedule owners map unreadable path=%s", owners_path, exc_info=True)
        try:
            if beads_path.exists():
                with open(beads_path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(row, dict) and row.get("id"):
                            result.beads[str(row["id"])] = row
        except Exception as exc:
            result.load_errors.append(f"beads store unreadable: {exc}")
            logger.warning("beads store unreadable path=%s", beads_path, exc_info=True)
        return result

    def owner_of(self, *, schedule_id: str, description: str | None) -> tuple[str | None, dict]:
        match = _PREFIX.match(description or "")
        adoption = self.owners.get(schedule_id) or {}
        bead = adoption.get("bead")
        if match and (match.group(1) in self.beads or not bead):
            return match.group(1), adoption
        # Match schedules.py: a non-bead route tag cannot defeat a real adoption.
        return (str(bead) if bead else None), adoption

    def creation_warnings(self, *, schedule_id: str, description: str | None) -> list[str]:
        """Diagnose invalid explicit ownership or legacy mappings after automatic assignment."""
        bead_id, _ = self.owner_of(schedule_id=schedule_id, description=description)
        rule = "Agent Operations schedule ownership rule (Scheduler steps 3–4)"
        if not bead_id:
            return [f"{rule}: missing [bead-id] description prefix or schedule-owners.json mapping; "
                    f"adopt schedule {schedule_id} under an open task/responsibility bead."]
        if bead_id not in self.beads or self.beads[bead_id].get("status") == "closed":
            return [f"{rule}: owner {bead_id} is missing or closed; schedule {schedule_id} is orphaned."]
        return []

    def protection(self, *, schedule_id: str, description: str | None, tree: str | None) -> Protection:
        bead_id, adoption = self.owner_of(schedule_id=schedule_id, description=description)
        if not bead_id:
            return Protection(False)
        row = self.beads.get(bead_id)
        if row is None or row.get("status") == "closed":
            return Protection(False, bead_id, "owner bead closed or missing")
        labels = row.get("labels") or []
        if "responsibility" in labels:
            return Protection(True, bead_id, "ongoing responsibility")
        owner_team = (row.get("metadata") or {}).get("owner_team") or adoption.get("owner_team")
        if tree and owner_team and owner_team != tree:
            return Protection(True, bead_id, f"owned by tree {owner_team}")
        return Protection(False, bead_id, "owned by this tree's own work")


def _owner_from_text(text: str, beads: dict[str, dict]) -> str | None:
    text = start_step.strip_injected_context(text)
    known = frozenset(bid for bid, row in beads.items() if row.get("status") != "closed")
    bead_pattern = r"([a-z][a-z0-9]*-[a-z0-9]{1,8}(?:\.[0-9]+)*)"
    for head in (r"\blane(?:\s+bead)?", r"\btask(?:\s+bead)?", r"\bbead(?:\s+id)?",
                 r"briefs/(?:\d{4}-\d{2}/)?"):
        for match in re.finditer(head + r"[\s:`*\"'=]*" + bead_pattern, text, re.IGNORECASE):
            if match.group(1) in known:
                return match.group(1)
    ids = start_step.find_bead_ids(text, known)
    return next(iter(ids)) if len(ids) == 1 else None


def _ensure_orphan_check(vault: Path, owners: ScheduleOwnership, *, team: str,
                         agent: str, session_id: str | None) -> str:
    scope = f"{team}/{agent}"
    prior = {bid: row for bid, row in owners.beads.items()
             if (row.get("metadata") or {}).get("schedule_owner_scope") == scope}
    for bid, row in prior.items():
        if row.get("status") != "closed":
            return bid
    generation = 0
    while True:
        seed = scope if generation == 0 else f"{scope}/generation/{generation}"
        bid = "ops-" + hashlib.sha256(seed.encode()).hexdigest()[:8]
        if bid not in owners.beads:
            break
        generation += 1
    due = dt.datetime.now(dt.timezone.utc).date().isoformat()
    what = f"Resolve schedule ownership for {agent} in lineage tree {team}"
    metadata = {"owner_team": team, "schedule_owner_scope": scope,
                "schedule_owner_generation": generation, "prior_schedule_owner_checks": list(prior),
                "check": {"kind": "delayed-verification", "of": None, "class": "script",
                          "series": 1, "n": 1, "of_n": 1, "due": due, "methods": [],
                          "what": what, "source_agent": agent, "source_session": session_id}}
    description = (f"Schedule registration automatically created this lineage-scoped orphan check because "
                   f"no open task owner was found for agent {agent} or its ancestors in tree {team}. "
                   f"Source session: {session_id}. Inspect its schedules and assign a substantive "
                   "task/responsibility owner; do not delete useful work just for missing registration. "
                   "Prior closed checks retain their exact history; new work gets a fresh generation.\n"
                   "priority-authority: none — tooling default, do not trust")
    command = [str(vault / ".beads" / "bd-safe"), "create", "--force", "--id", bid,
               "--title", what[:200], "--type", "chore",
               "--labels", "check,check-class:script,orphan-check,schedule-owner",
               "--defer", due, "--description", description, "--metadata", json.dumps(metadata), "--json"]
    subprocess.run(command, cwd=vault, check=True, capture_output=True, text=True, timeout=60)
    persisted = ScheduleOwnership.load(vault).beads.get(bid)
    if not persisted or persisted.get("status") == "closed":
        raise RuntimeError("schedule ownership check was not published by bd-safe")
    return bid


def assign_creation_owner(*, vault: Path, schedule_id: str, description: str | None,
                          team: str, agent: str, session_id: str | None,
                          contexts: list[dict]) -> dict | None:
    """Assign before schedule registration; no policy refusal or confirmation gate."""
    if _PREFIX.match(description or ""):
        return None
    path = Path(os.environ.get("OBS_SCHEDULE_OWNERS_FILE") or (vault / OWNERS_RELATIVE_PATH))
    lock_name = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    # Empty, ephemeral mutex only; durable evidence remains in Beads and the owners map.
    with open(Path(tempfile.gettempdir()) / f"obs-schedule-owner-{lock_name}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        owners = ScheduleOwnership.load(vault)
        bid = None
        source = "orphan-check"
        owner_agent = agent
        for context in contexts:
            name = context["agent"]
            texts = list(context.get("texts") or [])
            if context.get("jsonl"):
                texts += list(start_step.transcript_bead_contexts(
                    context["jsonl"], session_id=context.get("session_id")))[::-1]
            for text in texts:
                bid = _owner_from_text(text, owners.beads)
                if bid:
                    break
            if not bid:
                remembered = {entry["bead"] for entry in owners.owners.values()
                              if entry.get("owner_agent") == name and entry.get("bead") in owners.beads
                              and owners.beads[entry["bead"]].get("status") != "closed"}
                bid = next(iter(remembered)) if len(remembered) == 1 else None
            if not bid:
                registered = {key for key, row in owners.beads.items()
                              if row.get("status") != "closed" and
                              ((row.get("metadata") or {}).get("agents") or {}).get("root_agent") == name}
                bid = next(iter(registered)) if len(registered) == 1 else None
            if bid:
                owner_agent = name
                source = ("orphan-check" if (owners.beads[bid].get("metadata") or {}).get("schedule_owner_scope")
                          else context.get("source") or ("session-task" if name == agent else "ancestor-task"))
                break
        if not bid:
            bid = _ensure_orphan_check(vault, owners, team=team, agent=agent, session_id=session_id)
        data = json.loads(path.read_text()) if path.exists() else {"schedules": {}}
        note = f"Schedule ownership automatically assigned {bid} from {source} ({owner_agent})"
        data.setdefault("schedules", {})[schedule_id] = {
            "bead": bid, "owner_team": team, "owner_agent": owner_agent, "note": note,
        }
        # Preserve register.py's one-entry-per-line owners-map representation.
        blocks = []
        for key, value in data.items():
            if isinstance(value, dict):
                items = [f"    {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in value.items()]
                blocks.append(f"  {json.dumps(key)}: {{\n" + ",\n".join(items) + ("\n" if items else "") + "  }")
            else:
                blocks.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
        text = "{\n" + ",\n".join(blocks) + "\n}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        pending_path = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as pending:
                pending_path = Path(pending.name)
                pending.write(text)
                pending.flush()
                os.fsync(pending.fileno())
            os.replace(pending_path, path)
        finally:
            if pending_path is not None:
                pending_path.unlink(missing_ok=True)
        return {"bead": bid, "source": source, "agent": owner_agent, "note": note}


def format_unschedule_report(
    *,
    scope: str,
    deleted: dict[str, list[tuple[str, str]]],
    protected: list[tuple[str, str, Protection]],
    load_errors: list[str],
    max_ids: int = 40,
) -> str:
    """Per-tree counts plus ids/descriptions, protected schedules kept, and how to include them."""
    total = sum(len(items) for items in deleted.values())
    lines = [f"/unschedule {scope}: removed {total} schedule(s)"]
    shown = 0
    for tree in sorted(deleted, key=lambda t: (t == "", t)):
        items = deleted[tree]
        lines.append(f"- {tree or 'unknown tree'}: {len(items)}")
        for schedule_id, description in items:
            if shown >= max_ids:
                break
            lines.append(f"  {schedule_id} {(description or '')[:70]}")
            shown += 1
    if shown < total:
        lines.append(f"  … {total - shown} more")
    if protected:
        lines.append(f"kept {len(protected)} protected schedule(s):")
        for schedule_id, description, prot in protected[:max_ids]:
            lines.append(f"  {schedule_id} [{prot.owner_bead}: {prot.reason}] {(description or '')[:50]}")
        lines.append(f"to remove them too: /unschedule {scope} --include-protected")
    if load_errors:
        lines.append("owner data incomplete: " + "; ".join(load_errors))
    return "\n".join(lines)
