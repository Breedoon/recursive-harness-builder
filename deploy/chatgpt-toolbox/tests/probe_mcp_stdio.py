#!/usr/bin/env python3
import argparse
import json
import pathlib
import select
import subprocess
import time


CONTAINER = "chatgpt-toolbox"
SERVER = "/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/dist/index.js"


def send(process: subprocess.Popen[str], payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def receive_id(process: subprocess.Popen[str], message_id: int, timeout: float = 30.0) -> dict:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        line = process.stdout.readline()
        if not line:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == message_id:
            return payload
    raise TimeoutError(f"no MCP response for id {message_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default=CONTAINER)
    args = parser.parse_args()

    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            args.container,
            "node",
            SERVER,
            "--no-onboarding",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "obs-toolbox-local-proof", "version": "0.1.0"},
                },
            },
        )
        initialized = receive_id(process, 1)
        if "error" in initialized:
            raise RuntimeError("MCP initialize returned an error")

        send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_response = receive_id(process, 2)
        if "error" in tools_response:
            raise RuntimeError("MCP tools/list returned an error")

        tools = tools_response.get("result", {}).get("tools", [])
        tool_names = sorted(tool.get("name") for tool in tools if tool.get("name"))
        required = {"read_file", "write_file", "start_process"}
        result = {
            "schema": "chatgpt-toolbox-mcp-stdio-proof-v1",
            "server_name": initialized.get("result", {}).get("serverInfo", {}).get("name"),
            "server_version": initialized.get("result", {}).get("serverInfo", {}).get("version"),
            "protocol_version": initialized.get("result", {}).get("protocolVersion"),
            "tool_count": len(tool_names),
            "required_tools_present": sorted(required & set(tool_names)),
            "all_required_tools_present": required.issubset(tool_names),
            "initialized_notification_sent": False,
            "chrome_prefetch_triggered": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["all_required_tools_present"] else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
