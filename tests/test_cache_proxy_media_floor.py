"""Unit checks for batch eviction of old images past the CLI's media cap (bead obs-51).

The bundled CLI drops the oldest image/document beyond 100 on every request
(constant gFI=100, function LS9). `cli_evict` below is a line-by-line port of
that function, so these tests drive the proxy with the bodies the real CLI
would send. They pin the pure-function and handler contracts only; the live
proof is scripts/prefix_repro.py image_cap (docs/cache-proxy-safety.md) plus
the offline replay of the 2026-10-24 PDF-review session.
"""
import copy
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

REMINDER_TAG = "system" + "-reminder"  # built at runtime, never a literal in this file
T0 = 1_800_000_000.0


def _media(b):
    return isinstance(b, dict) and b.get("type") in ("image", "document")


def cli_evict(msgs, cap=100):
    """Port of the CLI 2.1.59 LS9(messages, 100) media cap."""
    count = 0
    for m in msgs:
        if not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if _media(b):
                count += 1
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                count += sum(1 for x in b["content"] if _media(x))
    excess = count - cap
    if excess <= 0:
        return msgs
    out = []
    for m in msgs:
        if excess <= 0 or not isinstance(m.get("content"), list):
            out.append(m)
            continue
        before = excess
        mapped = []
        for b in m["content"]:
            if excess <= 0 or b.get("type") != "tool_result" or not isinstance(b.get("content"), list):
                mapped.append(b)
                continue
            inner = []
            for x in b["content"]:
                if excess > 0 and _media(x):
                    excess -= 1
                    continue
                inner.append(x)
            mapped.append(b if len(inner) == len(b["content"]) else dict(b, content=inner))
        final = []
        for b in mapped:
            if excess > 0 and _media(b):
                excess -= 1
                continue
            final.append(b)
        out.append(m if before == excess else dict(m, content=final))
    return out


def _img(n):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": f"img{n:04d}"}}


def _history(turns, per_turn=6, start_text_turns=2):
    """Full JSONL-side history: a few text turns, then turns that each read
    `per_turn` images in parallel (one tool_result per image)."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "review the pages"}]}]
    n = 0
    for t in range(start_text_turns):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"thinking out loud {t}"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"go on {t}"}]})
    for t in range(turns):
        ids = [f"toolu_{t:03d}_{j}" for j in range(per_turn)]
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"reading batch {t}"}]
                     + [{"type": "tool_use", "id": i, "name": "Read", "input": {"file_path": f"/p/{i}.png"}}
                        for i in ids]})
        results = []
        for i in ids:
            results.append({"type": "tool_result", "tool_use_id": i, "content": [_img(n)]})
            n += 1
        msgs.append({"role": "user", "content": results})
    return msgs


def _request(history, *, live_reminder=False):
    """What the CLI sends: evicted history, the last block carrying the breakpoint."""
    msgs = copy.deepcopy(cli_evict(copy.deepcopy(history)))
    if live_reminder:
        # live form: the CLI splices a periodic reminder into the newest tool result
        last = msgs[-1]["content"][-1]
        if last.get("type") == "tool_result":
            last["content"] = list(last["content"]) + [
                {"type": "text", "text": f"<{REMINDER_TAG}>\nperiodic note\n</{REMINDER_TAG}>"}]
    msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return {
        "model": "claude-opus-5-5",
        "system": [{"type": "text", "text": "You are a Claude agent.",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "messages": msgs,
    }


def _proxy(body, store, now=T0):
    body, _ = cache_proxy.normalize_request(body)
    res = cache_proxy.pin_media_floor(body, store, now=now)
    return body, res


def _n_media(body):
    return len(cache_proxy._media_message_indices(body["messages"]))


def _cls(a, b):
    return prefix_diff.diff_requests(a, b)["classification"]


def _store(tmp_path=None):
    return cache_proxy.EffortPinStore(str(tmp_path / "pins.json") if tmp_path else None)


# ── the rule ─────────────────────────────────────────────────────────────

def test_below_the_cap_nothing_changes():
    store = _store()
    body = _request(_history(16))  # 96 images
    before = copy.deepcopy(cache_proxy.normalize_request(copy.deepcopy(body))[0])
    out, res = _proxy(body, store)
    assert res == {"media": 96, "front": None, "floor": None, "removed": 0, "recorded": False}
    assert out == before
    assert store.media == {}


def test_old_proxy_rebills_every_request_past_the_cap():
    # the 2026-10-24 shape: without the floor every request past 100 images is
    # history-changed at the oldest surviving image
    prev = None
    classes = []
    for turns in range(17, 23):
        body, _ = cache_proxy.normalize_request(_request(_history(turns)))
        if prev is not None:
            classes.append(_cls(prev, body))
        prev = body
    assert classes == ["history-changed"] * 5


def test_floor_turns_per_image_rebills_into_one_per_batch():
    store = _store()
    prev, breaks, appends = None, [], 0
    for turns in range(16, 60):  # 96 .. 354 images, 6 per request
        body, res = _proxy(_request(_history(turns)), store, now=T0 + turns)
        if prev is not None:
            cls = _cls(prev, body)
            if cls == "append":
                appends += 1
            else:
                assert res["recorded"], (turns, cls)
                breaks.append(turns)
        assert _n_media(body) <= cache_proxy.MEDIA_CAP
        prev = body
    # 254 images past the cap: one activation plus one move per ~50 images
    assert len(breaks) == 6 and appends == 37
    assert all(b - a >= 7 for a, b in zip(breaks, breaks[1:]))


def test_model_keeps_the_newest_images_between_50_and_100():
    store = _store()
    seen = []
    for turns in range(17, 40):
        body, _ = _proxy(_request(_history(turns)), store, now=T0 + turns)
        seen.append(_n_media(body))
    assert min(seen) >= 50 and max(seen) <= 100
    # the newest image is always present
    body, _ = _proxy(_request(_history(40)), store, now=T0 + 40)
    assert json.dumps(body["messages"][-1]).count('"type": "image"') == 6


def test_removed_images_become_one_fixed_placeholder_whoever_removed_them():
    store = _store()
    body, res = _proxy(_request(_history(25)), store)  # 150 images, CLI evicted 50
    assert res["recorded"] and res["removed"] > 0
    texts = [r["content"] for m in body["messages"][:res["floor"]] for r in m["content"]
             if r.get("type") == "tool_result"]
    assert texts and all(t == [{"type": "text", "text": cache_proxy.MEDIA_PLACEHOLDER}] for t in texts)
    assert cache_proxy.REMINDER_PLACEHOLDER not in json.dumps(body["messages"][:res["floor"]])
    # tool ids, order and the rest of every message are kept
    ids = [r["tool_use_id"] for m in body["messages"] for r in m["content"] if r.get("type") == "tool_result"]
    assert ids == [f"toolu_{t:03d}_{j}" for t in range(25) for j in range(6)]


def test_live_form_and_resumed_form_give_the_same_bytes():
    # live: the CLI splices a reminder into the newest tool result; the JSONL
    # replay (resume, fork) has none. The proxy must send identical bytes.
    for turns in (18, 26, 31):
        a, _ = _proxy(_request(_history(turns), live_reminder=True), _store())
        b, _ = _proxy(_request(_history(turns)), _store())
        assert _cls(a, b) == "identical"


def test_resume_after_a_proxy_restart_keeps_the_floor(tmp_path):
    store = _store(tmp_path)
    a, ra = _proxy(_request(_history(24)), store, now=T0)
    restarted = _store(tmp_path)
    b, rb = _proxy(_request(_history(25)), restarted, now=T0 + 600)
    assert rb["floor"] == ra["floor"] and not rb["recorded"]
    assert _cls(a, b) == "append"


def test_fork_from_an_earlier_point_matches_the_parent_there():
    store = _store()
    parent = {}
    for turns in range(16, 45):
        parent[turns], _ = _proxy(_request(_history(turns)), store, now=T0 + turns)
    # a fork child of the parent's turn-27 request, made after the parent moved
    # its floor further: same history plus the child's own prompt
    hist = _history(27) + [{"role": "assistant", "content": [{"type": "text", "text": "spawning"}]},
                           {"role": "user", "content": [{"type": "text", "text": "child task"}]}]
    child, res = _proxy(_request(hist), store, now=T0 + 100)
    assert not res["recorded"]
    assert _cls(parent[27], child) == "append"


def test_newest_message_is_never_touched():
    store = _store()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [_img(i) for i in range(120)]}]
    body, res = _proxy(_request(msgs), store)
    assert res["removed"] == 0 and res["floor"] is None
    assert _n_media(body) == 100


def test_breakpoints_are_never_dropped():
    store = _store()
    body = _request(_history(30))
    # an (unusual) marker on an old image that the floor removes
    first_img = next(r for m in body["messages"] for r in m["content"]
                     if r.get("type") == "tool_result" and r["content"])
    first_img["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    before = len(prefix_diff.breakpoints(body))
    out, res = _proxy(body, store)
    assert res["removed"] > 0
    assert len(prefix_diff.breakpoints(out)) == before


def test_tool_less_side_calls_and_documents():
    store = _store()
    body = _request(_history(30))
    body.pop("tools")
    _, res = _proxy(body, store)
    assert res["floor"] is None and store.media == {}
    # documents count like images (the CLI counts both)
    body = _request(_history(16))  # 96 images
    body["messages"][1]["content"] = [{"type": "document", "source": {"type": "text", "data": f"d{i}"}}
                                      for i in range(5)]
    _, res = _proxy(body, _store())
    assert res["media"] == 101 and res["floor"] is not None


def test_conversations_are_pinned_separately():
    store = _store()
    a, ra = _proxy(_request(_history(25)), store)
    other = _history(31)
    other[0]["content"][0]["text"] = "another conversation"
    b, rb = _proxy(_request(other), store)
    assert len(store.media) == 2 and ra["recorded"] and rb["recorded"]


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


def _through_handler(tmp_path, monkeypatch, model, turn_counts):
    logger = cache_proxy.RequestLogger(str(tmp_path / "requests"), max_bytes=10**9)
    monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", logger)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
    bodies = []
    up, stop = _stub(bodies)
    gpt_up, gpt_stop = _stub(bodies)  # a distinct URL: routes are told apart by upstream
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    monkeypatch.setattr(cache_proxy, "CLI_PROXY_UPSTREAM", gpt_up)
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", _free_port()), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
    try:
        for turns in turn_counts:
            body = _request(_history(turns))
            body["model"] = model
            body["max_tokens"] = 5
            r = httpx.post(url, content=json.dumps(body).encode(), timeout=10,
                           headers={"anthropic-version": "2023-06-01", "content-type": "application/json"})
            assert r.status_code == 200
    finally:
        srv.shutdown()
        stop()
        gpt_stop()
    logger.flush()
    rows = [json.loads(line) for p in sorted((tmp_path / "requests").glob("*/index.jsonl"))
            for line in p.read_text().splitlines() if line.strip()]
    return [json.loads(b) for b in bodies], rows


def test_handler_sends_the_floor_upstream_and_logs_it(tmp_path, monkeypatch):
    sent, rows = _through_handler(tmp_path, monkeypatch, "claude-opus-5-5", (10, 20, 21))
    # 120 images: the CLI sends 100; the floor sits at the message holding the
    # 51st, which leaves 54 (whole messages are cut); one more batch of 6 → 60
    assert [_n_media(b) for b in sent] == [60, 54, 60]
    assert _cls(sent[1], sent[2]) == "append"
    floors = [row["norm"].get("media_floor") for row in rows]
    assert floors[0]["floor"] is None and floors[0]["media"] == 60
    assert floors[1]["recorded"] is True and floors[2]["recorded"] is False


def test_handler_leaves_the_gpt_route_alone(tmp_path, monkeypatch):
    sent, rows = _through_handler(tmp_path, monkeypatch, "gpt-6.1-sol", (20, 21))
    assert [_n_media(b) for b in sent] == [100, 100]
    assert all("media_floor" not in (row.get("norm") or {}) for row in rows)
