"""Unit checks for the per-conversation breakpoint TTL pin (2026-10-20, bead vault-h6yt).

These pin the pure-function and handler contracts only. The live proof is
scripts/prefix_repro.py ttl_flip / ttl_flip_down (docs/cache-proxy-safety.md)
and, offline, a replay of the real request log through the pipeline.
"""
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import cache_proxy  # noqa: E402
import prefix_diff  # noqa: E402

T0 = 1_800_000_000.0


def _cc(ttl, scope=None):
    cc = {"type": "ephemeral"}
    if ttl == "1h":
        cc["ttl"] = "1h"
    if scope:
        cc["scope"] = scope
    return cc


def _body(ttl, n_turns=1, first="hello", tools=True, scope=None):
    msgs = [{"role": "user", "content": [{"type": "text", "text": first}]}]
    for i in range(1, n_turns):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"r{i}"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"q{i}"}]})
    msgs[-1]["content"][-1]["cache_control"] = _cc(ttl)
    body = {
        "model": "claude-opus-5-5",
        "system": [{"type": "text", "text": "x-anthropic-billing-header: v"},
                   {"type": "text", "text": "You are a Claude agent.", "cache_control": _cc(ttl, scope)},
                   {"type": "text", "text": "rest of system", "cache_control": _cc(ttl, scope)}],
        "messages": msgs,
    }
    if tools:
        body["tools"] = [{"name": "Bash", "input_schema": {"type": "object"}}]
    return body


def _ttls(body):
    return prefix_diff.breakpoint_ttls(body)


def _store(tmp_path=None):
    return cache_proxy.EffortPinStore(str(tmp_path / "pins.json") if tmp_path else None)


def test_once_1h_a_process_latched_at_5m_is_upgraded():
    # the 2026-10-20 08:05Z shape: a restarted CLI process sends 5m for a
    # conversation that ran at 1h; the next restart goes back to 1h
    store = _store()
    a = _body("1h", 3)
    assert cache_proxy.pin_cache_ttl(a, store, now=T0)["recorded"] is True
    b = _body("5m", 4)
    res = cache_proxy.pin_cache_ttl(b, store, now=T0 + 120)
    assert res == {"incoming": "5m", "effective": "1h", "applied": "upgraded", "recorded": False}
    assert _ttls(b) == ["1h"]
    c = _body("1h", 5)
    assert cache_proxy.pin_cache_ttl(c, store, now=T0 + 240)["applied"] is None
    assert _ttls(c) == ["1h"]


def test_upgrade_writes_the_cli_key_order_and_keeps_scope():
    store = _store()
    cache_proxy.pin_cache_ttl(_body("1h", 2), store, now=T0)
    b = _body("5m", 3, scope="global")
    cache_proxy.pin_cache_ttl(b, store, now=T0 + 1)
    assert json.dumps(b["system"][1]["cache_control"]) == '{"type": "ephemeral", "ttl": "1h", "scope": "global"}'
    assert b["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert json.dumps(_body("1h", 1, scope="global")["system"][1]["cache_control"]) == \
        json.dumps(b["system"][1]["cache_control"])


def test_warm_5m_conversation_is_kept_at_5m_when_the_cli_asks_for_1h():
    # a 1h request cannot read entries written with 5m; while they are warm,
    # keep 5m so the request reads them
    store = _store()
    cache_proxy.pin_cache_ttl(_body("5m", 2), store, now=T0)
    b = _body("1h", 3)
    res = cache_proxy.pin_cache_ttl(b, store, now=T0 + 118)
    assert res["applied"] == "kept-5m" and res["effective"] == "5m"
    assert _ttls(b) == ["5m"]
    assert "ttl" not in b["system"][1]["cache_control"]
    # still busy: still 5m (each request refreshes the 5-minute entries)
    c = _body("1h", 4)
    cache_proxy.pin_cache_ttl(c, store, now=T0 + 118 + 290)
    assert _ttls(c) == ["5m"]


def test_after_an_idle_gap_the_5m_conversation_moves_to_1h_and_stays():
    store = _store()
    cache_proxy.pin_cache_ttl(_body("5m", 2), store, now=T0)
    b = _body("1h", 3)
    res = cache_proxy.pin_cache_ttl(b, store, now=T0 + cache_proxy.TTL_PIN_WARM_SECONDS + 1)
    assert res["applied"] is None and _ttls(b) == ["1h"]
    c = _body("5m", 4)
    assert cache_proxy.pin_cache_ttl(c, store, now=T0 + 400)["applied"] == "upgraded"
    assert _ttls(c) == ["1h"]


def test_steady_5m_conversation_is_left_alone():
    store = _store()
    for i in range(1, 4):
        b = _body("5m", i)
        before = json.dumps(b)
        cache_proxy.pin_cache_ttl(b, store, now=T0 + i)
        assert json.dumps(b) == before


def test_pin_survives_a_proxy_restart(tmp_path):
    store = _store(tmp_path)
    cache_proxy.pin_cache_ttl(_body("1h", 2), store, now=T0)
    reloaded = _store(tmp_path)
    b = _body("5m", 3)
    assert cache_proxy.pin_cache_ttl(b, reloaded, now=T0 + 150)["applied"] == "upgraded"
    # a warm 5m conversation stays warm across a restart (last use is persisted)
    (tmp_path / "x").mkdir()
    s5 = _store(tmp_path / "x")
    cache_proxy.pin_cache_ttl(_body("5m", 2, first="other"), s5, now=T0)
    cache_proxy.pin_cache_ttl(_body("5m", 3, first="other"), s5, now=T0 + 59)  # not saved (< 60 s)
    again = _store(tmp_path / "x")
    c = _body("1h", 4, first="other")
    assert cache_proxy.pin_cache_ttl(c, again, now=T0 + 59 + 240)["applied"] == "kept-5m"


def test_conversations_are_pinned_separately_and_side_calls_untouched():
    store = _store()
    cache_proxy.pin_cache_ttl(_body("1h", 2, first="a"), store, now=T0)
    other = _body("5m", 2, first="b")
    cache_proxy.pin_cache_ttl(other, store, now=T0 + 1)
    assert _ttls(other) == ["5m"]
    side = _body("5m", 1, first="a", tools=False)
    before = json.dumps(side)
    assert cache_proxy.pin_cache_ttl(side, store, now=T0 + 2)["incoming"] is None
    assert json.dumps(side) == before


def test_prefix_diff_names_a_ttl_only_change():
    a, b = _body("5m", 2), _body("1h", 3)
    r = prefix_diff.diff_requests(a, b)
    assert r["classification"] == "ttl-changed"
    assert "breakpoint ttl 5m → 1h" in prefix_diff.format_result(r)
    assert prefix_diff.diff_requests(_body("1h", 2), _body("1h", 3))["classification"] == "append"


# ── through the real request handler ─────────────────────────────────────

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub(bodies):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            payload = json.dumps({"usage": {"input_tokens": 1}, "content": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", _free_port()), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown


def test_handler_sends_the_pinned_ttl_upstream(tmp_path, monkeypatch):
    logger = cache_proxy.RequestLogger(str(tmp_path / "requests"), max_bytes=10**9)
    monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", logger)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
    bodies = []
    up, stop = _stub(bodies)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", _free_port()), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
    try:
        for ttl, n in (("1h", 2), ("5m", 3), ("1h", 4)):
            body = _body(ttl, n)
            body["max_tokens"] = 5
            r = httpx.post(url, content=json.dumps(body).encode(), timeout=10,
                           headers={"anthropic-version": "2023-06-01", "content-type": "application/json"})
            assert r.status_code == 200
    finally:
        srv.shutdown()
        stop()
    assert [_ttls(json.loads(b)) for b in bodies] == [["1h"], ["1h"], ["1h"]]
    logger.flush()
    rows = [json.loads(line) for p in sorted((tmp_path / "requests").glob("*/index.jsonl"))
            for line in p.read_text().splitlines() if line.strip()]
    assert [row["norm"]["ttl_pin"]["applied"] for row in rows] == [None, "upgraded", None]
