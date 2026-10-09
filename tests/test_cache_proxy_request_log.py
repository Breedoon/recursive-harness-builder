"""Durable session-keyed request log + scripts/prefix_diff.py.

Mission cache-proxy-prefix-fix (2026-10-08, bead vault-mief.2). The request log
is the instrument used to find prompt-cache prefix breakage; these tests pin:
- pre bytes == exactly what the client sent, wire bytes == exactly what the
  upstream received (so a diff of logged bodies is a diff of real traffic);
- session id, HTTP status and usage reach the index and usage.jsonl;
- credentials never reach disk;
- logging never fails or alters the proxied request;
- prefix_diff finds the first divergence and flags a missing breakpoint.
"""

import gzip
import json
import os
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

SID = "0a1b2c3d-1111-2222-3333-444455556666"
SECRET = "sk-ant-oat01-SUPERSECRETVALUE"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Rec:
    def __init__(self):
        self.bodies = []


def _stub(rec: _Rec, status: int = 200, stream: bool = True):
    usage = {"input_tokens": 3, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 20, "output_tokens": 0}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            rec.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            if status != 200:
                payload = json.dumps({"type": "error", "error": {
                    "type": "rate_limit_error", "message": "would exceed your rate limit"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if stream:
                ev = {"type": "message_start", "message": {"usage": usage}}
                payload = f"event: message_start\ndata: {json.dumps(ev)}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(payload)
            else:
                payload = json.dumps({"usage": usage, "content": []}).encode()
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


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    logger = cache_proxy.RequestLogger(str(tmp_path / "requests"), max_bytes=10**9)
    monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", logger)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", _free_port()), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", logger, tmp_path
    srv.shutdown()


def _body(stream=True, extra_msgs=()):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello",
                                           "cache_control": {"type": "ephemeral"}}]}]
    msgs.extend(extra_msgs)
    return {"model": "claude-haiku-4-5", "stream": stream, "max_tokens": 5,
            "system": [{"type": "text", "text": "sys"}],
            "metadata": {"user_id": f"user_abc_account_def_session_{SID}"},
            "messages": msgs}


def _send(url, body):
    raw = json.dumps(body).encode()
    r = httpx.post(url + "/v1/messages", content=raw, timeout=10,
                   headers={"x-api-key": SECRET, "authorization": f"Bearer {SECRET}",
                            "anthropic-version": "2023-06-01",
                            "anthropic-beta": "prompt-caching-scope-2026-01-05",
                            "content-type": "application/json"})
    return raw, r


def _index(root):
    rows = []
    for p in sorted((root / "requests").glob("*/index.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return rows


def _all_files_text(root) -> str:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            if p.suffix == ".gz":
                data = gzip.decompress(data)
            out.append(data.decode("utf-8", errors="replace"))
    return "\n".join(out)


@pytest.mark.parametrize("stream", [True, False])
def test_capture_is_exact_and_session_keyed(proxy, monkeypatch, stream):
    url, logger, tmp = proxy
    rec = _Rec()
    up, stop = _stub(rec, stream=stream)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    try:
        raw, r = _send(url, _body(stream=stream))
    finally:
        stop()
    assert r.status_code == 200
    logger.flush()
    rows = _index(tmp)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == SID
    assert row["req_id"].endswith(SID[:8])
    assert row["http_status"] == 200
    assert row["usage"]["cache_read"] == 1000
    day_dir = tmp / "requests" / f"{row['req_id'][0:4]}-{row['req_id'][4:6]}-{row['req_id'][6:8]}"
    pre = gzip.decompress((day_dir / f"{row['req_id']}.pre.json.gz").read_bytes())
    wire = gzip.decompress((day_dir / f"{row['req_id']}.wire.json.gz").read_bytes())
    assert pre == raw                      # exactly what the client sent
    assert wire == rec.bodies[0]           # exactly what the upstream received
    assert row["wire_sha256"]
    # normalize_metadata ran on the wire copy only
    assert "_session_0" in json.loads(wire)["metadata"]["user_id"]
    # usage.jsonl carries the join keys
    usage = json.loads((tmp / "usage.jsonl").read_text().splitlines()[-1])
    assert usage["session_id"] == SID and usage["req_id"] == row["req_id"]
    assert usage["http_status"] == 200
    # credentials never reach disk
    assert SECRET not in _all_files_text(tmp)
    assert "x-api-key" not in row["client_headers"]
    assert "authorization" not in row["client_headers"]
    assert row["client_headers"]["anthropic-beta"] == "prompt-caching-scope-2026-01-05"


def test_error_status_is_recorded(proxy, monkeypatch):
    url, logger, tmp = proxy
    rec = _Rec()
    up, stop = _stub(rec, status=429)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    try:
        _, r = _send(url, _body())
    finally:
        stop()
    assert r.status_code == 429            # passed through unchanged
    logger.flush()
    row = _index(tmp)[0]
    assert row["http_status"] == 429
    assert "rate_limit_error" in row["error"]
    assert row["usage"] is None


def test_upstream_down_is_recorded_as_502(proxy, monkeypatch):
    url, logger, tmp = proxy
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", f"http://127.0.0.1:{_free_port()}")
    _, r = _send(url, _body())
    assert r.status_code == 502
    logger.flush()
    row = _index(tmp)[0]
    assert row["http_status"] == 502 and "proxy upstream error" in row["error"]


def test_logging_failure_never_breaks_traffic(proxy, monkeypatch):
    url, logger, tmp = proxy

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(logger, "submit_bodies", boom)
    monkeypatch.setattr(logger, "submit_index", boom)
    rec = _Rec()
    up, stop = _stub(rec)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    try:
        _, r = _send(url, _body())
    finally:
        stop()
    assert r.status_code == 200 and len(rec.bodies) == 1


def test_no_logger_means_no_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", None)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", _free_port()), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rec = _Rec()
    up, stop = _stub(rec)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    try:
        _, r = _send(f"http://127.0.0.1:{srv.server_address[1]}", _body())
    finally:
        stop()
        srv.shutdown()
    assert r.status_code == 200
    assert not (tmp_path / "requests").exists()


def test_queue_full_drops_instead_of_blocking(tmp_path):
    logger = cache_proxy.RequestLogger(str(tmp_path), queue_size=1)
    gate = threading.Event()
    logger._put(("barrier", gate))         # writer sets gate; fill queue meanwhile
    for _ in range(50):
        logger.submit_index({"req_id": "20261008T000000.000000Z-1-000001-x"})
    assert logger.dropped > 0


def test_prune_keeps_under_cap(tmp_path):
    logger = cache_proxy.RequestLogger(str(tmp_path), max_bytes=20_000)
    big = os.urandom(6000)  # incompressible
    for i in range(10):
        rid = f"20261008T0000{i:02d}.000000Z-1-{i:06d}-sess"
        logger.submit_bodies(rid, big, None, big)
    logger.flush()
    total = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert total <= 20_000
    remaining = sorted(p.name for p in tmp_path.rglob("*.json.gz"))
    assert remaining and remaining[-1].startswith("20261008T000009")  # newest kept


def test_extract_session_id():
    assert cache_proxy.extract_session_id(_body()) == SID
    assert cache_proxy.extract_session_id({"metadata": {"user_id": "x"}}) == ""
    assert cache_proxy.extract_session_id({}) == ""


# ── prefix_diff ──────────────────────────────────────────────────────────

def _req(msgs, system="sys", tools=("A", "B"), **cfg):
    r = {"model": "claude-x", "system": [{"type": "text", "text": system}],
         "tools": [{"name": t, "input_schema": {}} for t in tools], "messages": msgs}
    r.update(cfg)
    return r


def _u(text, bp=False):
    blk = {"type": "text", "text": text}
    if bp:
        blk["cache_control"] = {"type": "ephemeral"}
    return {"role": "user", "content": [blk]}


def _a(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def test_append_ignores_moved_cache_control():
    a = _req([_u("q1", bp=True)])
    b = _req([_u("q1"), _a("r1"), _u("q2", bp=True)])
    r = prefix_diff.diff_requests(a, b)
    assert r["classification"] == "append"
    assert r["first_divergence"] is None
    assert r["b_last_message_breakpoint"] == 2


def test_history_change_located_to_block_and_byte():
    a = _req([_u("q1"), _a("r1"), _u("q2", bp=True)])
    b = _req([_u("q1"), _a("r1 CHANGED"), _u("q2"), _a("r2"), _u("q3", bp=True)])
    r = prefix_diff.diff_requests(a, b)
    assert r["classification"] == "history-changed"
    d = r["first_divergence"]
    assert d["path"] == "messages[1].content[0]"
    assert d["message_index"] == 1
    assert "CHANGED" in d["b_snippet"]


def test_missing_message_breakpoint_flagged():
    a = _req([_u("q1", bp=True)])
    b = _req([_u("q1"), _a("r1"), _u("q2")])
    r = prefix_diff.diff_requests(a, b)
    assert r["classification"] == "append"
    assert r["b_has_message_breakpoint"] is False
    assert "NO MESSAGE BREAKPOINT" in prefix_diff.format_result(r)


def test_system_tools_config_shrunk():
    base = [_u("q1"), _a("r1"), _u("q2")]
    assert prefix_diff.diff_requests(_req(base), _req(base + [_a("x")], system="sys2"))[
        "classification"] == "system-changed"
    assert prefix_diff.diff_requests(_req(base), _req(base, tools=("B", "A")))[
        "classification"] == "tools-changed"
    r = prefix_diff.diff_requests(_req(base, thinking={"type": "enabled"}),
                                  _req(base + [_a("x")], thinking={"type": "disabled"}))
    assert r["classification"] == "config-changed"
    assert r["config_diffs"][0]["key"] == "thinking"
    assert prefix_diff.diff_requests(_req(base), _req(base[:1]))["classification"] == "shrunk"
    r = prefix_diff.diff_requests(_req(base), _req(base + [_a("x")]), "beta1", "beta2")
    assert r["classification"] == "config-changed"


def test_session_and_prev_any_over_logged_requests(tmp_path):
    logger = cache_proxy.RequestLogger(str(tmp_path))
    reqs = [
        _req([_u("q1", bp=True)]),
        _req([_u("q1"), _a("r1"), _u("q2", bp=True)]),
        _req([_u("q1"), _a("r1 EDITED"), _u("q2"), _a("r2"), _u("q3", bp=True)]),
    ]
    ids = []
    for i, body in enumerate(reqs):
        rid = f"20261008T0000{i:02d}.000000Z-1-{i:06d}-{SID[:8]}"
        wire = json.dumps(body).encode()
        logger.submit_bodies(rid, wire, None, wire)
        logger.submit_index({"req_id": rid, "ts": 1000 + i, "ts_iso": f"2026-10-08T00:00:0{i}Z",
                             "session_id": SID, "model": "claude-x", "http_status": 200})
        ids.append(rid)
    # a "recovery" request from a new session id that continues request 1
    rec_id = f"20261008T000009.000000Z-1-000009-ffffffff"
    rec_body = _req([_u("q1"), _a("r1"), _u("q2"), _a("r2b"), _u("q3b", bp=True)])
    logger.submit_bodies(rec_id, json.dumps(rec_body).encode(), None, json.dumps(rec_body).encode())
    logger.submit_index({"req_id": rec_id, "ts": 1009, "ts_iso": "2026-10-08T00:00:09Z",
                         "session_id": "ffffffff-0000", "model": "claude-x"})
    logger.flush()
    pairs = prefix_diff.session_pairs(str(tmp_path), SID[:8], "wire")
    assert [p["classification"] for p in pairs] == ["append", "history-changed"]
    best = prefix_diff.prev_any(str(tmp_path), rec_id, "wire", window_min=90)
    assert best["a"] == ids[1] and best["classification"] == "append"
    assert prefix_diff.main(["--session", SID[:8], "--log-dir", str(tmp_path)]) == 0


def test_request_log_default_on_only_for_prod_port(monkeypatch):
    monkeypatch.setattr(cache_proxy, "_REQUEST_LOG_ENV", "")
    assert cache_proxy.request_log_enabled(cache_proxy.PROD_PORT) is True
    assert cache_proxy.request_log_enabled(cache_proxy.PROD_PORT + 1) is False
    monkeypatch.setattr(cache_proxy, "_REQUEST_LOG_ENV", "1")
    assert cache_proxy.request_log_enabled(12345) is True
    monkeypatch.setattr(cache_proxy, "_REQUEST_LOG_ENV", "0")
    assert cache_proxy.request_log_enabled(cache_proxy.PROD_PORT) is False


# ── standing pool: ~1 GB ring buffer + secret redaction (Daniel 2026-10-08 23:55Z) ──

BOT = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0"
OAUTH = "sk-ant-oat01-" + "Q" * 20 + "abcdefgh12345678"
SESSION_STR = "1" + "BVtsOHgBu7Zx9KqLm3Pd" * 18  # telethon-like, 361 chars


def test_default_cap_is_one_gigabyte():
    import importlib
    old = os.environ.pop("CACHE_PROXY_REQUEST_LOG_MAX_GB", None)
    try:
        mod = importlib.reload(cache_proxy)
        assert mod.REQUEST_LOG_MAX_BYTES == 10**9
    finally:
        if old is not None:
            os.environ["CACHE_PROXY_REQUEST_LOG_MAX_GB"] = old
        importlib.reload(cache_proxy)


def test_redaction_scrubs_body_secrets_keeps_json_and_equality():
    text = (f"env: TELEGRAM_BOT_TOKEN={BOT} OAUTH={OAUTH} "
            f"session_string={SESSION_STR} Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 "
            f"ghp_{'a1B2' * 9} plain words stay, input_tokens 12345678")
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
            "api_key": "abcd1234efgh5678", "max_tokens": 32000}
    raw = json.dumps(body).encode()
    red = cache_proxy.redact_secrets(raw)
    for secret in (BOT, OAUTH, SESSION_STR, "abcdefghijklmnopqrstuvwxyz0123", "ghp_", "abcd1234efgh5678"):
        assert secret.encode() not in red, secret
    parsed = json.loads(red)                       # still valid JSON
    assert parsed["max_tokens"] == 32000
    assert "plain words stay" in parsed["messages"][0]["content"][0]["text"]
    assert cache_proxy.redact_secrets(red) == red  # idempotent
    # same secret -> same placeholder; different secret -> different placeholder
    a = cache_proxy.redact_secrets(f"x {OAUTH} y".encode())
    b = cache_proxy.redact_secrets(f"x {OAUTH} y".encode())
    c = cache_proxy.redact_secrets(f"x {OAUTH[:-1]}Z y".encode())
    assert a == b and a != c


def test_redaction_leaves_paths_and_ordinary_json_alone():
    path = "/workspace/runtime/git/obs-artifacts/Drafts/Artifacts/" + "a-b-c/" * 80
    raw = json.dumps({"p": path, "usage": {"input_tokens": 123456789,
                                           "cache_read_input_tokens": 5}}).encode()
    assert cache_proxy.redact_secrets(raw) == raw


def test_captured_files_and_index_are_redacted(proxy, monkeypatch):
    url, logger, tmp = proxy
    rec = _Rec()
    up, stop = _stub(rec)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", up)
    body = _body()
    body["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    body["messages"].append({"role": "user", "content": [
        {"type": "text", "text": f"my bot token is {BOT} and {OAUTH}", "cache_control": {"type": "ephemeral"}}]})
    try:
        _, r = _send(url, body)
    finally:
        stop()
    assert r.status_code == 200
    assert BOT.encode() in rec.bodies[0]           # upstream got the real bytes
    logger.flush()
    disk = _all_files_text(tmp / "requests")
    assert BOT not in disk and OAUTH not in disk and SECRET not in disk
    assert "[REDACTED:telegram-bot:" in disk
    row = _index(tmp)[0]
    day_dir = tmp / "requests" / f"{row['req_id'][0:4]}-{row['req_id'][4:6]}-{row['req_id'][6:8]}"
    wire = json.loads(gzip.decompress((day_dir / f"{row['req_id']}.wire.json.gz").read_bytes()))
    assert len(wire["messages"]) == 3               # structure intact for prefix_diff


def test_prefix_diff_append_across_redacted_captures(tmp_path):
    logger = cache_proxy.RequestLogger(str(tmp_path))
    m1 = [_u(f"token={BOT}", bp=True)]
    m2 = [_u(f"token={BOT}"), _a("r1"), _u("q2", bp=True)]
    ids = []
    for i, msgs in enumerate((m1, m2)):
        rid = f"20261008T0000{i:02d}.000000Z-1-{i:06d}-{SID[:8]}"
        wire = json.dumps(_req(msgs)).encode()
        logger.submit_bodies(rid, wire, None, wire)
        ids.append(rid)
    logger.flush()
    a = prefix_diff.load_request(ids[0], "wire", str(tmp_path))
    b = prefix_diff.load_request(ids[1], "wire", str(tmp_path))
    assert prefix_diff.diff_requests(a, b)["classification"] == "append"


def test_ring_buffer_evicts_old_days_including_index(tmp_path):
    logger = cache_proxy.RequestLogger(str(tmp_path), max_bytes=30_000)
    big = os.urandom(6000)
    for day in ("20261006", "20261007"):
        for i in range(3):
            rid = f"{day}T0000{i:02d}.000000Z-1-{i:06d}-sess"
            logger.submit_bodies(rid, big, None, big)
            logger.submit_index({"req_id": rid, "pad": "x" * 500})
    logger.flush()
    for i in range(3):
        rid = f"20261008T0000{i:02d}.000000Z-1-{i:06d}-sess"
        logger.submit_bodies(rid, big, None, big)
        logger.submit_index({"req_id": rid})
    logger.flush()
    total = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert total <= 30_000
    assert not (tmp_path / "2026-10-06").exists()   # whole oldest day evicted, index too
    assert (tmp_path / "2026-10-08" / "index.jsonl").exists()


def _a_think(text, sig="sigA"):
    return {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm", "signature": sig},
        {"type": "text", "text": text}]}


def test_prefix_diff_flags_dropped_thinking_blocks():
    a = _req([_u("q1"), _a_think("r1"), _u("q2", bp=True)])
    b = _req([_u("q1"), _a("r1"), _u("q2"), _a("r2"), _u("q3", bp=True)])
    r = prefix_diff.diff_requests(a, b)
    assert r["classification"] == "thinking-changed"
    assert r["thinking_count_changed_at"] == [1]
    assert r["first_divergence"]["thinking"] == {"a": 1, "b": 0}
    assert "thinking-block count differs" in prefix_diff.format_result(r)


def test_obs_fork_context_extracted_from_last_user_message():
    boot = ("<obs-bootstrap version=\"2\"><fork_context><origin>session_recovery</origin>"
            "<is_fork>true</is_fork><session_id>839b1149-238e-431b-bcae-2df3475981d4</session_id>"
            "<parent_session_id>9f495d93-ca57-419e-9425-80d0501cd299</parent_session_id>"
            "</fork_context></obs-bootstrap>")
    body = {"messages": [_u("old"), _a("x"), _u(boot + " continue")]}
    assert cache_proxy.extract_obs_fork_context(body) == {
        "origin": "session_recovery",
        "parent_session_id": "9f495d93-ca57-419e-9425-80d0501cd299"}
    assert cache_proxy.extract_obs_fork_context({"messages": [_u("plain")]}) == {}
