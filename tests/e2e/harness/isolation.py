"""Isolation proof, obs-test side (design-v3 §2.1 probes (i)–(vi); G5, G11; ADR-0016, ADR-0036).

Probes (i)–(iii) run inside lanes (``suites/test_isolation.py`` through lanectl).
This module runs the parts only obs-test can see, before and after a run:

- (ii) the host sentinel: a process in obs-test's PID namespace whose argv[0]
  carries the agent-CLI marker, so ``kill_process_tree`` WOULD kill it if a lane
  could see it; it must be alive after the lane probes;
- (iv) production ``usage.jsonl``, ``effort-pins.json`` and
  ``cache-gate-bypass.jsonl``: size and mtime before/after; because production keeps
  serving traffic, a changed file passes only if none of the bytes appended in the
  run window mention a lane session id (usage) or a lane conversation key (pins);
- (v) the shared Claude refresh token's fingerprint is unchanged across the run
  (a lane token is expired during the run by the lane-side test);
- (vi) 0 ``Conflict: terminated by other getUpdates request`` lines appended to the
  production log in the run window, and no getUpdates/setWebhook/deleteWebhook/
  close/logOut row for the prod-send token in ``runner/botapi-calls.jsonl``, in a run
  that exercised prod-send.

Everything is written to ``<run>/isolation/``; values are fingerprints and counts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .creds import refresh_token_fingerprint

PROD_FILES = {
    "usage.jsonl": Path("/workspace/runtime/logs/cache-proxy/usage.jsonl"),
    "effort-pins.json": Path("/workspace/runtime/logs/cache-proxy/effort-pins.json"),
    "cache-gate-bypass.jsonl": Path("/workspace/runtime/state/cache-gate-bypass.jsonl"),
}
PROD_LOG = Path(os.environ.get("OBS_E2E_PROD_LOG") or "/workspace/runtime/logs/telegram-prod.log")
CONFLICT = b"Conflict: terminated by other getUpdates request"
FORBIDDEN_PROD_METHODS = {"getUpdates", "setWebhook", "deleteWebhook", "close", "logOut"}
AGENT_CLI_MARKER = "claude_agent_sdk/_bundled/claude"


def _stat(path: Path) -> dict:
    try:
        st = path.stat()
        return {"exists": True, "size": st.st_size, "mtime": st.st_mtime}
    except FileNotFoundError:
        return {"exists": False}


def start_host_sentinel(run_dir: Path) -> int:
    """A `sleep` in obs-test whose argv[0] contains the agent-CLI marker (not an orphan)."""
    marker_dir = run_dir / "isolation" / "sentinel" / AGENT_CLI_MARKER.rsplit("/", 1)[0]
    marker_dir.mkdir(parents=True, exist_ok=True)
    argv0 = str(marker_dir / "claude")
    # Own session and process group: production's orphan reaper skips group
    # leaders, so even if the launcher dies the sentinel is never touched by it.
    proc = subprocess.Popen(["bash", "-c", f'exec -a "{argv0}" sleep 14400'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            cmd = Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        except OSError:
            cmd = ""
        if AGENT_CLI_MARKER in cmd:
            break
        time.sleep(0.05)  # obs-e2e-lint: allow (harness: waiting for exec to replace argv)
    _SENTINELS[proc.pid] = proc
    return proc.pid


_SENTINELS: dict[int, subprocess.Popen] = {}


def sentinel_alive(pid: int) -> bool:
    proc = _SENTINELS.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_host_sentinel(pid: int) -> None:
    proc = _SENTINELS.pop(pid, None)
    if proc is not None and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def before_run(run_dir: Path, *, with_sentinel: bool) -> dict:
    out_dir = run_dir / "isolation"
    out_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "taken_at": time.time(),
        "files": {name: _stat(path) for name, path in PROD_FILES.items()},
        "refresh_token_fp": refresh_token_fingerprint(),
        "prod_log": {"path": str(PROD_LOG), **_stat(PROD_LOG)},
    }
    if with_sentinel:
        pid = start_host_sentinel(run_dir)
        state["host_sentinel"] = {"pid": pid, "alive": sentinel_alive(pid),
                                  "argv0": Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()}
    (out_dir / "before.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _appended(path: Path, start_size: int | None) -> bytes:
    if start_size is None or not path.exists():
        return b""
    with open(path, "rb") as fh:
        fh.seek(start_size)
        return fh.read()


def _lane_session_ids(run_dir: Path) -> set[str]:
    ids: set[str] = set()
    for events in (run_dir / "lanes").glob("*/events.jsonl"):
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for key in ("session_id", "old_session_id"):
                if event.get(key):
                    ids.add(str(event[key]))
    for project in (run_dir / "lanes").glob("*/home/.claude/projects/*/*.jsonl"):
        ids.add(project.stem)
    return ids


def _lane_pin_keys(run_dir: Path) -> set[str]:
    keys: set[str] = set()
    for pins in (run_dir / "lanes").glob("*/logs/cache-proxy/effort-pins.json"):
        try:
            data = json.loads(pins.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # EffortPinStore.texts de-duplicates common section content by its hash;
        # only the other tables' keys identify conversations and their prefixes.
        for name, table in data.items() if isinstance(data, dict) else []:
            if name != "texts" and isinstance(table, dict):
                keys.update(table.keys())
    return keys


def after_run(run_dir: Path, before: dict, *, prod_send_exercised_required: bool) -> dict:
    out_dir = run_dir / "isolation"
    lane_sessions = _lane_session_ids(run_dir)
    lane_pins = _lane_pin_keys(run_dir)
    results: dict = {"taken_at": time.time(), "lane_sessions": len(lane_sessions), "lane_pin_keys": len(lane_pins)}

    # (iv)
    files = {}
    ok_iv = True
    for name, path in PROD_FILES.items():
        start, end = before["files"][name], _stat(path)
        row = {"before": start, "after": end, "unchanged": start == end}
        if not row["unchanged"]:
            data = _appended(path, start.get("size"))
            if name == "usage.jsonl":
                hits = sum(1 for sid in lane_sessions if sid.encode() in data)
                row.update(appended_bytes=len(data), lane_session_hits=hits)
                row["verdict"] = "PASS (production traffic only)" if hits == 0 else "FAIL (lane rows in production usage)"
            elif name == "effort-pins.json":
                try:
                    prod = json.loads(path.read_text(encoding="utf-8"))
                    prod_keys = {k for name, table in prod.items() if name != "texts" and isinstance(table, dict) for k in table}
                except (OSError, ValueError):
                    prod_keys = set()
                hits = len(prod_keys & lane_pins)
                row.update(lane_key_hits=hits)
                row["verdict"] = "PASS (production pins only)" if hits == 0 else "FAIL (lane pins in production)"
            else:
                row["verdict"] = "FAIL (gate bypass log changed during the run)"
            ok_iv = ok_iv and row["verdict"].startswith("PASS")
        else:
            row["verdict"] = "PASS (unchanged)"
        files[name] = row
    results["iv"] = {"pass": ok_iv, "files": files}

    # (v)
    fp_after = refresh_token_fingerprint()
    results["v"] = {"pass": bool(before.get("refresh_token_fp")) and fp_after == before.get("refresh_token_fp"),
                    "before": before.get("refresh_token_fp"), "after": fp_after}

    # (vi)
    appended = _appended(PROD_LOG, before["prod_log"].get("size"))
    conflicts = appended.count(CONFLICT)
    calls_path = run_dir / "runner" / "botapi-calls.jsonl"
    calls = []
    if calls_path.exists():
        for line in calls_path.read_text(encoding="utf-8").splitlines():
            try:
                calls.append(json.loads(line))
            except ValueError:
                pass
    executed = [c for c in calls if not c.get("refused")]
    forbidden = [c for c in executed if c.get("method") in FORBIDDEN_PROD_METHODS]
    refused = [c for c in calls if c.get("refused")]
    exercised = any(c.get("method") == "sendMessage" for c in executed)
    results["vi"] = {
        "pass": conflicts == 0 and not forbidden and (exercised or not prod_send_exercised_required),
        "prod_log_window_bytes": len(appended), "conflict_lines": conflicts,
        "botapi_calls": len(executed), "methods": sorted({c.get("method") for c in executed}),
        "forbidden_calls": len(forbidden), "refused_attempts": len(refused), "prod_send_exercised": exercised,
    }

    # (ii) obs-test half
    sentinel = before.get("host_sentinel")
    if sentinel:
        results["ii_host"] = {"pid": sentinel["pid"], "alive_after": sentinel_alive(sentinel["pid"]),
                              "argv0": sentinel.get("argv0")}
    (out_dir / "after.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def summarize(run_dir: Path, after: dict) -> dict:
    """Combine lane-side (i)–(iii) records with the obs-test-side (ii)/(iv)–(vi)."""
    out_dir = run_dir / "isolation"
    lane_side: dict[str, dict] = {}
    for path in sorted(out_dir.glob("lane-*.json")):
        lane_side[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    def lanes_pass(key: str) -> bool:
        rows = [v.get(key) for v in lane_side.values() if v.get(key) is not None]
        return bool(rows) and all(r.get("pass") for r in rows)
    ii_host = after.get("ii_host") or {}
    summary = {
        "i": {"pass": lanes_pass("i"), "lanes": {k: v.get("i") for k, v in lane_side.items()}},
        "ii": {"pass": lanes_pass("ii") and bool(ii_host.get("alive_after")),
               "host_sentinel": ii_host, "lanes": {k: v.get("ii") for k, v in lane_side.items()}},
        "iii": {"pass": lanes_pass("iii"), "lanes": {k: v.get("iii") for k, v in lane_side.items()}},
        "iv": after.get("iv"), "v": after.get("v"), "vi": after.get("vi"),
    }
    summary["all_pass"] = all(bool((summary[p] or {}).get("pass")) for p in ("i", "ii", "iii", "iv", "v", "vi"))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
