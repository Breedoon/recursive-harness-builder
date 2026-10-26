#!/usr/bin/env python3
"""Planning-only, lane-local lifecycle shape fixtures; never OBS runtime evidence alone."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import sys
import time
from pathlib import Path


FLAGS = ("OBS_STOP_KILL_GRACE_SECONDS", "OBS_ORPHAN_CLI_REAP", "OBS_CLI_HANG_WATCHDOG_SECONDS",
         "OBS_COMPACT_POLICY", "OBS_CLAUDE_KILL_ON_IDLE", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
         "CLAUDE_CODE_EAGER_FLUSH", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS", "ENABLE_TOOL_SEARCH",
         "ENABLE_CLAUDEAI_MCP_SERVERS", "CLAUDE_CODE_ENABLE_TASKS", "DISABLE_AUTO_COMPACT")


def record(root: Path, kind: str, **fields) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "signals.jsonl").open("a") as handle:
        stat = (Path("/proc") / str(os.getpid()) / "stat").read_text().rsplit(")", 1)[1].split()
        handle.write(json.dumps({"kind": kind, "ts": time.time(), "pid": os.getpid(), "start": int(stat[19]),
                                 "ppid": os.getppid(), "pgid": os.getpgrp(), "sid": os.getsid(0), **fields}) + "\n")
        handle.flush()


def hold(root: Path, kind: str) -> str:
    fifo = root / "release.fifo"
    root.mkdir(parents=True, exist_ok=True)
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        record(root, kind)
        deadline = time.monotonic() + 180
        heartbeat = (root / "heartbeat-enabled").exists()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("EV-C bounded shape fixture was never released")
            if heartbeat:
                record(root, "heartbeat")
            readable, _, _ = select.select([descriptor], [], [], min(0.5, remaining) if heartbeat else remaining)
            if readable:
                os.read(descriptor, 4096)
                break
    finally:
        os.close(descriptor)
    return "EV-C fixture released"


def _safe_cli_flags(args: list[bytes]) -> dict:
    tokens = [value.decode(errors="replace") for value in args if value]
    safe_values = {"--input-format", "--output-format", "--model", "--permission-mode", "--setting-sources", "--resume", "--session-id"}
    result = {"flag_names": [value for value in tokens if value.startswith("--")]}
    for index, value in enumerate(tokens[:-1]):
        if value in safe_values:
            result[value] = tokens[index + 1]
        elif value == "--settings":
            try:
                settings = json.loads(tokens[index + 1])
                result["settings_top_level_keys"] = sorted(settings)
                result["settings_env_names"] = sorted(settings.get("env", {}))
            except (ValueError, TypeError, AttributeError):
                result["settings_parse_error"] = True
        elif value == "--mcp-config":
            try:
                servers = json.loads(tokens[index + 1]).get("mcpServers", {})
                result["sdk_mcp_server_names"] = sorted(servers)
            except (ValueError, TypeError, AttributeError):
                result["mcp_config_parse_error"] = True
    return result


def _process_record(entry: Path, *, cli: bool) -> dict:
    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
    env, env_names = {}, []
    try:
        for item in (entry / "environ").read_bytes().split(b"\0"):
            name, separator, value = item.partition(b"=")
            if not separator:
                continue
            key = name.decode(errors="replace")
            env_names.append(key)
            if key in FLAGS:
                env[key] = value.decode(errors="replace")
    except OSError:
        pass
    try:
        binary = Path(os.readlink(entry / "exe"))
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"pid": int(entry.name), "state": fields[0], "ppid": int(fields[1]),
            "pgid": int(fields[2]), "sid": int(fields[3]), "start": int(fields[19]),
            "cli": cli, "binary_sha256": digest, "flags": env, "env_names": sorted(set(env_names)),
            "cli_launch": _safe_cli_flags((entry / "cmdline").read_bytes().split(b"\0")) if cli else None}


def snapshot(root: Path, identities: dict | None = None) -> dict:
    identities = {int(pid): int(start) for pid, start in (identities or {}).items()}
    vault = Path("/workspace/runtime/git/obs-vault-active")
    hashes = {}
    for relative in (".claude/settings.json", ".claude/settings.local.json", ".mcp.json"):
        path = vault / relative
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    processes = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if not (int(entry.name) in identities or b"stream-json" in args or b"obs_agent.telegram_main" in args
                    or any(str(root).encode() in arg for arg in args)):
                continue
            processes.append(_process_record(entry, cli=b"stream-json" in args))
        except (OSError, ValueError, IndexError):
            continue
    checks = []
    for pid, expected in sorted(identities.items()):
        try:
            current = _process_record(Path("/proc") / str(pid), cli=False)
        except FileNotFoundError:
            checks.append({"pid": pid, "expected_start": expected, "outcome": "ABSENT"})
        except (OSError, ValueError, IndexError) as exc:
            checks.append({"pid": pid, "expected_start": expected, "outcome": "UNAVAILABLE", "error": type(exc).__name__})
        else:
            checks.append({"pid": pid, "expected_start": expected, "observed_start": current["start"],
                           "state": current["state"], "outcome": "SAME_IDENTITY" if current["start"] == expected else "REUSED_PID"})
    return {"ts": time.time(), "settings_mcp_sha256": hashes,
            "lane_flags": {key: os.environ[key] for key in FLAGS if key in os.environ},
            "processes": processes, "identity_checks": checks,
            "process_snapshot_scope": "filtered catalogue plus direct stat for every requested PID/start; Z/X retained even with empty cmdline or missing exe",
            "tool_started": (root / "tool-started").exists()}


def setup(root: Path, shape: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "release.fifo").exists():
        os.mkfifo(root / "release.fifo", 0o600)
    vault = Path("/workspace/runtime/git/obs-vault-active")
    helper_path = vault / "evc_fixture.py"
    helper_path.write_bytes(Path(__file__).read_bytes())
    helper = str(helper_path)
    if shape in {"S4", "S5h"}:
        settings_path = vault / ".claude/settings.json"
        settings = json.loads(settings_path.read_text())
        import shlex
        command = shlex.join([sys.executable, helper, "hook", "--root", str(root)])
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append({"matcher": "Bash", "hooks": [{"type": "command", "command": command}]})
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    if shape == "S5m":
        mcp_path = vault / ".mcp.json"
        mcp = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
        mcp.setdefault("mcpServers", {})["evc-hold"] = {"command": sys.executable,
            "args": [helper, "mcp", "--root", str(root)]}
        mcp_path.write_text(json.dumps(mcp, indent=2) + "\n")
        settings_path = vault / ".claude/settings.json"
        settings = json.loads(settings_path.read_text())
        settings["enableAllProjectMcpServers"] = True
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return snapshot(root)


def zombie_control(root: Path) -> None:
    child = os.fork()
    if child == 0:
        os._exit(0)
    try:
        os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
        identity = _process_record(Path("/proc") / str(child), cli=False)
        record(root, "zombie_control", child_pid=child, child_start=identity["start"], child_state=identity["state"])
        hold(root, "zombie_parent_holding")
    finally:
        os.waitpid(child, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hold", "hook", "mcp", "snapshot", "setup", "release", "zombie"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shape", default="S2")
    parser.add_argument("--identities", type=json.loads, default={})
    args = parser.parse_args()
    if args.mode == "hold":
        (args.root / "tool-started").touch()
        print(hold(args.root, "tool_running"))
    elif args.mode == "hook":
        payload = json.load(sys.stdin)
        hold(args.root, "hook_running")
        print(json.dumps({"continue": True}))
    elif args.mode == "mcp":
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("evc-hold")
        @server.tool()
        def hold_probe() -> str:
            return hold(args.root, "mcp_running")
        server.run(transport="stdio")
    elif args.mode == "zombie":
        zombie_control(args.root)
    elif args.mode == "snapshot":
        print(json.dumps(snapshot(args.root, args.identities)))
    elif args.mode == "setup":
        print(json.dumps(setup(args.root, args.shape)))
    elif args.mode == "release":
        descriptor = os.open(args.root / "release.fifo", os.O_RDWR | os.O_NONBLOCK)
        os.write(descriptor, b"release\n")
        os.close(descriptor)
        print("released")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
