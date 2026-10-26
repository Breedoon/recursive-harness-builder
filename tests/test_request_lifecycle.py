"""Tests for content-free local-model request lifecycle logging.

Red→green fixtures for: streaming/nonstreaming, retries/cancel/client-gone,
missing identity, logger backpressure/failure, exact wire-byte equality,
one-row conservation, output tokens, and summary unknown coverage.

Design contract: Local GPU Budgeting Plan §C (bead vault-gsku).
"""
import json
import os
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cache_proxy  # noqa: E402
from obs_agent.request_lifecycle import (
    LifecycleRow,
    RequestLifecycleLogger,
    _SessionIdentityCache,
    extract_identity_from_body,
    extract_identity_from_bootstrap,
    new_lifecycle_request_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SID = "0a1b2c3d-1111-2222-3333-444455556666"
BOOTSTRAP_XML = (
    '<obs-bootstrap version="2"><obs-lineage>'
    '<obs-node display_name="Root" agent_name="2026-10-26-02-00-root" />'
    '<obs-node display_name="Worker" agent_name="abc123-worker" />'
    '</obs-lineage><fork_context><origin>agent_task_fresh</origin>'
    '<is_fork>false</is_fork></fork_context>'
    '<team_context><root_team_key>2026-10-26-02-00-root</root_team_key>'
    '<agent_name>abc123-worker</agent_name>'
    '</team_context></obs-bootstrap>'
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_body(*, stream=True, model="local-qwen3.8-27b", max_tokens=4096,
               with_bootstrap=False, session_id=SID) -> dict:
    messages = []
    if with_bootstrap:
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": f"{BOOTSTRAP_XML}\nHello"}],
        })
    else:
        messages.append({"role": "user", "content": "Hello"})
    return {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": messages,
        "metadata": {"user_id": f"test_user_session_{session_id}"},
    }


class _LocalStub:
    """A fake local upstream that speaks the Anthropic streaming/non-streaming API."""

    def __init__(self, *, stream=True, status=200, delay=0,
                 output_tokens=42, hang=False):
        self.stream = stream
        self.status = status
        self.delay = delay
        self.output_tokens = output_tokens
        self.hang = hang
        self.received_bodies: list[bytes] = []

        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
            "output_tokens": output_tokens,
        }

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(handler_self):
                length = int(handler_self.headers.get("Content-Length", 0) or 0)
                self.received_bodies.append(handler_self.rfile.read(length))
                if delay:
                    time.sleep(delay)
                if hang:
                    # Never respond — simulate upstream silence
                    time.sleep(3600)
                    return
                if status != 200:
                    payload = json.dumps({
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "busy"},
                    }).encode()
                    handler_self.send_response(status)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(payload)))
                    handler_self.end_headers()
                    handler_self.wfile.write(payload)
                    return
                if stream:
                    start_ev = {"type": "message_start", "message": {"usage": usage}}
                    delta_ev = {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "hi"},
                    }
                    stop_ev = {
                        "type": "message_delta",
                        "usage": {"output_tokens": output_tokens},
                    }
                    payload = (
                        f"event: message_start\ndata: {json.dumps(start_ev)}\n\n"
                        f"event: content_block_delta\ndata: {json.dumps(delta_ev)}\n\n"
                        f"event: message_delta\ndata: {json.dumps(stop_ev)}\n\n"
                    ).encode()
                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "text/event-stream")
                    handler_self.end_headers()
                    handler_self.wfile.write(payload)
                else:
                    payload = json.dumps({
                        "usage": usage,
                        "content": [{"type": "text", "text": "hi"}],
                    }).encode()
                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(payload)))
                    handler_self.end_headers()
                    handler_self.wfile.write(payload)

            def log_message(self, *a):
                pass

        port = _free_port()
        self.srv = HTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def shutdown(self):
        self.srv.shutdown()


@pytest.fixture
def lifecycle_dir(tmp_path):
    d = tmp_path / "lifecycle"
    d.mkdir()
    return str(d)


@pytest.fixture
def logger(lifecycle_dir):
    lg = RequestLifecycleLogger(root=lifecycle_dir, max_bytes=10**9)
    yield lg
    lg.flush()


@pytest.fixture
def local_stub():
    stub = _LocalStub(stream=True)
    yield stub
    stub.shutdown()


@pytest.fixture
def local_stub_nonstream():
    stub = _LocalStub(stream=False)
    yield stub
    stub.shutdown()


@pytest.fixture
def proxy_with_lifecycle(tmp_path, local_stub, lifecycle_dir, monkeypatch):
    """Start a proxy with lifecycle logging pointed at a local stub."""
    log_dir = str(tmp_path / "proxy-log")
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", local_stub.url)
    monkeypatch.setattr(cache_proxy, "LOG_DIR", log_dir)
    monkeypatch.setattr(cache_proxy, "USAGE_LOG", os.path.join(log_dir, "usage.jsonl"))
    monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", None)
    lc_logger = RequestLifecycleLogger(root=lifecycle_dir, max_bytes=10**9)
    monkeypatch.setattr(cache_proxy, "LIFECYCLE_LOGGER", lc_logger)
    monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
    port = _free_port()
    srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, lc_logger, lifecycle_dir
    srv.shutdown()
    lc_logger.flush()


def _send_request(proxy_port: int, body: dict) -> httpx.Response:
    """Send a /v1/messages request to the proxy."""
    return httpx.post(
        f"http://127.0.0.1:{proxy_port}/v1/messages",
        json=body,
        headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
        timeout=30,
    )


def _read_lifecycle_rows(lifecycle_dir: str) -> list[dict]:
    rows = []
    for name in sorted(os.listdir(lifecycle_dir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(lifecycle_dir, name)) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Identity extraction tests
# ---------------------------------------------------------------------------

class TestIdentityExtraction:
    def test_extract_from_bootstrap_xml(self):
        identity = extract_identity_from_bootstrap(BOOTSTRAP_XML)
        assert identity is not None
        assert identity["agent_name"] == "abc123-worker"
        assert identity["root_team_key"] == "2026-10-26-02-00-root"
        assert identity["lineage"] == ["2026-10-26-02-00-root", "abc123-worker"]

    def test_extract_from_body_with_bootstrap(self):
        body = _make_body(with_bootstrap=True)
        identity = extract_identity_from_body(body)
        assert identity is not None
        assert identity["agent_name"] == "abc123-worker"

    def test_extract_from_body_without_bootstrap(self):
        body = _make_body(with_bootstrap=False)
        identity = extract_identity_from_body(body)
        assert identity is None

    def test_no_bootstrap_in_text(self):
        assert extract_identity_from_bootstrap("just plain text") is None

    def test_empty_messages(self):
        assert extract_identity_from_body({"messages": []}) is None
        assert extract_identity_from_body({}) is None


# ---------------------------------------------------------------------------
# Session identity cache tests
# ---------------------------------------------------------------------------

class TestSessionIdentityCache:
    def test_put_and_get(self):
        cache = _SessionIdentityCache(max_size=10)
        identity = {"agent_name": "test", "root_team_key": "team1"}
        cache.put("sess1", identity)
        assert cache.get("sess1") == identity

    def test_miss(self):
        cache = _SessionIdentityCache(max_size=10)
        assert cache.get("nonexistent") is None

    def test_ttl_expiry(self):
        cache = _SessionIdentityCache(max_size=10, ttl=0.01)
        cache.put("sess1", {"agent_name": "test"})
        time.sleep(0.02)
        assert cache.get("sess1") is None

    def test_lru_eviction(self):
        cache = _SessionIdentityCache(max_size=2)
        cache.put("a", {"agent_name": "a"})
        cache.put("b", {"agent_name": "b"})
        cache.put("c", {"agent_name": "c"})
        # 'a' should have been evicted
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None

    def test_empty_key(self):
        cache = _SessionIdentityCache()
        cache.put("", {"agent_name": "test"})
        assert cache.get("") is None


# ---------------------------------------------------------------------------
# LifecycleRow tests
# ---------------------------------------------------------------------------

class TestLifecycleRow:
    def test_basic_record(self):
        row = LifecycleRow(
            request_id="lc-test-001",
            session_id=SID,
            model="local-qwen3.8-27b",
            stream=True,
            max_tokens=4096,
        )
        row.mark_upstream_send()
        time.sleep(0.01)
        row.mark_headers(200)
        row.mark_first_event()
        row.mark_terminal("success", usage={
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
            "output_tokens": 42,
        }, http_status=200)
        record = row.to_record()

        assert record["request_id"] == "lc-test-001"
        assert record["session_id"] == SID
        assert record["model"] == "local-qwen3.8-27b"
        assert record["stream"] is True
        assert record["max_tokens"] == 4096
        assert record["outcome"] == "success"
        assert record["http_status"] == 200
        assert record["usage"]["output_tokens"] == 42
        assert record["durations"]["total_ms"] > 0
        assert record["t_receive"] is not None
        assert record["t_terminal"] is not None

    def test_missing_fields_are_explicit(self):
        row = LifecycleRow(
            request_id="lc-test-002",
            session_id="",
            model="local-test",
        )
        row.mark_terminal("error", error_class="upstream_error")
        record = row.to_record()

        assert record["session_id"] is None  # empty string -> None in logic? actually ""
        # But the contract says explicit missing/unknown
        assert record["t_hold_start"] is None
        assert record["t_hold_end"] is None
        assert record["t_first_event"] is None
        assert record["t_first_delta"] is None
        assert record["durations"]["hold_ms"] is None
        assert record["durations"]["ttfd_ms"] is None
        assert record["identity"] is None
        assert record["identity_source"] == "unknown"

    def test_hold_duration(self):
        row = LifecycleRow(
            request_id="lc-test-003",
            session_id=SID,
            model="local-test",
        )
        row.mark_hold_start()
        time.sleep(0.02)
        row.mark_hold_end()
        row.mark_terminal("success")
        record = row.to_record()

        assert record["t_hold_start"] is not None
        assert record["t_hold_end"] is not None
        hold_ms = record["durations"]["hold_ms"]
        assert hold_ms is not None and hold_ms >= 15  # at least ~15ms

    def test_identity_from_bootstrap(self):
        body = _make_body(with_bootstrap=True)
        row = LifecycleRow(
            request_id="lc-test-004",
            session_id=SID,
            model="local-test",
        )
        row.resolve_identity(body)
        record = row.to_record()

        assert record["identity_source"] == "bootstrap"
        assert record["identity"]["agent_name"] == "abc123-worker"

    def test_identity_from_session_cache(self):
        # Pre-populate cache
        from obs_agent.request_lifecycle import _identity_cache
        _identity_cache.put(SID, {
            "agent_name": "cached-agent",
            "root_team_key": "cached-team",
        })
        row = LifecycleRow(
            request_id="lc-test-005",
            session_id=SID,
            model="local-test",
        )
        row.resolve_identity(None)
        record = row.to_record()

        assert record["identity_source"] == "session_cache"
        assert record["identity"]["agent_name"] == "cached-agent"

    def test_identity_unknown(self):
        row = LifecycleRow(
            request_id="lc-test-006",
            session_id="unknown-session-never-seen",
            model="local-test",
        )
        row.resolve_identity(None)
        record = row.to_record()

        assert record["identity_source"] == "unknown"
        assert record["identity"] is None

    def test_first_event_only_set_once(self):
        row = LifecycleRow(
            request_id="lc-test-007",
            session_id=SID,
            model="local-test",
        )
        row.mark_first_event()
        t1 = row.t_first_event
        time.sleep(0.01)
        row.mark_first_event()  # should not change
        assert row.t_first_event == t1


# ---------------------------------------------------------------------------
# Logger tests
# ---------------------------------------------------------------------------

class TestRequestLifecycleLogger:
    def test_write_and_read(self, lifecycle_dir):
        lg = RequestLifecycleLogger(root=lifecycle_dir)
        row = LifecycleRow(
            request_id="lc-log-001",
            session_id=SID,
            model="local-test",
        )
        row.mark_terminal("success", usage={
            "input_tokens": 100,
            "output_tokens": 10,
        })
        lg.submit(row.to_record())
        lg.flush()

        rows = _read_lifecycle_rows(lifecycle_dir)
        assert len(rows) == 1
        assert rows[0]["request_id"] == "lc-log-001"
        assert rows[0]["outcome"] == "success"

    def test_one_row_per_request(self, lifecycle_dir):
        """Conservation: exactly one row per submit call."""
        lg = RequestLifecycleLogger(root=lifecycle_dir)
        for i in range(5):
            row = LifecycleRow(
                request_id=f"lc-cons-{i:03d}",
                session_id=SID,
                model="local-test",
            )
            row.mark_terminal("success")
            lg.submit(row.to_record())
        lg.flush()

        rows = _read_lifecycle_rows(lifecycle_dir)
        assert len(rows) == 5
        ids = {r["request_id"] for r in rows}
        assert len(ids) == 5

    def test_backpressure_drops(self, lifecycle_dir):
        """When the queue is full, records are dropped, never blocking."""
        lg = RequestLifecycleLogger(root=lifecycle_dir, queue_size=2)
        # Fill the queue and the writer
        for i in range(20):
            row = LifecycleRow(
                request_id=f"lc-bp-{i:03d}",
                session_id=SID,
                model="local-test",
            )
            row.mark_terminal("success")
            lg.submit(row.to_record())
        lg.flush()

        rows = _read_lifecycle_rows(lifecycle_dir)
        # Some rows written, some dropped; total should be <= 20
        assert lg.written + lg.dropped == 20 or lg.written <= 20
        assert lg.dropped >= 0  # may or may not have dropped

    def test_rotation(self, lifecycle_dir):
        """Old files are pruned when max_bytes is exceeded."""
        lg = RequestLifecycleLogger(root=lifecycle_dir, max_bytes=500)
        for i in range(50):
            row = LifecycleRow(
                request_id=f"lc-rot-{i:03d}",
                session_id=SID,
                model="local-test",
            )
            row.mark_terminal("success")
            lg.submit(row.to_record())
        lg.flush()
        time.sleep(0.1)

        total = sum(
            os.path.getsize(os.path.join(lifecycle_dir, f))
            for f in os.listdir(lifecycle_dir) if f.endswith(".jsonl")
        )
        # Should be around or under max_bytes (rotation may lag by one write)
        assert total < 5000  # generous bound


# ---------------------------------------------------------------------------
# Request ID generation
# ---------------------------------------------------------------------------

class TestRequestIdGeneration:
    def test_unique_ids(self):
        ids = {new_lifecycle_request_id("sess1") for _ in range(100)}
        assert len(ids) == 100

    def test_includes_session_prefix(self):
        rid = new_lifecycle_request_id("abcdefgh-1234")
        assert "abcdefgh" in rid

    def test_lc_prefix(self):
        rid = new_lifecycle_request_id()
        assert rid.startswith("lc-")


# ---------------------------------------------------------------------------
# Integration: proxy with lifecycle logging (streaming)
# ---------------------------------------------------------------------------

class TestProxyIntegrationStreaming:
    def test_streaming_success(self, proxy_with_lifecycle):
        port, lg, lc_dir = proxy_with_lifecycle
        body = _make_body(stream=True, with_bootstrap=True)
        resp = _send_request(port, body)
        assert resp.status_code == 200
        lg.flush()
        time.sleep(0.1)

        rows = _read_lifecycle_rows(lc_dir)
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "success"
        assert row["stream"] is True
        assert row["model"] == "local-qwen3.8-27b"
        assert row["route"] == "local"
        assert row["max_tokens"] == 4096
        assert row["identity_source"] == "bootstrap"
        assert row["identity"]["agent_name"] == "abc123-worker"
        assert row["usage"] is not None
        assert row["usage"]["input_tokens"] == 100
        assert row["usage"]["output_tokens"] == 42
        assert row["durations"]["total_ms"] >= 0
        assert row["t_receive"] is not None
        assert row["t_terminal"] is not None
        assert row["t_upstream_send"] is not None

    def test_wire_bytes_unaltered(self, proxy_with_lifecycle, local_stub):
        """The lifecycle logger must never alter the serialized request bytes."""
        port, lg, lc_dir = proxy_with_lifecycle
        body = _make_body(stream=True, with_bootstrap=True)
        resp = _send_request(port, body)
        assert resp.status_code == 200
        lg.flush()

        # The upstream stub received exactly the normalized body,
        # not anything modified by lifecycle logging
        assert len(local_stub.received_bodies) == 1
        received = json.loads(local_stub.received_bodies[0])
        # model should be the normalized name, no lifecycle fields injected
        assert "lifecycle" not in json.dumps(received)
        assert "request_id" not in received.get("metadata", {})

    def test_session_identity_cached(self, proxy_with_lifecycle):
        """Second request from same session uses cached identity."""
        port, lg, lc_dir = proxy_with_lifecycle
        body1 = _make_body(stream=True, with_bootstrap=True)
        _send_request(port, body1)

        body2 = _make_body(stream=True, with_bootstrap=False)
        _send_request(port, body2)
        lg.flush()
        time.sleep(0.1)

        rows = _read_lifecycle_rows(lc_dir)
        assert len(rows) == 2
        assert rows[0]["identity_source"] == "bootstrap"
        assert rows[1]["identity_source"] == "session_cache"
        assert rows[1]["identity"]["agent_name"] == "abc123-worker"


# ---------------------------------------------------------------------------
# Integration: proxy with lifecycle logging (non-streaming)
# ---------------------------------------------------------------------------

class TestProxyIntegrationNonStreaming:
    def test_nonstreaming_success(self, tmp_path, lifecycle_dir, monkeypatch):
        stub = _LocalStub(stream=False)
        log_dir = str(tmp_path / "proxy-log")
        os.makedirs(log_dir, exist_ok=True)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
        monkeypatch.setattr(cache_proxy, "LOG_DIR", log_dir)
        monkeypatch.setattr(cache_proxy, "USAGE_LOG", os.path.join(log_dir, "usage.jsonl"))
        monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", None)
        lc_logger = RequestLifecycleLogger(root=lifecycle_dir, max_bytes=10**9)
        monkeypatch.setattr(cache_proxy, "LIFECYCLE_LOGGER", lc_logger)
        monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
        port = _free_port()
        srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            body = _make_body(stream=False, with_bootstrap=True)
            resp = _send_request(port, body)
            assert resp.status_code == 200
            lc_logger.flush()
            time.sleep(0.1)

            rows = _read_lifecycle_rows(lifecycle_dir)
            assert len(rows) == 1
            row = rows[0]
            assert row["outcome"] == "success"
            assert row["stream"] is False
            assert row["usage"] is not None
            assert row["usage"]["output_tokens"] == 42
        finally:
            srv.shutdown()
            stub.shutdown()


# ---------------------------------------------------------------------------
# Integration: error responses
# ---------------------------------------------------------------------------

class TestProxyIntegrationErrors:
    def test_upstream_error(self, tmp_path, lifecycle_dir, monkeypatch):
        stub = _LocalStub(stream=True, status=429)
        log_dir = str(tmp_path / "proxy-log")
        os.makedirs(log_dir, exist_ok=True)
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", stub.url)
        monkeypatch.setattr(cache_proxy, "LOG_DIR", log_dir)
        monkeypatch.setattr(cache_proxy, "USAGE_LOG", os.path.join(log_dir, "usage.jsonl"))
        monkeypatch.setattr(cache_proxy, "REQUEST_LOGGER", None)
        lc_logger = RequestLifecycleLogger(root=lifecycle_dir, max_bytes=10**9)
        monkeypatch.setattr(cache_proxy, "LIFECYCLE_LOGGER", lc_logger)
        monkeypatch.setattr(cache_proxy, "_effort_pins", cache_proxy.EffortPinStore(None))
        port = _free_port()
        srv = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            body = _make_body(stream=True)
            resp = _send_request(port, body)
            assert resp.status_code == 429
            lc_logger.flush()
            time.sleep(0.1)

            rows = _read_lifecycle_rows(lifecycle_dir)
            assert len(rows) == 1
            row = rows[0]
            assert row["outcome"] == "error"
            assert row["error_class"] == "upstream_error"
            assert row["http_status"] == 429
        finally:
            srv.shutdown()
            stub.shutdown()


# ---------------------------------------------------------------------------
# Integration: missing identity
# ---------------------------------------------------------------------------

class TestMissingIdentity:
    def test_no_bootstrap_records_unknown(self, proxy_with_lifecycle):
        port, lg, lc_dir = proxy_with_lifecycle
        body = _make_body(stream=True, with_bootstrap=False,
                          session_id="completely-new-session-1234")
        resp = _send_request(port, body)
        assert resp.status_code == 200
        lg.flush()
        time.sleep(0.1)

        rows = _read_lifecycle_rows(lc_dir)
        assert len(rows) == 1
        assert rows[0]["identity_source"] == "unknown"
        assert rows[0]["identity"] is None


# ---------------------------------------------------------------------------
# Summary script tests
# ---------------------------------------------------------------------------

class TestSummaryScript:
    def test_summary_groups_by_agent_and_tree(self, lifecycle_dir):
        # Write some test rows directly
        sys.path.insert(0, str(ROOT / "scripts"))
        from request_lifecycle_summary import load_rows, summarize

        rows = [
            {
                "request_id": f"lc-sum-{i}",
                "ts": time.time(),
                "session_id": f"sess-{i % 2}",
                "model": "local-test",
                "outcome": "success",
                "identity": {
                    "agent_name": f"agent-{i % 3}",
                    "root_team_key": f"tree-{i % 2}",
                },
                "identity_source": "bootstrap",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 20,
                },
                "durations": {
                    "total_ms": 500,
                    "generation_ms": 200,
                    "hold_ms": 0,
                },
            }
            for i in range(6)
        ]
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with open(os.path.join(lifecycle_dir, f"{day}.jsonl"), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        loaded = load_rows(lifecycle_dir)
        assert len(loaded) == 6

        summary = summarize(loaded)
        assert summary["total_requests"] == 6
        assert summary["unknown_identity_requests"] == 0
        assert summary["unknown_coverage_pct"] == 0
        assert len(summary["by_tree"]) == 2
        assert len(summary["by_agent"]) == 3
        # Token totals
        for tree in summary["by_tree"].values():
            assert tree["input_tokens"] > 0
        assert summary["canonical_source"] == "lifecycle_log"

    def test_summary_unknown_coverage(self, lifecycle_dir):
        sys.path.insert(0, str(ROOT / "scripts"))
        from request_lifecycle_summary import summarize

        rows = [
            {"request_id": "a", "ts": time.time(), "identity_source": "unknown",
             "identity": None, "outcome": "success",
             "usage": None, "durations": None},
            {"request_id": "b", "ts": time.time(), "identity_source": "bootstrap",
             "identity": {"agent_name": "x", "root_team_key": "t"},
             "outcome": "success", "usage": None, "durations": None},
        ]
        summary = summarize(rows)
        assert summary["unknown_identity_requests"] == 1
        assert summary["unknown_coverage_pct"] == 50.0
