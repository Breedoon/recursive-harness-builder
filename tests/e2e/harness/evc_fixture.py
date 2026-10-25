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
         "EAGER_FLUSH", "CLAUDE_CODE_ENABLE_EAGER_FLUSH", "DISABLE_AUTO_COMPACT")


def record(root: Path, kind: str, **fields) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "signals.jsonl").open("a") as handle:
        handle.write(json.dumps({"kind": kind, "ts": time.time(), "pid": os.getpid(),
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
        readable, _, _ = select.select([descriptor], [], [], 180)
        if not readable:
            raise TimeoutError("EV-C bounded shape fixture was never released")
        os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    return "EV-C fixture released"


def snapshot(root: Path) -> dict:
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
            if not (b"stream-json" in args or b"obs_agent.telegram_main" in args or any(str(root).encode() in arg for arg in args)):
                continue
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            env = {}
            for item in (entry / "environ").read_bytes().split(b"\0"):
                name, _, value = item.partition(b"=")
                if name.decode(errors="replace") in FLAGS:
                    env[name.decode()] = value.decode(errors="replace")
            binary = Path(os.readlink(entry / "exe"))
            processes.append({"pid": int(entry.name), "state": fields[0], "ppid": int(fields[1]),
                              "pgid": int(fields[2]), "sid": int(fields[3]), "start": int(fields[19]),
                              "cli": b"stream-json" in args, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                              "flags": env})
        except (OSError, ValueError, IndexError):
            continue
    return {"ts": time.time(), "settings_mcp_sha256": hashes,
            "lane_flags": {key: os.environ[key] for key in FLAGS if key in os.environ},
            "processes": processes, "tool_started": (root / "tool-started").exists()}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hold", "hook", "mcp", "snapshot", "setup", "release"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shape", default="S2")
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
    elif args.mode == "snapshot":
        print(json.dumps(snapshot(args.root)))
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
