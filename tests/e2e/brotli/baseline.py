#!/usr/bin/env python3
"""Benign exact-CLI decoder baseline, exclusively inside an obs-e2e container."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIN = "7a4a653982b07e0a8157f8d3b2c2f8e442520ab07b2fa2e692ba054dbba210c9"
CLI = Path("/workspace/obs/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude")
MARKER = "BUN_WEBFETCH_OK"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compress(data):
    return subprocess.run(["node", "-e", "process.stdout.write(require('node:zlib').brotliCompressSync(require('node:fs').readFileSync(0)))"], input=data, capture_output=True, check=True).stdout


def sse_message(block):
    stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
    rows = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_benign_fixture", "type": "message", "role": "assistant", "content": [], "model": "claude-haiku-4-5-20251001", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {**block, **({"input": {}} if block["type"] == "tool_use" else {"text": ""})}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 10}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in rows).encode()


class Fixture:
    def __init__(self, directory, path, encoding):
        self.directory, self.path, self.encoding = directory, path, encoding
        self.events = []
        self.lock = threading.Lock()
        self.server = None
        self.node_version = subprocess.check_output(["node", "--version"], text=True).strip()

    def record(self, event):
        with self.lock:
            self.events.append({"at_ns": time.time_ns(), **event})

    def start(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, body, path, content_type="application/json", status=200, force_br=False):
                encoded = force_br or (fixture.path == path and fixture.encoding == "br")
                wire = compress(body) if encoded else body
                fixture.record({"kind": "response", "path": path, "method": self.command, "status": status, "accept_encoding": self.headers.get("accept-encoding"), "encoding": "br" if encoded else "identity", "content_type": content_type, "wire_bytes": len(wire), "wire_sha256": digest(wire), "decoded_bytes": len(body), "decoded_sha256": digest(body)})
                self.send_response(status)
                self.send_header("content-type", content_type)
                if encoded:
                    self.send_header("content-encoding", "br")
                self.send_header("content-length", str(len(wire)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(wire)
                self.wfile.flush()

            def do_GET(self):
                path = self.path.split("?", 1)[0].strip("/")
                if path == "control":
                    self.reply(b"BUN_NATIVE_FETCH_CONTROL\n", path, "text/plain", force_br=True)
                elif path == "direct":
                    self.reply(b'{"can_fetch":true}', path)
                elif path == "webfetch":
                    self.reply((MARKER + "\nBenign native WebFetch content.\n").encode(), path, "text/markdown")
                elif path == "mcp":
                    self.reply(b"", path, status=405)
                else:
                    self.reply(b"{}", "bootstrap")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                data = json.loads(body or b"{}")
                path = self.path.split("?", 1)[0].strip("/")
                if path == "mcp":
                    method = data.get("method")
                    fixture.record({"kind": "mcp_request", "method": method, "id": data.get("id")})
                    if method == "initialize":
                        result = {"protocolVersion": data["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "benign-bun-probe", "version": "1"}}
                    elif method == "tools/list":
                        result = {"tools": [{"name": "bun_probe", "description": "Return the benign transport marker", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
                    elif method == "tools/call":
                        result = {"content": [{"type": "text", "text": "BUN_MCP_OK"}], "isError": False}
                    else:
                        self.reply(b"", path, status=202)
                        return
                    response = {"jsonrpc": "2.0", "id": data["id"], "result": result}
                    wire = ("event: message\ndata: " + json.dumps(response) + "\n\n").encode()
                    self.reply(wire, path, "text/event-stream")
                elif path.startswith("model") or path.startswith("v1/messages"):
                    tools = data.get("tools", [])
                    names = [x.get("name") for x in tools]
                    messages = data.get("messages", [])
                    results = [b for m in messages for b in (m.get("content") if isinstance(m.get("content"), list) else []) if isinstance(b, dict) and b.get("type") == "tool_result"]
                    fixture.record({"kind": "model_request", "model": data.get("model"), "tools": names, "tools_sha256": digest(json.dumps(tools, sort_keys=True).encode()), "roles": [m.get("role") for m in messages], "tool_results": results})
                    mcp_name = next((x for x in names if x and x.endswith("__bun_probe")), None)
                    if not results and fixture.path == "mcp" and mcp_name:
                        block = {"type": "tool_use", "id": "tool_bun_mcp", "name": mcp_name, "input": {}}
                    elif not results and fixture.path != "mcp" and "WebFetch" in names:
                        block = {"type": "tool_use", "id": "tool_bun_web", "name": "WebFetch", "input": {"url": f"https://fixture.obs.test:{fixture.server.server_port}/page", "prompt": "Return the exact benign marker in the fetched content."}}
                    else:
                        block = {"type": "text", "text": "BUN_MCP_OK" if fixture.path == "mcp" else MARKER}
                    self.reply(sse_message(block), "model", "text/event-stream")
                else:
                    self.reply(b"{}", "bootstrap")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cert, key = self.directory / "cert.pem", self.directory / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture.obs.test", "-addext", "subjectAltName=DNS:fixture.obs.test,IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return cert


async def run_cell(args):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    if digest(CLI.read_bytes()) != PIN:
        raise RuntimeError("Exact pinned CLI hash mismatch")
    output = Path(args.out) / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="bun-baseline-") as tmp:
        root = Path(tmp)
        fixture = Fixture(root, args.path, args.encoding)
        cert = fixture.start()
        trace = output / "trace.jsonl"
        env = {
            "HOME": str(root / "home"), "CLAUDE_CONFIG_DIR": str(root / "home" / ".claude"),
            "BUN_OPTIONS": f"--preload {Path(__file__).with_name('trace_preload.cjs')}",
            "BUN_PROBE_TRACE": str(trace), "BUN_PROBE_PORT": str(fixture.server.server_port),
            "NODE_EXTRA_CA_CERTS": str(cert), "ANTHROPIC_BASE_URL": f"https://127.0.0.1:{fixture.server.server_port}/model",
            "ANTHROPIC_API_KEY": "benign-model-fixture-not-a-credential",
            "ENABLE_TOOL_SEARCH": "false", "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_EAGER_FLUSH": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        }
        (root / "home").mkdir()
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps({"env": {"ENABLE_TOOL_SEARCH": "false"}}))
        servers = {"exa": {"type": "http", "url": "https://mcp.exa.ai/mcp"}} if args.path == "mcp" else {}
        messages, errors = [], []
        options = ClaudeAgentOptions(cli_path=CLI, cwd=root, model="claude-haiku-4-5-20251001", system_prompt={"type": "preset", "preset": "claude_code"}, permission_mode="bypassPermissions", setting_sources=["project"], mcp_servers=servers, extra_args={"strict-mcp-config": None}, env=env, max_turns=4, stderr=errors.append)
        started = time.time()
        error = None
        try:
            async with asyncio.timeout(90):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(f"Benign transport baseline {args.run_id}: execute the fixture tool and report its marker.")
                    async for message in client.receive_response():
                        messages.append({"class": type(message).__name__, "value": dataclasses.asdict(message)})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            fixture.server.shutdown()
            fixture.server.server_close()
        (output / "server-events.json").write_text(json.dumps(fixture.events, indent=2))
        (output / "cli-events.json").write_text(json.dumps(messages, indent=2, default=str))
        (output / "cli-stderr.txt").write_text("\n".join(errors))
        result = {"run_id": args.run_id, "path": args.path, "encoding": args.encoding, "cli_sha256": PIN, "node_encoder": fixture.node_version, "fixture_program_sha256": digest(Path(__file__).read_bytes()), "trace_program_sha256": digest(Path(__file__).with_name("trace_preload.cjs").read_bytes()), "seconds": time.time()-started, "error": error, "events": len(messages), "network_events": len(fixture.events), "env_names": sorted(env), "no_production_credentials": True, "configuration_difference": "test-only model/TLS/route instrumentation; production nonessential-traffic setting remains unset; project-only settings, eager flush, background/git/ClaudeAI/tool-search controls reproduced"}
        (output / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
        if error:
            return 1
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--path", choices=["mcp", "webfetch", "direct"], required=True)
    parser.add_argument("--encoding", choices=["identity", "br"], required=True)
    parser.add_argument("--out", default="/evidence")
    args = parser.parse_args()
    return asyncio.run(run_cell(args))


if __name__ == "__main__":
    sys.exit(main())
