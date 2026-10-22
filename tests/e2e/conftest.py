"""Root conftest for obs-e2e suites.

Tests run in the runner container's own PID namespace and act on a lane only
through ``lane`` (lanectl, inside the lane). As a second guard, ``os.kill`` and
``os.killpg`` refuse any signal to a process that is not this pytest process or
one of its descendants (the obs-3 guard of tests/conftest.py, kept for E2E).
"""

from __future__ import annotations

import os
from pathlib import Path

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
