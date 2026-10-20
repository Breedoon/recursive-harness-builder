"""Shared-service freeze check for OBS's own restart and deploy paths (bead ops-47.6).

Why: the Agent Operations freeze tool (``scripts/ops/freeze.py`` in the vault) is enforced by a
project-level PreToolUse hook on the Bash tool. That hook only sees commands an agent types. It
cannot see a restart that a Python entry point performs itself, e.g. ``python -m
obs_agent.maintenance_restart`` sending SIGUSR1, or the cache-gate pre-push hook. Every such entry
point asks this module first, passing the command text it is about to run.

Contract (mirrors freeze.py so the two can never disagree):

* ``freeze.py check --command <text> --cwd <dir>`` exits 3 when an active freeze covers the
  command, 0 when allowed. The freeze owner (or someone it cleared) runs anyway by setting
  ``OPS_FREEZE_ACK=<freeze id>``; we append that to the checked text so freeze.py's own ack rule
  decides.
* Fail open. A missing tool, a timeout, an unexpected exit code or any exception allows the action
  and is reported in ``FreezeCheck.error``. A broken guard must never stop a restart.

Paths: ``OBS_FREEZE_TOOL`` overrides the tool path (tests); ``OPS_FREEZE_STATE`` is inherited by
the subprocess, so tests can point it at a scratch state file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOOL = Path(
    "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/"
    "Agent Operations/scripts/ops/freeze.py"
)
BLOCKED_RC = 3
TIMEOUT_SECONDS = 10.0
ACK_ENV = "OPS_FREEZE_ACK"


@dataclass
class FreezeCheck:
    blocked: bool
    message: str = ""
    error: str | None = None


def tool_path() -> Path:
    return Path(os.environ.get("OBS_FREEZE_TOOL") or DEFAULT_TOOL)


def check(command: str, cwd: str | os.PathLike | None = None) -> FreezeCheck:
    """Ask freeze.py whether *command* may run now. Never raises; fails open."""
    tool = tool_path()
    if not tool.is_file():
        return FreezeCheck(blocked=False, error=f"freeze tool not found at {tool}")
    text = command
    ack = os.environ.get(ACK_ENV, "").strip()
    if ack:
        text = f"{ACK_ENV}={ack} {command}"
    argv = [sys.executable or "python3", str(tool), "check", "--command", text]
    if cwd is not None:
        argv += ["--cwd", str(cwd)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
    except Exception as exc:  # noqa: BLE001 - fail open by contract
        return FreezeCheck(blocked=False, error=f"freeze check could not run: {exc}")
    out = (proc.stdout or "").strip()
    if proc.returncode == BLOCKED_RC:
        return FreezeCheck(blocked=True, message=out or "blocked by an active shared-service freeze")
    if proc.returncode == 0:
        return FreezeCheck(blocked=False, message=out)
    err = (proc.stderr or out or "").strip()[:400]
    return FreezeCheck(blocked=False, error=f"freeze check exited {proc.returncode}: {err}")
