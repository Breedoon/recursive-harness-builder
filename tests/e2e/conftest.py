"""Root conftest for obs-e2e suites.

Tests run in the runner container's own PID namespace and act on a lane only
through ``lane`` (lanectl, inside the lane). As a second guard, ``os.kill`` and
``os.killpg`` refuse any signal to a process that is not this pytest process or
one of its descendants (the obs-3 guard of tests/conftest.py, kept for E2E).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _inside_harness_container() -> bool:
    # The runner container gets OBS_E2E_RUN_ID from launcher._runner_env and runs under
    # `docker run --init`, so its PID 1 is docker-init; obs-test's PID 1 is supervisord.
    try:
        init = Path("/proc/1/comm").read_text().strip()
    except OSError:
        return False
    return bool(os.environ.get("OBS_E2E_RUN_ID")) and init == "docker-init"


# INVARIANT(side-effects): OBS tests run only inside an obs-e2e container (L9, AD-15). Collecting tests/e2e on obs-test starts nothing, so a pytest typed on the host never reaches production paths. There is no override switch. Breaking it lets a host pytest share obs-test's user and process tree with production.
# Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
if not _inside_harness_container():
    pytest.exit(
        "obs-e2e suites run only inside a harness container (L9, AD-15): use "
        "`deploy/obs-e2e/obs-e2e run --code <worktree> --suite <name> --profile <profile>`; "
        "see the OBS Platform Testing Protocol and the E2E Harness How to Run note.",
        returncode=2,
    )

_REAL_KILL, _REAL_KILLPG = os.kill, os.killpg
_PYTEST_PID = os.getpid()


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat.rsplit(")", 1)[1].split()[1])


def _is_own_process(pid: int) -> bool:
    seen = 0
    while pid > 1 and seen < 64:
        if pid == _PYTEST_PID:
            return True
        pid = _parent_pid(pid) or 0
        seen += 1
    return False


def _guarded_kill(pid, sig):
    if sig != 0:
        target = -pid if isinstance(pid, int) and pid < -1 else pid
        if type(target) is not int or target <= 1 or not _is_own_process(target):
            raise PermissionError(f"obs-e2e isolation: refused os.kill({pid!r}, {sig!r}); signal lane processes via lane.exec/lanectl")
    return _REAL_KILL(pid, sig)


def _guarded_killpg(pgid, sig):
    if sig != 0 and (type(pgid) is not int or pgid <= 1 or not _is_own_process(pgid)):
        raise PermissionError(f"obs-e2e isolation: refused os.killpg({pgid!r}, {sig!r})")
    return _REAL_KILLPG(pgid, sig)


os.kill = _guarded_kill
os.killpg = _guarded_killpg
