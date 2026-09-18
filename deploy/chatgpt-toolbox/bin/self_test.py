#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import urllib.request


def command_output(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)
    return result.stdout.strip().splitlines()[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9._-]{8,96}", args.nonce):
        raise SystemExit("nonce must be 8-96 safe characters")

    workspace = pathlib.Path(os.environ.get("TOOLBOX_WORKSPACE", "/workspace"))
    active_vault = pathlib.Path(
        os.environ.get("TOOLBOX_ACTIVE_VAULT", "/workspace/runtime/git/obs-vault-active")
    )
    probe_path = workspace / f"local-positive-{args.nonce}.txt"
    active_vault_probe_path = active_vault / f".obs-toolbox-active-vault-probe-{args.nonce}"
    payload = f"chatgpt-toolbox-local-positive:{args.nonce}\n"
    active_vault_payload = f"chatgpt-toolbox-active-vault-proof:{args.nonce}\n"
    probe_path.write_text(payload, encoding="utf-8")
    round_trip = probe_path.read_text(encoding="utf-8") == payload
    probe_path.unlink()
    active_vault_probe_path.write_text(active_vault_payload, encoding="utf-8")
    active_vault_round_trip = (
        active_vault_probe_path.read_text(encoding="utf-8") == active_vault_payload
    )
    active_vault_probe_path.unlink()
    active_vault_cleanup = not active_vault_probe_path.exists()

    result = {
        "schema": "chatgpt-toolbox-self-test-v1",
        "uid": os.getuid(),
        "gid": os.getgid(),
        "hostname": socket.gethostname(),
        "cwd": os.getcwd(),
        "nonce": args.nonce,
        "workspace_round_trip": round_trip,
        "active_vault": {
            "path": str(active_vault),
            "round_trip": active_vault_round_trip,
            "cleanup": active_vault_cleanup,
        },
        "tools": {
            "node": command_output("node", "--version"),
            "python": command_output("python3", "--version"),
            "curl": command_output("curl", "--version"),
            "git": command_output("git", "--version"),
            "ripgrep": command_output("rg", "--version"),
            "jq": command_output("jq", "--version"),
            "desktop_commander": command_output(
                "node",
                "-p",
                "require('/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/package.json').version",
            ),
        },
        "runtime_https": "not-requested",
    }

    if args.network:
        request = urllib.request.Request(
            "https://registry.npmjs.org/-/ping",
            headers={"User-Agent": "obs-chatgpt-toolbox-local-proof/0.1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read(1024)
            result["runtime_https"] = {
                "endpoint": "registry.npmjs.org/-/ping",
                "status": response.status,
            }

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if round_trip and active_vault_round_trip and active_vault_cleanup and os.getuid() != 0 else 1


if __name__ == "__main__":
    sys.exit(main())
