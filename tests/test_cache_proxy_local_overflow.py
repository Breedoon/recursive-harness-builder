"""Local-route overflow translation (vault-u3b.64).

vLLM rejects input + max_tokens > window; the local gate returns that as an
HTTP 500 internal_error. Claude Code 2.1.59 only recovers from Anthropic's
400 "input length and `max_tokens` exceed context limit: I + M > W" (it retries
with max_tokens = max(3000, W - I - 1000)). The proxy rewrites the local error
into that shape; hosted routes and unrelated errors pass through unchanged.

No network beyond loopback stubs.
"""

import json
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy

# Verbatim live error (nvidia 6h reassessment fork, session 4dd3e505, 2026-09-26T03:51:05Z).
VLLM_500 = json.dumps({
    "type": "error",
    "error": {
        "type": "internal_error",
        "message": (
            "This model's maximum context length is 262144 tokens. However, you "
            "requested 32000 output tokens and your prompt contains at least 230145 "
            "input tokens, for a total of at least 262145 tokens. Please reduce the "
            "length of the input prompt or the number of requested output tokens. "
            "(parameter=input_tokens, value=230145)"
        ),
    },
}).encode()

# The CLI's own parser (bundled 2.1.59, function OpI).
CLI_RE = re.compile(r"input length and `max_tokens` exceed context limit: (\d+) \+ (\d+) > (\d+)")


@pytest.fixture(autouse=True)
def reset_stats():
    for key in cache_proxy.stats:
        cache_proxy.stats[key] = 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub(status: int, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server.shutdown


@pytest.fixture
def proxy_server():
    port = _free_port()
    server = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _post(proxy: str, model: str, *, stream: bool) -> httpx.Response:
    body = {"model": model, "stream": stream, "max_tokens": 32000,
            "messages": [{"role": "user", "content": "hi"}]}
    return httpx.post(f"{proxy}/v1/messages", json=body,
                      headers={"authorization": "Bearer local-token"}, timeout=10)


def test_translation_matches_the_cli_parser():
    out = cache_proxy.translate_local_overflow_error(500, VLLM_500)
    data = json.loads(out)
    assert data["type"] == "error"
    match = CLI_RE.search(data["error"]["message"])
    assert match and tuple(int(g) for g in match.groups()) == (230145, 32000, 262144)


@pytest.mark.parametrize("status, body", [
    (200, VLLM_500),
    (500, b""),
    (500, b'{"type":"error","error":{"type":"internal_error","message":"boom"}}'),
    (400, b"not json at all"),
])
def test_other_responses_are_not_translated(status, body):
    assert cache_proxy.translate_local_overflow_error(status, body) is None


@pytest.mark.parametrize("stream", [True, False])
def test_local_overflow_becomes_cli_recoverable_400(proxy_server, monkeypatch, stream):
    url, shutdown = _stub(500, VLLM_500)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
    try:
        resp = _post(proxy_server, "local-qwen3.8-27b", stream=stream)
    finally:
        shutdown()
    assert resp.status_code == 400
    assert CLI_RE.search(resp.json()["error"]["message"])
    assert cache_proxy.stats["local_overflow_translations"] == 1


@pytest.mark.parametrize("stream", [True, False])
def test_other_local_errors_pass_through_unchanged(proxy_server, monkeypatch, stream):
    other = b'{"type":"error","error":{"type":"internal_error","message":"boom"}}'
    url, shutdown = _stub(500, other)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
    try:
        resp = _post(proxy_server, "local-qwen3.8-27b", stream=stream)
    finally:
        shutdown()
    assert resp.status_code == 500
    assert resp.content == other
    assert cache_proxy.stats["local_overflow_translations"] == 0


def test_hosted_route_is_never_translated(proxy_server, monkeypatch):
    url, shutdown = _stub(500, VLLM_500)
    monkeypatch.setattr(cache_proxy, "CLI_PROXY_UPSTREAM", url)
    try:
        resp = _post(proxy_server, "gpt-5.5", stream=True)
    finally:
        shutdown()
    assert resp.status_code == 500
    assert cache_proxy.stats["local_overflow_translations"] == 0
