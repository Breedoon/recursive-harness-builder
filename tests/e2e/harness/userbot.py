"""The harness's single Telethon client and its cross-run session lock.

INVARIANT(credential-selection): exactly one Telethon client per harness run uses
the test userbot session, and it lives in the runner (orchestrator) process.
Lanes never get the session string; tests reach Telegram content only through
the runner's RPC (rpc.py). Parallel runs on one host serialize on an exclusive
``flock`` of the session's lock file, so the second run waits (or fails visibly
after ``--userbot-lock-timeout``). Breaking it risks AUTH_KEY_DUPLICATED and the
revocation of the session (design-v3 §2.1 item 6, ADR-0036).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Runner#Single userbot client
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .secrets import fingerprint

LOCK_DIR = Path(os.environ.get("OBS_E2E_LOCK_DIR") or "/workspace/runtime/e2e/locks")


class UserbotLockTimeout(RuntimeError):
    pass


@dataclass
class UserbotLock:
    """An exclusive flock held for the lifetime of the run's single client."""

    path: Path
    fd: int | None = None

    @classmethod
    def for_session(cls, session_string: str) -> "UserbotLock":
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        return cls(LOCK_DIR / f"userbot-{fingerprint(session_string).split(':', 1)[1]}.lock")

    def holder(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {}

    def acquire(self, *, timeout: float, owner: dict, on_wait=None) -> None:
        """Block up to ``timeout`` seconds (0 = try once); write the owner record."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(timeout, 0.0)
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
                if not announced and on_wait is not None:
                    on_wait(self.holder())
                    announced = True
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise UserbotLockTimeout(
                        f"userbot session lock {self.path} is held by {self.holder() or 'an unknown process'}"
                    ) from None
                # Lock contention is not an event source: poll the kernel lock.
                time.sleep(0.5)  # obs-e2e-lint: allow (harness lock poll, not a test wait)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({**owner, "pid": os.getpid(), "acquired_at": time.time()}).encode())
        os.fsync(fd)
        self.fd = fd

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def make_client(session_string: str, api_id: int, api_hash: str):
    """Construct (not connect) the run's one Telethon client."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session_string), int(api_id), api_hash)
