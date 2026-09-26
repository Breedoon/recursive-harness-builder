"""Opt-in compatibility oracle for the *installed* Claude binary.

Run only in a loopback-only Linux network namespace. A tiny local fake Anthropic
API answers the request; there are no model-provider calls or real credentials.
The test reads the binary's own compaction threshold from its debug log. Missing
or changed diagnostic output is a failure, not a fabricated success.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OBS_RUN_LOCAL_CLAUDE_CONTEXT_TESTS") != "1",
    reason="Explicit opt-in and a loopback-only network namespace are required",
)


def _sse_response(model: str) -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_context_probe", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 500, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "OK"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {
            "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {kind}\ndata: {json.dumps(body)}\n\n" for kind, body in events).encode()


@contextmanager
def _fake_anthropic_api(*, capture_bodies: bool = False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2_000_000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length) or "{}")
            requests.append((self.path, body) if capture_bodies else self.path)
            if "count_tokens" in self.path:
                payload = b'{"input_tokens": 500}'
                content_type = "application/json"
            elif self.path.split("?")[0] == "/v1/messages":
                payload = _sse_response(body.get("model", "claude-opus-4-6"))
                content_type = "text/event-stream"
            else:
                payload = b"{}"
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def binary_workspace(tmp_path, monkeypatch):
    # Actual namespace isolation, not just a flag promising no provider traffic.
    interfaces = {name for _index, name in socket.if_nameindex()}
    assert interfaces <= {"lo"}, (
        "Native CLI probes require a loopback-only network namespace; "
        f"observed interfaces: {sorted(interfaces)}"
    )
    project = tmp_path / "project"
    home = tmp_path / "home"
    (project / ".claude").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    monkeypatch.setattr("obs_agent.session.create_obs_tools", lambda *args, **kwargs: {})
    monkeypatch.setattr("obs_agent.session.create_hook_matchers", lambda *args, **kwargs: {})
    for key in (
        "DISABLE_COMPACT", "DISABLE_AUTO_COMPACT", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    return project, home


@pytest.mark.parametrize("model, window, output_cap, expected, native_defaults, stale_project", [
    ("gpt-5.6-sol", 100_000, 32_000, 67_000, False, False),
    ("gpt-5.6-sol", 200_000, 32_000, 167_000, False, False),
    ("gpt-5.6-sol", 400_000, 32_000, 367_000, False, False),
    # vault-u3b.64: gpt-5.6-sol's real window is 900K (MODEL_CONTEXT_WINDOWS),
    # so a 1M budget is capped at 900K - 32K max_output - 13K buffer.
    ("gpt-5.6-sol", 1_000_000, 32_000, 855_000, False, False),
    ("claude-opus-4-6", 100_000, 32_000, 67_000, False, False),
    ("claude-opus-4-6", 400_000, 32_000, 367_000, False, False),
    ("gpt-5.6-sol", 128_000, 32_000, 95_000, False, False),
    ("gpt-5.6-sol", 64_000, 32_000, 31_000, False, False),
    ("gpt-5.6-sol", 400_000, 8_000, 367_000, False, False),
    ("gpt-5.6-sol", 35_000, 32_000, 2_000, False, False),
    ("gpt-5.6-sol", 333_000, 32_000, 300_000, False, False),
    # Independently verify native anchors without OBS's percentage/window.
    ("claude-opus-4-6", 200_000, 32_000, 167_000, True, False),
    ("claude-opus-4-6", 1_000_000, 32_000, 967_000, True, False),
    # A stale project-level override must not undo a fresh 400K session.
    ("gpt-5.6-sol", 400_000, 32_000, 367_000, False, True),
])
def test_binary_reports_the_requested_compaction_threshold(
    binary_workspace, monkeypatch, tmp_path, request, model, window, output_cap, expected,
    native_defaults, stale_project,
):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from obs_agent.config import OBSConfig
    from obs_agent.session import SessionManager

    project, home = binary_workspace
    if stale_project:
        (project / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "10",
        }}))
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(output_cap))
    manager = SessionManager(config=OBSConfig(
        vault_path=project, model=f"{model}[{window // 1000}k]", cache_proxy_enabled=False,
    ))
    options = manager.create_options()
    debug_log = tmp_path / "claude-debug.log"
    options.mcp_servers = {}
    options.hooks = {}
    options.tools = []
    options.permission_mode = "default"
    options.max_turns = 1
    options.system_prompt = "Answer the user briefly."
    options.extra_args = {"debug-file": str(debug_log)}
    # Use the pinned SDK's command builder and bundled-binary discovery. The
    # direct process launch below deliberately uses a clean, allowlisted env.
    if native_defaults:
        options.settings = json.dumps({"env": {}})
    context_env = json.loads(options.settings)["env"]
    transport = SubprocessCLITransport(prompt="unused", options=options)
    command = transport._build_command()

    with _fake_anthropic_api() as (base_url, requests):
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home), "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "sk-offline-context-probe",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_cap),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            **context_env,
        }
        user_message = {"type": "user", "session_id": "default", "parent_tool_use_id": None,
                        "message": {"role": "user", "content": "Reply with OK."}}
        try:
            result = subprocess.run(
                command, input=json.dumps(user_message) + "\n", cwd=project,
                env=child_env, text=True, capture_output=True, timeout=45,
            )
        except subprocess.TimeoutExpired as error:
            # Keep the binary's debug file and partial streams even on timeout.
            # subprocess.run has already killed and waited for its direct child.
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            result = subprocess.CompletedProcess(
                command, returncode=-1,
                stdout=stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
                stderr=(stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
                + "\nNative compatibility probe timed out after 45 seconds.",
            )
    log_text = debug_log.read_text(errors="replace") if debug_log.exists() else ""
    artifact_dir = os.environ.get("OBS_CONTEXT_TEST_ARTIFACT_DIR")
    if artifact_dir:
        destination = Path(artifact_dir)
        destination.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
        (destination / f"{name}.log").write_text(log_text)
        (destination / f"{name}.json").write_text(json.dumps({
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "context_env": context_env, "model": options.model, "requests": requests,
        }, indent=2))
    diagnostic = f"exit={result.returncode}\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}\n{log_text[-6000:]}"
    assert result.returncode == 0, diagnostic
    assert any(path.startswith("/v1/messages") for path in requests), diagnostic
    thresholds = [int(value) for value in re.findall(r"autocompact:.*?threshold=(\d+)", log_text)]
    assert thresholds, "No native threshold diagnostic; review CLI compatibility.\n" + diagnostic
    assert set(thresholds) == {expected}, diagnostic
