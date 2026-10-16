"""Local-route liveness (bead obs-3, 2026-10-16).

A local model can send nothing for minutes (cold 500k prefill, queueing, the
3090 gate on hold). The CLI drops a request after ~5 minutes without bytes, and
the proxy used to keep the abandoned request running upstream. These tests use
loopback stubs with sub-second timings; no model, no network.
"""

import json
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy

MESSAGE_START = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m",'
                 b'"type":"message","role":"assistant","content":[],"model":"x",'
                 b'"usage":{"input_tokens":5,"output_tokens":1}}}\n\n')
REST = (b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":2}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
NONSTREAM_BODY = json.dumps({"id": "m", "type": "message", "role": "assistant",
                             "content": [{"type": "text", "text": "hi"}],
                             "usage": {"input_tokens": 5, "output_tokens": 2}}).encode()


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch, tmp_path):
    for key in cache_proxy.stats:
        cache_proxy.stats[key] = 0
    monkeypatch.setattr(cache_proxy, "LOCAL_KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(cache_proxy, "LOCAL_HEADER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM_SILENCE_SECONDS", 30.0)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub:
    """Upstream whose behaviour is a function of the handler."""

    def __init__(self, behave):
        self.closed_at = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                behave(self, stub)

            def log_message(self, *a):
                pass

        self.port = _free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def wait_for_close(self, handler, limit: float):
        """Block until the proxy closes the connection; record when."""
        sock = handler.connection
        end = time.monotonic() + limit
        while time.monotonic() < end:
            readable, _, _ = select.select([sock], [], [], 0.05)
            if readable:
                try:
                    if sock.recv(1) == b"":
                        self.closed_at = time.monotonic()
                        return
                except OSError:
                    self.closed_at = time.monotonic()
                    return

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def proxy(monkeypatch):
    port = _free_port()
    server = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _body(stream: bool) -> dict:
    return {"model": "local-sparks-glm-5.3-flash-uncensored", "stream": stream,
            "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}


def _events(raw: bytes) -> list[str]:
    return [block.split(b"\n", 1)[0].removeprefix(b"event: ").decode()
            for block in raw.split(b"\n\n") if block.strip()]


def _sse_headers(h):
    h.send_response(200)
    h.send_header("content-type", "text/event-stream")
    h.end_headers()


def test_silence_after_message_start_is_filled_with_pings(proxy, monkeypatch):
    def behave(h, stub):
        _sse_headers(h)
        h.wfile.write(MESSAGE_START); h.wfile.flush()
        time.sleep(1.0)
        h.wfile.write(REST); h.wfile.flush()
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=_body(True), timeout=10)
    finally:
        stub.stop()
    events = _events(resp.content)
    assert resp.status_code == 200
    assert events[0] == "message_start" and events[-1] == "message_stop"
    assert events.count("ping") >= 3
    # Removing the pings gives back exactly what upstream sent.
    assert resp.content.replace(cache_proxy.SSE_PING, b"") == MESSAGE_START + REST
    assert cache_proxy.stats["local_keepalives"] >= 3


def test_slow_upstream_headers_get_early_200_and_pings(proxy, monkeypatch):
    def behave(h, stub):
        time.sleep(1.2)
        _sse_headers(h)
        h.wfile.write(MESSAGE_START + REST); h.wfile.flush()
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    first_byte = None
    t0 = time.monotonic()
    try:
        with httpx.stream("POST", f"{proxy}/v1/messages", json=_body(True), timeout=10) as resp:
            data = b""
            for chunk in resp.iter_raw():
                if first_byte is None:
                    first_byte = time.monotonic() - t0
                data += chunk
    finally:
        stub.stop()
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/event-stream"
    assert first_byte < 0.9
    assert data.replace(cache_proxy.SSE_PING, b"") == MESSAGE_START + REST
    assert cache_proxy.stats["local_early_commits"] == 1


def test_slow_nonstream_body_is_kept_alive_with_whitespace(proxy, monkeypatch):
    def behave(h, stub):
        time.sleep(1.2)
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("Content-Length", str(len(NONSTREAM_BODY)))
        h.end_headers()
        h.wfile.write(NONSTREAM_BODY)
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=_body(False), timeout=10)
    finally:
        stub.stop()
    assert resp.status_code == 200
    assert resp.content.startswith(b" ")
    assert resp.json() == json.loads(NONSTREAM_BODY)


def test_fast_nonstream_body_is_unchanged(proxy, monkeypatch):
    def behave(h, stub):
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("Content-Length", str(len(NONSTREAM_BODY)))
        h.end_headers()
        h.wfile.write(NONSTREAM_BODY)
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=_body(False), timeout=10)
    finally:
        stub.stop()
    assert resp.status_code == 200
    assert resp.content == NONSTREAM_BODY


def test_upstream_silence_limit_ends_with_retryable_error(proxy, monkeypatch):
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM_SILENCE_SECONDS", 1.0)

    def behave(h, stub):
        _sse_headers(h)
        h.wfile.write(MESSAGE_START); h.wfile.flush()
        stub.wait_for_close(h, 10)
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=_body(True), timeout=10)
    finally:
        stub.stop()
    events = _events(resp.content)
    assert events[0] == "message_start" and events[-1] == "error"
    err = json.loads(resp.content.split(b"event: error\ndata: ", 1)[1])
    assert err["error"]["type"] == "overloaded_error"
    assert cache_proxy.stats["local_upstream_silence"] == 1


@pytest.mark.parametrize("before_headers", [True, False])
def test_client_disconnect_closes_upstream_promptly(proxy, monkeypatch, before_headers):
    opened = threading.Event()

    def behave(h, stub):
        opened.set()
        if not before_headers:
            _sse_headers(h)
            h.wfile.write(MESSAGE_START); h.wfile.flush()
        stub.wait_for_close(h, 10)
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    host, port = proxy.removeprefix("http://").split(":")
    payload = json.dumps(_body(True)).encode()
    client = socket.create_connection((host, int(port)))
    client.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\n"
                   + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    assert opened.wait(5)
    client.recv(65536)
    time.sleep(0.5)  # let the early 200 / message_start arrive
    client.close()
    gone = time.monotonic()
    deadline = gone + 5
    while stub.closed_at is None and time.monotonic() < deadline:
        time.sleep(0.05)
    stub.stop()
    assert stub.closed_at is not None, "upstream request was left running"
    assert stub.closed_at - gone < 2.0
    assert cache_proxy.stats["local_client_gone"] == 1


def test_late_upstream_error_after_early_commit_becomes_sse_error(proxy, monkeypatch):
    other = b'{"type":"error","error":{"type":"overloaded_error","message":"busy"}}'

    def behave(h, stub):
        time.sleep(1.0)
        h.send_response(503)
        h.send_header("content-type", "application/json")
        h.send_header("Content-Length", str(len(other)))
        h.end_headers()
        h.wfile.write(other)
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=_body(True), timeout=10)
    finally:
        stub.stop()
    assert resp.status_code == 200
    assert _events(resp.content)[-1] == "error"
    err = json.loads(resp.content.split(b"event: error\ndata: ", 1)[1])
    assert err["error"] == {"type": "overloaded_error", "message": "busy"}


def test_hosted_route_is_untouched(proxy, monkeypatch):
    def behave(h, stub):
        _sse_headers(h)
        h.wfile.write(MESSAGE_START); h.wfile.flush()
        time.sleep(0.8)
        h.wfile.write(REST); h.wfile.flush()
    stub = Stub(behave)
    monkeypatch.setattr(cache_proxy, "CLI_PROXY_UPSTREAM", stub.url)
    body = dict(_body(True), model="gpt-5.5")
    try:
        resp = httpx.post(f"{proxy}/v1/messages", json=body, timeout=10)
    finally:
        stub.stop()
    assert resp.content == MESSAGE_START + REST
    assert cache_proxy.stats["local_keepalives"] == 0
