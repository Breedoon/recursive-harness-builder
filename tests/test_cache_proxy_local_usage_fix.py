"""Local-route SSE usage correction (vLLM start/delta + CLI >0 merge double count).

The bundled CLI 2.1.59 merges stream usage per field only when the new value
is > 0. vLLM puts the whole prompt S in message_start and the split in
message_delta; a delta with input_tokens == 0 therefore leaves S in place and
the CLI records S + cr + cc = 2S. These tests pin the proxy-side fix and prove
hosted streams are untouched byte for byte.
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
from cache_proxy import LocalUsageFixer


def cli_merge(start: dict, delta: dict) -> dict:
    """Mirror of the CLI 2.1.59 ``AEH`` usage merge (only > 0 overwrites)."""
    out = dict(start)
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = delta.get(k)
        if v is not None and v > 0:
            out[k] = v
    return out


def total(u: dict) -> int:
    return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0))


def sse(start_usage: dict, delta_usage: dict) -> bytes:
    start = {"type": "message_start", "message": {"id": "x", "type": "message",
             "role": "assistant", "content": [], "model": "local-qwen3.8-27b",
             "usage": start_usage}}
    delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": delta_usage}
    return (b"event: message_start\ndata: " + json.dumps(start).encode() + b"\n\n"
            b"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,"
            b"\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
            b"event: message_delta\ndata: " + json.dumps(delta).encode() + b"\n\n"
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")


def events(raw: bytes) -> dict:
    out = {}
    for line in raw.decode().split("\n"):
        if line.startswith("data: "):
            obj = json.loads(line[6:])
            out[obj["type"]] = obj
    return out


def run_fixer(raw: bytes, chunk: int) -> tuple[bytes, int]:
    f = LocalUsageFixer()
    out = b"".join(f.feed(raw[i:i + chunk]) for i in range(0, len(raw), chunk)) + f.flush()
    return out, f.rewrites


# Real shapes captured from the gate (2026-09-25) and from the prod proxy log.
S = 182144
CASES = [
    # (delta usage, expected rewrite)
    ({"input_tokens": 0, "output_tokens": 9, "cache_read_input_tokens": 56800,
      "cache_creation_input_tokens": 125344}, True),                      # session 0f8df623 04:03Z
    ({"input_tokens": 0, "output_tokens": 9, "cache_read_input_tokens": S,
      "cache_creation_input_tokens": 0}, True),                           # all cache-read
    ({"input_tokens": 10, "output_tokens": 16, "cache_read_input_tokens": S - 10,
      "cache_creation_input_tokens": 0}, False),                          # normal warm turn
    ({"input_tokens": S, "output_tokens": 16, "cache_read_input_tokens": 0,
      "cache_creation_input_tokens": 0}, False),                          # cold turn
]


@pytest.mark.parametrize("delta,expect_rewrite", CASES)
@pytest.mark.parametrize("chunk", [1, 7, 64, 100000])
def test_cli_visible_total_equals_prompt(delta, expect_rewrite, chunk):
    raw = sse({"input_tokens": S, "output_tokens": 0}, delta)
    before = cli_merge({"input_tokens": S}, delta)
    out, rewrites = run_fixer(raw, chunk)
    ev = events(out)
    after = cli_merge(ev["message_start"]["message"]["usage"], ev["message_delta"]["usage"])
    assert total(after) == S
    assert rewrites == (1 if expect_rewrite else 0)
    if expect_rewrite:
        assert total(before) == 2 * S  # the bug this fixes
    else:
        assert out == raw  # untouched bytes


def test_only_delta_usage_changes_and_other_events_identical():
    raw = sse({"input_tokens": S, "output_tokens": 0}, CASES[0][0])
    out, _ = run_fixer(raw, 13)
    a, b = raw.split(b"\n\n"), out.split(b"\n\n")
    assert len(a) == len(b)
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(diffs) == 1 and b"message_delta" in a[diffs[0]]
    d = events(out)["message_delta"]["usage"]
    assert d["input_tokens"] == 1 and d["cache_creation_input_tokens"] == 125343
    assert d["cache_read_input_tokens"] == 56800 and d["output_tokens"] == 9


def test_zero_prompt_or_malformed_is_left_alone():
    zero = sse({"input_tokens": 0}, {"input_tokens": 0, "output_tokens": 1,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0})
    assert run_fixer(zero, 5) == (zero, 0)
    bad = b"event: message_delta\ndata: {not json\n\n"
    assert run_fixer(bad, 3) == (bad, 0)
    partial = b"event: message_start\ndata: {\"type\":\"message_start\"}"  # no terminator
    assert run_fixer(partial, 4) == (partial, 0)


# ── Through the proxy handler ──────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub(payload: bytes):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(payload), 37):
                self.wfile.write(payload[i:i + 37])
                self.wfile.flush()

        def log_message(self, *a):
            pass

    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv.shutdown


@pytest.fixture
def proxy():
    for k in cache_proxy.stats:
        cache_proxy.stats[k] = 0
    port = _free_port()
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _post(proxy_url: str, model: str) -> bytes:
    body = {"model": model, "stream": True, "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}]}
    with httpx.Client(timeout=10) as c:
        with c.stream("POST", proxy_url + "/v1/messages", json=body,
                      headers={"x-api-key": "k", "anthropic-version": "2023-06-01"}) as r:
            return b"".join(r.iter_raw())


def test_local_route_corrected_through_proxy(proxy, monkeypatch, tmp_path):
    payload = sse({"input_tokens": S, "output_tokens": 0}, CASES[0][0])
    url, stop = _stub(payload)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", url)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "u.jsonl"))
    try:
        out = _post(proxy, "local-qwen3.8-27b")
    finally:
        stop()
    ev = events(out)
    assert total(cli_merge(ev["message_start"]["message"]["usage"],
                           ev["message_delta"]["usage"])) == S
    assert cache_proxy.stats["local_usage_rewrites"] == 1
    logged = json.loads((tmp_path / "u.jsonl").read_text().splitlines()[-1])
    assert logged["total"] == S and logged["route"] == "local"


@pytest.mark.parametrize("model,attr", [("claude-opus-4-6", "ANTHROPIC_UPSTREAM"),
                                        ("gpt-5.5", "CLI_PROXY_UPSTREAM")])
def test_hosted_streams_byte_identical(proxy, monkeypatch, tmp_path, model, attr):
    # Even a hosted stream with the exact bug-shaped delta must pass untouched.
    payload = sse({"input_tokens": S, "output_tokens": 0}, CASES[0][0])
    url, stop = _stub(payload)
    monkeypatch.setattr(cache_proxy, attr, url)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "u.jsonl"))
    try:
        out = _post(proxy, model)
    finally:
        stop()
    assert out == payload
    assert cache_proxy.stats["local_usage_rewrites"] == 0
