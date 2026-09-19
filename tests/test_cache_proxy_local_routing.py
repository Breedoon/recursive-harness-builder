"""Unit tests for cache-proxy per-model upstream routing (local-* support).

Covers the Step-2 fix that makes ALL Claude Code traffic — including
``local-*`` models — flow through the cache proxy, which then forwards each
request to the upstream its model selects.

No network: the "upstreams" are stub HTTP servers on loopback. No GPU, no gate,
no Anthropic call.

Properties under test:
  1. upstream selection by model name;
  2. per-upstream auth handling (the local gate credential must arrive
     untouched; the CLIProxyAPI key swap must still happen and must NOT leak
     onto other upstreams);
  3. no-fallback on a local misconfiguration ("fail local");
  4. hosted request bodies are byte-identical to the pre-fix behaviour;
  5. the usage log distinguishes local turns from hosted ones.
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_stats():
    for key in cache_proxy.stats:
        cache_proxy.stats[key] = 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Recorder:
    """Captures what a stub upstream received."""

    def __init__(self):
        self.requests: list[dict] = []


def _make_stub_upstream(recorder: _Recorder, *, stream: bool = False):
    """Start a stub Anthropic-shaped upstream. Returns (base_url, shutdown)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length)
            recorder.requests.append({
                "path": self.path,
                "raw_body": raw,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            if stream:
                payload = (
                    b'event: message_start\n'
                    b'data: {"type":"message_start","message":{"usage":'
                    b'{"input_tokens":5,"cache_read_input_tokens":800,'
                    b'"cache_creation_input_tokens":0}}}\n\n'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(payload)
            else:
                body = json.dumps({
                    "type": "message",
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 800,
                        "cache_creation_input_tokens": 0,
                    },
                }).encode()
                self.send_response(200)
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
    """Run ProxyHandler in-process on a free port. Yields the base URL."""
    port = _free_port()
    server = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _body(model: str, *, stream: bool = False) -> dict:
    return {
        "model": model,
        "stream": stream,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=9.9.9.z; cch=1;"},
            {"type": "text", "text": "second block"},
            {"type": "text", "text": "third block\ngitStatus: dirty stuff here"},
        ],
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {"name": "Zeta", "input_schema": {"type": "object",
                                              "properties": {"a": {"type": "array"}}}},
            {"name": "Alpha", "input_schema": {"type": "object"}},
        ],
        "metadata": {"user_id": "u_session_deadbeef-1111"},
    }


# ── 1. Upstream selection by model ────────────────────────────────────────


class TestLocalUpstreamSelection:
    def test_local_model_routes_to_local_upstream(self, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream("local-qwen3.8-27b") == "http://gate:8080"

    def test_local_shorthand_routes_to_local_upstream(self, monkeypatch):
        # "qwen" resolves to local-qwen3.8-27b via OBS model resolution.
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream("qwen") == "http://gate:8080"
        assert cache_proxy._resolve_upstream("local-qwen") == "http://gate:8080"

    def test_local_with_context_suffix_routes_to_local_upstream(self, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream("local-gemma4-31b[48k]") == "http://gate:8080"

    def test_local_prefix_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream("LOCAL-Qwen3.8-27B") == "http://gate:8080"

    def test_is_local_model(self, monkeypatch):
        assert cache_proxy._is_local_model("local-qwen3.8-27b") is True
        assert cache_proxy._is_local_model("qwen") is True
        assert cache_proxy._is_local_model("claude-opus-4-6") is False
        assert cache_proxy._is_local_model("gpt-5.5") is False
        assert cache_proxy._is_local_model("") is False
        # "localish" must not be mistaken for the local- prefix
        assert cache_proxy._is_local_model("localish-model") is False

    def test_route_label(self):
        assert cache_proxy._route_label("http://gate:8080", True) == "local"
        assert cache_proxy._route_label(cache_proxy.ANTHROPIC_UPSTREAM, False) == "anthropic"
        assert cache_proxy._route_label(cache_proxy.CLI_PROXY_UPSTREAM, False) == "cli-proxy"


class TestHostedRoutingUnchanged:
    """The pre-existing routing answers must not move."""

    @pytest.mark.parametrize("model", [
        "claude-opus-4-6", "claude-opus-4-6[1m]", "claude", "sonnet", "haiku[1m]",
    ])
    def test_claude_still_routes_to_anthropic(self, model, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream(model) == cache_proxy.ANTHROPIC_UPSTREAM

    @pytest.mark.parametrize("model", [
        "gpt-5.5", "gemini-3.1-flash-lite-preview[1m]", "", "deepseek-v4",
    ])
    def test_non_claude_still_routes_to_cli_proxy(self, model, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "http://gate:8080")
        assert cache_proxy._resolve_upstream(model) == cache_proxy.CLI_PROXY_UPSTREAM


# ── 2. Auth handling per upstream ─────────────────────────────────────────


class TestAuthPerUpstream:
    def test_local_upstream_receives_client_credentials_untouched(
        self, proxy_server, monkeypatch
    ):
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            resp = httpx.post(
                proxy_server + "/v1/messages",
                json=_body("local-qwen3.8-27b"),
                headers={
                    "Authorization": "Bearer gate-token-abc",
                    "x-api-key": "client-supplied-key",
                    "anthropic-version": "2023-06-01",
                },
                timeout=10,
            )
        finally:
            shutdown()
        assert resp.status_code == 200
        assert len(rec.requests) == 1
        got = rec.requests[0]["headers"]
        # The gate credential must arrive exactly as the client sent it.
        assert got["authorization"] == "Bearer gate-token-abc"
        # and the CLIProxyAPI key swap must NOT have fired on this upstream.
        assert got["x-api-key"] == "client-supplied-key"
        assert got["x-api-key"] != cache_proxy.CLI_PROXY_API_KEY
        assert got["anthropic-version"] == "2023-06-01"

    def test_cli_proxy_upstream_still_gets_its_key_swapped(
        self, proxy_server, monkeypatch
    ):
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "CLI_PROXY_UPSTREAM", url)
        try:
            resp = httpx.post(
                proxy_server + "/v1/messages",
                json=_body("gpt-5.5"),
                headers={"x-api-key": "client-supplied-key"},
                timeout=10,
            )
        finally:
            shutdown()
        assert resp.status_code == 200
        assert rec.requests[0]["headers"]["x-api-key"] == cache_proxy.CLI_PROXY_API_KEY


# ── 3. Fail local: never fall back to a hosted upstream ───────────────────


class TestNoHostedFallbackForLocal:
    def test_unconfigured_local_upstream_returns_502(self, proxy_server, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "")
        resp = httpx.post(
            proxy_server + "/v1/messages",
            json=_body("local-qwen3.8-27b"),
            timeout=10,
        )
        assert resp.status_code == 502
        assert cache_proxy.stats["local_unconfigured"] == 1
        # Crucially: it did not get counted as hosted traffic.
        assert cache_proxy.stats["routed_anthropic"] == 0
        assert cache_proxy.stats["routed_cli_proxy"] == 0

    def test_resolve_upstream_returns_empty_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "")
        assert cache_proxy._resolve_upstream("local-qwen3.8-27b") == ""

    def test_local_upstream_error_surfaces_and_does_not_retry_hosted(
        self, proxy_server, monkeypatch
    ):
        # Point the local upstream at a closed port: the failure must surface
        # as a 502, not become an Anthropic call.
        dead = f"http://127.0.0.1:{_free_port()}"
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", dead)
        anthropic_rec = _Recorder()
        a_url, a_shutdown = _make_stub_upstream(anthropic_rec)
        monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", a_url)
        try:
            resp = httpx.post(
                proxy_server + "/v1/messages",
                json=_body("local-qwen3.8-27b"),
                timeout=30,
            )
        finally:
            a_shutdown()
        assert resp.status_code == 502
        assert anthropic_rec.requests == []

    def test_unparseable_local_body_is_not_sent_to_anthropic(
        self, proxy_server, monkeypatch
    ):
        """A malformed body still carries the gate credential — don't leak it."""
        local_rec = _Recorder()
        l_url, l_shutdown = _make_stub_upstream(local_rec)
        anthropic_rec = _Recorder()
        a_url, a_shutdown = _make_stub_upstream(anthropic_rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", l_url)
        monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", a_url)
        try:
            resp = httpx.post(
                proxy_server + "/v1/messages",
                content=b'{"model": "local-qwen3.8-27b", TRUNCATED',
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer gate-token-abc"},
                timeout=10,
            )
        finally:
            l_shutdown()
            a_shutdown()
        assert resp.status_code == 200
        assert anthropic_rec.requests == []
        assert len(local_rec.requests) == 1
        assert local_rec.requests[0]["headers"]["authorization"] == "Bearer gate-token-abc"

    def test_unparseable_hosted_body_still_passes_through_to_anthropic(
        self, proxy_server, monkeypatch
    ):
        """Pre-existing behaviour for hosted traffic must not change."""
        anthropic_rec = _Recorder()
        a_url, a_shutdown = _make_stub_upstream(anthropic_rec)
        monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", a_url)
        raw = b'{"model": "claude-opus-4-6", TRUNCATED'
        try:
            resp = httpx.post(
                proxy_server + "/v1/messages",
                content=raw,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        finally:
            a_shutdown()
        assert resp.status_code == 200
        assert len(anthropic_rec.requests) == 1
        assert anthropic_rec.requests[0]["raw_body"] == raw


# ── 4. Hosted request bodies unchanged (golden equality) ──────────────────


class TestHostedBodyUnchanged:
    def test_normalized_hosted_body_matches_golden(self, proxy_server, monkeypatch):
        """The bytes Anthropic receives must equal normalize_request's output.

        This is the regression guard for "hosted behaviour is byte-identical":
        the routing change must not alter what hosted upstreams see.
        """
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", url)
        payload = _body("claude-opus-4-6")
        expected, _ = cache_proxy.normalize_request(json.loads(json.dumps(payload)))
        expected_bytes = json.dumps(expected, separators=(",", ":")).encode()
        try:
            httpx.post(proxy_server + "/v1/messages", json=payload, timeout=10)
        finally:
            shutdown()
        assert rec.requests[0]["raw_body"] == expected_bytes

    def test_local_body_gets_the_same_normalizations(self, proxy_server, monkeypatch):
        """The whole point: local traffic now receives the cache normalizations."""
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("local-qwen3.8-27b"), timeout=10)
        finally:
            shutdown()
        sent = json.loads(rec.requests[0]["raw_body"])
        assert sent["system"][0]["text"] == cache_proxy.FIXED_BILLING_HEADER
        assert sent["system"][2]["text"].endswith("gitStatus: normalized")
        assert [t["name"] for t in sent["tools"]] == ["Alpha", "Zeta"]
        assert sent["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
        assert sent["metadata"]["user_id"] == "u_session_0"

    def test_local_model_name_is_not_rewritten(self, proxy_server, monkeypatch):
        """llama-swap matches the canonical local-* id literally."""
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("local-qwen3.8-27b"), timeout=10)
        finally:
            shutdown()
        assert json.loads(rec.requests[0]["raw_body"])["model"] == "local-qwen3.8-27b"

    def test_local_tool_schemas_are_not_openai_sanitized(self, proxy_server, monkeypatch):
        """Local kept its direct-to-gate schema behaviour; only CLIProxyAPI is sanitized."""
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("local-qwen3.8-27b"), timeout=10)
        finally:
            shutdown()
        sent = json.loads(rec.requests[0]["raw_body"])
        zeta = [t for t in sent["tools"] if t["name"] == "Zeta"][0]
        assert "items" not in zeta["input_schema"]["properties"]["a"]
        assert cache_proxy.stats["schemas_sanitized"] == 0

    def test_cli_proxy_tool_schemas_are_still_sanitized(self, proxy_server, monkeypatch):
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "CLI_PROXY_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages", json=_body("gpt-5.5"), timeout=10)
        finally:
            shutdown()
        sent = json.loads(rec.requests[0]["raw_body"])
        zeta = [t for t in sent["tools"] if t["name"] == "Zeta"][0]
        assert zeta["input_schema"]["properties"]["a"]["items"] == {}
        assert cache_proxy.stats["schemas_sanitized"] == 1


# ── 5. Usage log distinguishes local from hosted ──────────────────────────


class TestUsageLogCarriesRoute:
    def test_local_turn_is_logged_with_route_and_model(
        self, proxy_server, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("local-qwen3.8-27b"), timeout=10)
        finally:
            shutdown()
        entries = [json.loads(line) for line in
                   (tmp_path / "usage.jsonl").read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["route"] == "local"
        assert entries[0]["model"] == "local-qwen3.8-27b"
        assert entries[0]["cache_read"] == 800
        assert cache_proxy.stats["routed_local"] == 1

    def test_streaming_local_turn_is_logged(self, proxy_server, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec, stream=True)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("local-qwen3.8-27b", stream=True), timeout=10)
        finally:
            shutdown()
        entries = [json.loads(line) for line in
                   (tmp_path / "usage.jsonl").read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["route"] == "local"
        assert entries[0]["cache_read"] == 800

    def test_hosted_turn_is_logged_with_anthropic_route(
        self, proxy_server, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
        rec = _Recorder()
        url, shutdown = _make_stub_upstream(rec)
        monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", url)
        try:
            httpx.post(proxy_server + "/v1/messages",
                       json=_body("claude-opus-4-6"), timeout=10)
        finally:
            shutdown()
        entry = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
        assert entry["route"] == "anthropic"
        assert entry["model"] == "claude-opus-4-6"


# ── 6. /health stays local and upstream-independent ───────────────────────


def test_health_does_not_depend_on_any_upstream(proxy_server, monkeypatch):
    """The prod wrapper SIGTERMs the daemon after 3 failed /health polls."""
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "")
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM",
                        f"http://127.0.0.1:{_free_port()}")
    resp = httpx.get(proxy_server + "/health", timeout=10)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
