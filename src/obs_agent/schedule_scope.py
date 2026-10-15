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

import json
import logging
import os
import re
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
        if match:
            return match.group(1), adoption
        bead = adoption.get("bead")
        return (str(bead) if bead else None), adoption

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
