"""Isolation proof, obs-test side (design-v3 §2.1 probes (i)–(vi) and E-TP probe (vii); G5, G11; ADR-0016, ADR-0036).

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

- (vii) "a lane cannot touch production paths", obs-test halves (the lane half is
  ``suites/test_isolation.py``, which checks each lane's own mount table and writes
  token-named files only where the mounts allow):
  (a) ``docker inspect`` Mounts of every container the launcher started: every
  read-write source lies under the host path of this run's directory (the runner
  also gets the registry and locks directories under ``/workspace/runtime/e2e/``);
  (c) a read-only stat (mode, uid, gid; ctime reported, never judged: production
  writes into ``state/`` constantly) of the production directories and files before
  and after, plus a non-recursive scan of them for this run's probe token;
  (d) an audit hook in the launcher process (``sys.addaudithook``) records every
  write-flavoured call; every path must resolve under ``/workspace/runtime/e2e/``.

Everything is written to ``<run>/isolation/``; values are fingerprints and counts.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import signal
import subprocess
import sys
import threading
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
E2E_ROOT = Path("/workspace/runtime/e2e")
# Probe (vii)(c): production directories a lane must never change, listed non-recursively.
# The set matches the obs-fleetwatch mode/owner manifest (P0 owner ops-230, 2026-11-10): /workspace,
# /workspace/runtime, runtime/{state,git,secrets,bin}, /workspace/obs and the agent home. Only names
# starting with the probe token are reported; nothing is read inside them.
VII_STAT_DIRS = ("/workspace", "/workspace/runtime", "/workspace/runtime/state", "/workspace/runtime/git",
                 "/workspace/runtime/secrets", "/workspace/runtime/bin", "/workspace/obs", "/home/agent")
VII_SCAN_EXTRA = ("/workspace/obs/.venv",)
_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def probe_token(run_id: str) -> str:
    """The file-name prefix lane-side probe (vii) uses; obs-test scans production directories for it."""
    return f".obs-e2e-vii-{run_id}"


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
        "vii": _vii_snapshot(),
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


def _vii_stat(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"exists": False, "errno": exc.errno}
    return {"exists": True, "mode": oct(st.st_mode & 0o7777), "uid": st.st_uid, "gid": st.st_gid, "ctime": st.st_ctime}


def _vii_snapshot() -> dict:
    return {"stat": {path: _vii_stat(path) for path in (*VII_STAT_DIRS, *map(str, PROD_FILES.values()))}}


def _vii_scan(token: str, directories: list[str]) -> dict:
    """Non-recursive: names in each directory that start with the probe token."""
    found: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for directory in directories:
        try:
            with os.scandir(directory) as entries:
                hits = sorted(entry.name for entry in entries if entry.name.startswith(token))
        except OSError as exc:
            errors[directory] = exc.__class__.__name__
            continue
        if hits:
            found[directory] = hits
    return {"found": found, "unreadable": errors}


def _under(path: str, root: str) -> bool:
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root + "/")


def _vii_part_c(run_dir: Path, before: dict) -> dict:
    token = probe_token(run_dir.name)
    after = _vii_snapshot()["stat"]
    rows, ok = {}, True
    for path, start in before["vii"]["stat"].items():
        end = after[path]
        same = all(start.get(key) == end.get(key) for key in ("exists", "mode", "uid", "gid"))
        rows[path] = {"before": start, "after": end, "mode_uid_gid_equal": same,
                      "ctime_changed": start.get("ctime") != end.get("ctime")}
        ok = ok and same
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    code_dir = (manifest.get("code") or {}).get("mounted_path")
    scanned = [*VII_STAT_DIRS, *VII_SCAN_EXTRA, *([code_dir] if code_dir else [])]
    scan = _vii_scan(token, scanned)
    return {"pass": ok and not scan["found"] and not scan["unreadable"], "token": token, "stat": rows,
            "ctime_note": "ctime is reported, not judged: production writes into state/ constantly",
            "scanned_directories": scanned, "token_found": scan["found"], "unreadable": scan["unreadable"]}


def _vii_part_a(run_dir: Path, registry_dir: Path) -> dict:
    from . import docker
    host_run = docker.to_host_path(run_dir)
    host_e2e = docker.to_host_path(E2E_ROOT)
    runner_extra = [docker.to_host_path(registry_dir), docker.to_host_path(E2E_ROOT / "locks")]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((run_dir / "isolation").glob("mounts-*.json"))]
    containers, violations = {}, []
    for record in records:
        rw = [row for row in record["mounts"] if row.get("RW")]
        for row in rw:
            source = row.get("Source") or ""
            allowed = row.get("Type") == "bind" and (
                _under(source, host_run) or
                (record["role"] == "runner" and _under(source, host_e2e) and any(_under(source, extra) for extra in runner_extra)))
            if not allowed:
                violations.append({"container": record["container"], "role": record["role"],
                                   "type": row.get("Type"), "source": source, "destination": row.get("Destination")})
        containers[record["container"]] = {
            "role": record["role"], "mounts": len(record["mounts"]),
            "rw_sources": [{"source": row.get("Source"), "destination": row.get("Destination"), "type": row.get("Type")} for row in rw]}
    expected = {lane["container"] for lane in manifest.get("lanes", [])} | {f"obs-e2e-{run_dir.name}-runner"}
    missing = sorted(expected - set(containers))
    return {"pass": not violations and not missing, "host_run_dir": host_run, "host_e2e_root": host_e2e,
            "runner_rw_allowed": runner_extra, "containers": containers, "violations": violations,
            "missing_records": missing}


# ---- (vii)(d): the launcher's own writes -------------------------------------------------------
_WRITE_EVENTS = {"os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chown", "shutil.rmtree",
                 "os.symlink", "os.link", "os.truncate", "os.utime"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# Argument positions of the directory file descriptors of each event, one per path argument:
# an fd-relative name (shutil.rmtree's internals) is joined to what /proc/self/fd/<fd> points at.
_DIR_FD_ARGS = {"os.remove": (1,), "os.rmdir": (1,), "os.mkdir": (2,), "os.chmod": (2,), "os.chown": (3,),
                "os.rename": (2, 3), "os.link": (2, 3), "os.symlink": (None, 2), "os.utime": (3,), "shutil.rmtree": (1,)}
# Devices the launcher opens for writing to silence a child's stdio (subprocess.DEVNULL): not files.
_EXEMPT_DEVICES = ("/dev/null",)
_audit_rows: list[dict] = []
_audit_lock = threading.Lock()
_audit_state = {"installed": False, "flushed": 0}
_audit_local = threading.local()


def _audit_hook(event: str, args: tuple) -> None:
    if getattr(_audit_local, "quiet", False):
        return
    try:
        if event == "open":
            path, _mode, flags = args[0], args[1], args[2]
            if isinstance(path, int) or not isinstance(flags, int) or not flags & _WRITE_FLAGS:
                return
            row = {"event": "open-write", "paths": [os.fsdecode(os.fspath(path))]}
        elif event in _WRITE_EVENTS:
            fds = _DIR_FD_ARGS.get(event, ())
            paths = []
            for index, item in enumerate(args[:2]):
                if not isinstance(item, (str, bytes, os.PathLike)) or (event == "os.symlink" and index == 0):
                    continue  # symlink(src, dst): only dst is written
                name = os.fsdecode(os.fspath(item))
                fd = args[fds[index]] if index < len(fds) and fds[index] is not None and len(args) > fds[index] else None
                if isinstance(fd, int) and fd >= 0 and not os.path.isabs(name):
                    name = os.path.join(os.readlink(f"/proc/self/fd/{fd}"), name)
                paths.append(name)
            row = {"event": event, "paths": paths}
        elif event == "subprocess.Popen":
            argv = args[1] if len(args) > 1 else []
            row = {"event": event, "argv": [_BOT_TOKEN.sub("bot<id>:<redacted>", os.fsdecode(item)) for item in argv]}
        else:
            return
        row["t"], row["cwd"] = time.time(), os.getcwd()
        with _audit_lock:
            _audit_rows.append(row)
    except Exception:
        return  # an audit hook must never break the launcher


def install_write_audit() -> None:
    """Start recording the launcher's writes in memory; ``flush_write_audit`` saves them."""
    if not _audit_state["installed"]:
        sys.addaudithook(_audit_hook)
        _audit_state["installed"] = True


def flush_write_audit(run_dir: Path) -> list[dict]:
    """Append rows not yet saved to isolation/launcher-writes.jsonl; return the new, resolved rows."""
    with _audit_lock:
        rows = _audit_rows[_audit_state["flushed"]:]
        _audit_state["flushed"] = len(_audit_rows)
    root = str(E2E_ROOT.resolve())
    for row in rows:
        if "paths" in row:
            row["resolved"] = [os.path.realpath(os.path.join(row["cwd"], path)) for path in row["paths"]]
            row["exempt_device"] = all(path in _EXEMPT_DEVICES for path in row["resolved"])
            row["under_e2e"] = all(_under(path, root) or path in _EXEMPT_DEVICES for path in row["resolved"])
    _audit_local.quiet = True
    try:
        with open(run_dir / "isolation" / "launcher-writes.jsonl", "a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
    finally:
        _audit_local.quiet = False
    return rows


def _vii_part_d(run_dir: Path) -> dict:
    if not _audit_state["installed"]:
        return {"pass": False, "error": "write audit was not installed in the launcher"}
    flush_write_audit(run_dir)
    rows = [json.loads(line) for line in (run_dir / "isolation" / "launcher-writes.jsonl").read_text(encoding="utf-8").splitlines()]
    writes = [row for row in rows if "paths" in row]
    outside = [row for row in writes if not row["under_e2e"]]
    root = str(E2E_ROOT.resolve())
    by_dir: dict[str, int] = {}
    for row in writes:
        for path in row["resolved"]:
            key = path if not _under(path, root) else "e2e/" + (path[len(root) + 1:].split("/")[0] if path != root else "(the directory itself)")
            by_dir[key] = by_dir.get(key, 0) + 1
    children: dict[str, int] = {}
    for row in rows:
        if row["event"] == "subprocess.Popen" and row["argv"]:
            name = posixpath.basename(row["argv"][0])
            children[name] = children.get(name, 0) + 1
    return {"pass": bool(writes) and not outside, "events": len(rows), "write_events": len(writes),
            "by_directory": dict(sorted(by_dir.items())), "subprocess_children_by_command": children,
            "exempt_device_events": sum(1 for row in writes if row.get("exempt_device")),
            "exempt_reason": "open for write of /dev/null: the launcher's subprocess.DEVNULL stdio, a device, not a file",
            "outside_e2e": [{"event": row["event"], "resolved": row["resolved"], "cwd": row["cwd"]} for row in outside][:50],
            "outside_e2e_count": len(outside),
            "note": "subprocess children (git, tar, ssh) are listed, not judged; their writes are covered by code reading and (c)"}


def after_run(run_dir: Path, before: dict, *, prod_send_exercised_required: bool, registry_dir: Path | None = None) -> dict:
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
    # (vii) obs-test halves: (a) mount table, (c) production stat/scan, (d) launcher write audit
    registry = registry_dir or Path(os.environ.get("OBS_E2E_REGISTRY_DIR") or E2E_ROOT / "registry")
    results["vii_host"] = {"a": _vii_part_a(run_dir, registry), "c": _vii_part_c(run_dir, before),
                           "d": _vii_part_d(run_dir)}
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
    host = after.get("vii_host") or {}
    summary["vii"] = {"pass": lanes_pass("vii") and all(bool((host.get(part) or {}).get("pass")) for part in ("a", "c", "d")),
                      "a_mounts": host.get("a"), "b_lanes": {k: v.get("vii") for k, v in lane_side.items()},
                      "c_production_paths": host.get("c"), "d_launcher_writes": host.get("d")}
    summary["all_pass"] = all(bool((summary[p] or {}).get("pass")) for p in ("i", "ii", "iii", "iv", "v", "vi", "vii"))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
