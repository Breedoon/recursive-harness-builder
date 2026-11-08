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
import pytest

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


def test_an_early_fork_that_grew_differently_gets_its_own_floor():
    # V1 counterexample at 15452b2: the parent learned front 6 / floor 22; a fork
    # from its first image round then read 94 images in one new message. The
    # same front reused floor 22 on a 9-message history and dropped all 100.
    store = _store()
    _proxy(_request(_history(17)), store)
    child = _history(1) + [{"role": "assistant", "content": [{"type": "text", "text": "the rest together"}]},
                           {"role": "user", "content": [_img(6 + i) for i in range(94)]}]
    body = _request(child)
    newest = copy.deepcopy(cache_proxy.normalize_request(copy.deepcopy(body))[0]["messages"][-1])
    out, res = _proxy(body, store)
    assert _n_media(out) == 94 and out["messages"][-1] == newest
    # and the child's own floor then holds steady for it, while the parent's stays put
    later = child + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                     {"role": "user", "content": [{"type": "text", "text": "compare them"}]}]
    out2, res2 = _proxy(_request(later), store)
    assert res2["floor"] == res["floor"] and not res2["recorded"] and _cls(out, out2) == "append"
    again, res3 = _proxy(_request(_history(18)), store)
    assert res3["floor"] == 22 and not res3["recorded"]


def _rounds(sizes, first="review the pages"):
    """Full history whose turn t reads sizes[t] images in parallel."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": first}]}]
    n = 0
    for t, k in enumerate(sizes):
        ids = [f"toolu_{t:03d}_{j}" for j in range(k)]
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"reading batch {t}"}]
                     + [{"type": "tool_use", "id": i, "name": "Read", "input": {"file_path": f"/p/{i}.png"}}
                        for i in ids]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": [_img(n + j)]}
                                                 for j, i in enumerate(ids)]})
        n += k
    return msgs


def test_a_floor_that_would_cut_more_than_a_batch_here_is_not_reused():
    # The parent learned front 2 / floor 18 when the CLI had already evicted 36
    # of the 40 images in its front message. A fork made after the floor that
    # then grew less reaches the cap with 36 of them still there: reusing floor
    # 18 would cut 78 of its 100 images. It gets its own floor instead.
    store = _store()
    parent, rp = _proxy(_request(_rounds([40] + [6] * 16)), store, now=T0)
    assert (rp["front"], rp["floor"], rp["recorded"]) == (2, 18, True)
    child_hist = _rounds([40] + [6] * 9) + [
        {"role": "assistant", "content": [{"type": "text", "text": "ten more together"}]},
        {"role": "user", "content": [_img(1000 + i) for i in range(10)]}]
    body = _request(child_hist)
    cli = _n_media(body)
    out, rc = _proxy(body, store, now=T0 + 1)
    assert rc["front"] == 2 and rc["floor"] != 18 and rc["recorded"]
    assert _n_media(out) >= cli - cache_proxy.MEDIA_FLOOR_STEP
    # the parent keeps its own floor: its next request is an append
    nxt, rn = _proxy(_request(_rounds([40] + [6] * 17)), store, now=T0 + 2)
    assert rn["floor"] == 18 and not rn["recorded"] and _cls(parent, nxt) == "append"


def test_a_history_that_differs_before_the_floor_never_uses_it():
    # Two conversations with the same first message (one pin key) that differ
    # early but carry the same message just before the floor: each keeps its
    # own floors, so neither one's later floors move the other's bytes.
    store = _store()
    a_hist = _history(17)
    b_hist = copy.deepcopy(a_hist)
    b_hist[3]["content"][0]["text"] = "go on, differently"
    a, ra = _proxy(_request(a_hist), store, now=T0)
    b, rb = _proxy(_request(b_hist), store, now=T0 + 1)
    assert ra["floor"] == rb["floor"] and ra["recorded"] and rb["recorded"]
    assert a["messages"][ra["floor"] - 1] == b["messages"][rb["floor"] - 1]
    # each conversation then moves along its own floors only
    prev = {"a": a, "b": b}
    for turns in range(18, 40):
        for name, hist in (("a", _history(turns)), ("b", copy.deepcopy(_history(turns)))):
            if name == "b":
                hist[3]["content"][0]["text"] = "go on, differently"
            body, res = _proxy(_request(hist), store, now=T0 + turns)
            cls = _cls(prev[name], body)
            assert cls == "append" or res["recorded"], (name, turns, cls, res)
            prev[name] = body


def test_a_breakpoint_that_moves_off_the_history_does_not_move_the_floor():
    # the floor sits right behind a message that carried a cache_control marker
    # when it was learned; the CLI moves its markers forward, the floor stays
    store = _store()
    hist = _rounds([50, 50])
    body = _request(hist)
    body["messages"][3]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    first, r1 = _proxy(body, store)
    assert (r1["front"], r1["floor"], r1["recorded"]) == (2, 4, True)
    later = hist + [{"role": "assistant", "content": [{"type": "text", "text": "read"}]},
                    {"role": "user", "content": [{"type": "text", "text": "compare them"}]}]
    second, r2 = _proxy(_request(later), store)
    assert r2["floor"] == 4 and not r2["recorded"]


@pytest.mark.parametrize("batch", [1, 2, 3, 5, 6, 10, 17, 25, 33, 40, 49, 50, 51, 60, 75, 90, 99, 100, 101])
def test_never_more_than_a_batch_beyond_the_cli_and_never_the_newest_message(batch):
    store = _store()
    for turns in range(max(1, 100 // batch - 1), max(1, 100 // batch - 1) + 8):
        body = _request(_history(turns, per_turn=batch))
        cli = _n_media(body)
        newest = copy.deepcopy(cache_proxy.normalize_request(copy.deepcopy(body))[0]["messages"][-1])
        out, res = _proxy(body, store, now=T0 + turns)
        assert _n_media(out) >= cli - cache_proxy.MEDIA_FLOOR_STEP, (batch, turns, res)
        assert out["messages"][-1] == newest, (batch, turns, res)


def test_newest_message_is_never_touched():
    store = _store()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [_img(i) for i in range(120)]}]
    body, res = _proxy(_request(msgs), store)
    assert res["removed"] == 0 and res["floor"] is None
    assert _n_media(body) == 100


def test_a_large_batch_in_one_message_is_never_dropped_whole():
    # V1 counterexamples (2026-10-25): 100 images read in one parallel round,
    # then an ordinary text turn; and 90 in one round plus 10 in the newest.
    # Cutting whole messages there would leave 0 or 10 images: no floor instead.
    history = _history(1, per_turn=100)
    history += [{"role": "assistant", "content": [{"type": "text", "text": "images read"}]},
                {"role": "user", "content": [{"type": "text", "text": "compare those images"}]}]
    body, res = _proxy(_request(history), _store())
    assert res["floor"] is None and _n_media(body) == 100
    history = _history(1, per_turn=90)
    history += [{"role": "assistant", "content": [{"type": "text", "text": "ten more"}]},
                {"role": "user", "content": [_img(90 + i) for i in range(10)]}]
    body, res = _proxy(_request(history), _store())
    assert res["floor"] is None and _n_media(body) == 100
    # at most one batch is ever cut beyond the CLI's own eviction
    for per_turn in (6, 25, 40, 50):
        store = _store()
        for turns in range(100 // per_turn, 100 // per_turn + 12):
            body, res = _proxy(_request(_history(turns, per_turn=per_turn)), store, now=T0 + turns)
            assert _n_media(body) >= 100 - cache_proxy.MEDIA_FLOOR_STEP, (per_turn, turns, res)


def _markers(value):
    """Every cache_control value, nested ones included (prefix_diff.breakpoints sees top-level only)."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("cache_control"), dict):
            found.append(json.dumps(value["cache_control"], sort_keys=True))
        for key, child in value.items():
            if key != "cache_control":
                found += _markers(child)
    elif isinstance(value, list):
        for child in value:
            found += _markers(child)
    return sorted(found)


def test_breakpoints_are_never_dropped():
    for nested in ("image", "image+text"):
        store = _store()
        body = _request(_history(30))
        # unusual markers on old images that the floor removes, nested in tool results
        olds = [r for m in body["messages"] for r in m["content"]
                if r.get("type") == "tool_result" and r["content"]][:2]
        olds[0]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        olds[1]["content"][0]["cache_control"] = {"type": "ephemeral"}
        if nested == "image+text":
            olds[1]["content"].append({"type": "text", "text": "caption"})
        before = _markers(body)
        out, res = _proxy(body, store)
        assert res["removed"] > 0
        assert _markers(out) == before
        assert len(prefix_diff.breakpoints(out)) == len(prefix_diff.breakpoints(body))


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
