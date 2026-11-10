"""Refresh-token-free Claude credentials for E2E lanes (impl-plan-v3 §3 E1 WP4, design-v3 §2.1 item 4).

Each lane's HOME gets:
- ``.claude/.credentials.json``: a copy of the shared file with ``refreshToken``
  removed from every OAuth block;
- ``.claude.json``: ``oauthAccount`` and ``userID`` from the shared file. Other keys
  the lane's own CLIs write there (feature caches) are kept, so a re-sync never
  rolls a lane's state back; only those two keys are refreshed.

Both are written atomically (tmp file, fsync, rename) at lane start and every
10 minutes by :class:`ResyncThread`, which runs in the launcher (obs-test side),
the only place that can read the shared files.

INVARIANT(auth): a lane never holds the shared refresh token, so no lane CLI can
rotate it (the two-refresher failure, design-v3 §1.8.1). Breaking it lets a test
run refresh and invalidate the token every production agent uses.
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Profiles#Lane credentials
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

SHARED_CREDENTIALS = Path(os.environ.get("OBS_E2E_SHARED_CREDENTIALS") or "/home/agent/.claude/.credentials.json")
SHARED_CLAUDE_JSON = Path(os.environ.get("OBS_E2E_SHARED_CLAUDE_JSON") or "/home/agent/.claude.json")
RESYNC_SECONDS = 600


def _strip_refresh(data: object) -> object:
    if isinstance(data, dict):
        return {k: _strip_refresh(v) for k, v in data.items() if k != "refreshToken"}
    if isinstance(data, list):
        return [_strip_refresh(v) for v in data]
    return data


def _atomic_write_json(path: Path, payload: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]


def refresh_token_fingerprint(path: Path = SHARED_CREDENTIALS) -> str | None:
    """Fingerprint of the shared refresh token (isolation probe (v)); never the value."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = ((data or {}).get("claudeAiOauth") or {}).get("refreshToken")
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


def write_lane_credentials(home_dir: Path, *, expire_access_token: bool = False) -> dict:
    """Write one lane's credential files; return non-secret fingerprints.

    ``expire_access_token`` sets the lane copy's ``expiresAt`` to the past (isolation
    probe (v): a lane token expires while the shared refresh token must not move).
    """
    shared = json.loads(SHARED_CREDENTIALS.read_text(encoding="utf-8"))
    lane_creds = _strip_refresh(copy.deepcopy(shared))
    if expire_access_token and isinstance(lane_creds, dict):
        block = lane_creds.get("claudeAiOauth")
        if isinstance(block, dict):
            block["expiresAt"] = int((time.time() - 3600) * 1000)
    creds_path = home_dir / ".claude" / ".credentials.json"
    _atomic_write_json(creds_path, lane_creds)

    try:
        shared_json = json.loads(SHARED_CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shared_json = {}
    lane_json_path = home_dir / ".claude.json"
    try:
        lane_json = json.loads(lane_json_path.read_text(encoding="utf-8"))
        if not isinstance(lane_json, dict):
            lane_json = {}
    except (OSError, ValueError):
        lane_json = {}
    for key in ("oauthAccount", "userID"):
        if key in shared_json:
            lane_json[key] = shared_json[key]
    _atomic_write_json(lane_json_path, lane_json)

    assert "refreshToken" not in creds_path.read_text(encoding="utf-8"), "lane credential copy kept a refresh token"
    block = (lane_creds or {}).get("claudeAiOauth") or {}
    return {
        "credentials": str(creds_path),
        "access_token_fp": _sha(block.get("accessToken")),
        "expires_at": block.get("expiresAt"),
        "has_refresh_token": False,
        "claude_json_keys": sorted(lane_json.keys()),
        "written_at": time.time(),
    }


class ResyncThread(threading.Thread):
    """Re-write every lane's credentials every ``interval`` seconds until stopped."""

    def __init__(self, home_dirs: list[Path], interval: float = RESYNC_SECONDS, log=None):
        super().__init__(name="obs-e2e-cred-resync", daemon=True)
        self.home_dirs = list(home_dirs)
        self.interval = interval
        self._stop_event = threading.Event()
        self.log = log or (lambda msg: None)
        self.syncs = 0

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            for home in self.home_dirs:
                try:
                    info = write_lane_credentials(home)
                    self.log(f"[creds] re-synced {home} access_token_fp={info['access_token_fp']}")
                except Exception as exc:  # keep lanes running on the last good copy
                    self.log(f"[creds] re-sync failed for {home}: {type(exc).__name__}")
            self.syncs += 1

    def stop(self) -> None:
        self._stop_event.set()
