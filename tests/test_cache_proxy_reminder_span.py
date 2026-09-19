"""Unit tests for span-level, recursive reminder stripping (+ D2, F1).

Offline: no network, no GPU, no gate, no Anthropic call. The F1 routing tests
use stub HTTP servers on loopback, in the style of
``tests/test_cache_proxy_local_routing.py``.

Covers the takeover-1 Step-1 fix:

  (a) rule 3 becomes span-level and recursive — a reminder that shares a block
      with real content no longer takes the content with it, and reminders
      nested inside ``tool_result`` content (the dominant remaining
      cache-divergence class) are stripped too;
  (b) ``parse_sse_usage`` merges ``message_start`` and ``message_delta`` usage
      so streaming local turns log a real ``cache_read`` (D2);
  (c) ``_forward_simple`` picks its upstream from the request credential so a
      local session's non-``/v1/messages`` request never reaches Anthropic
      carrying the gate token (F1).
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


# The literal tag, assembled so this file can be read into a prompt safely.
OPEN = "<" + "system-reminder" + ">"
CLOSE = "</" + "system-reminder" + ">"


def _reminder(body_text="changed_files: a.py, b.py"):
    return f"{OPEN}\n{body_text}\n{CLOSE}"


def _user(content):
    return {"role": "user", "content": content}


def _text(text):
    return {"type": "text", "text": text}


def _tool_result(content, tool_use_id="toolu_1"):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _body(messages):
    return {"model": "claude-sonnet-4-6", "messages": messages}


@pytest.fixture(autouse=True)
def reset_stats():
    for key in cache_proxy.stats:
        cache_proxy.stats[key] = 0


def test_module_under_test_comes_from_this_checkout():
    """Guard: the editable install points at /workspace/obs/src, so a worktree
    run must still import the worktree's own copy."""
    expected = Path(__file__).resolve().parents[1] / "src" / "cache_proxy.py"
    assert Path(cache_proxy.__file__).resolve() == expected


# ── (a) span-level stripping ──────────────────────────────────────────────


class TestPureReminderBlockIsUnchangedFromBefore:
    """Golden case: a block that is nothing but a reminder must still vanish,
    byte for byte, so existing hosted cache prefixes do not shift."""

    def test_pure_reminder_block_is_dropped(self):
        body = _body([_user([_text("real prompt"), _text(_reminder())])])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 1
        assert body["messages"][0]["content"] == [_text("real prompt")]

    def test_pure_reminder_with_surrounding_whitespace_is_dropped(self):
        body = _body([_user([_text("real prompt"), _text(f"\n\n{_reminder()}\n")])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"] == [_text("real prompt")]

    def test_output_bytes_match_the_old_block_level_behaviour(self):
        """Same serialized bytes as a hand-built body with the block removed."""
        with_reminder = _body([
            _user([_text("alpha"), _text(_reminder()), _text("beta")]),
        ])
        without = _body([_user([_text("alpha"), _text("beta")])])
        cache_proxy.strip_all_system_reminders(with_reminder)
        assert json.dumps(with_reminder, separators=(",", ":")) == json.dumps(
            without, separators=(",", ":")
        )


class TestMixedTextBlock:
    def test_span_removed_prose_kept(self):
        text = f"keep this\n\n{_reminder()}"
        body = _body([_user([_text(text)])])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 1
        assert body["messages"][0]["content"] == [_text("keep this")]

    def test_prose_on_both_sides_is_kept(self):
        text = f"before{_reminder()}after"
        body = _body([_user([_text(text)])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"][0]["text"] == "beforeafter"

    def test_multiple_spans_in_one_block(self):
        text = f"head\n\n{_reminder('one')}\n\n{_reminder('two')}"
        body = _body([_user([_text(text)])])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 2
        assert body["messages"][0]["content"][0]["text"] == "head"

    def test_unterminated_opening_tag_strips_to_end_of_that_block_only(self):
        body = _body([
            _user([_text(f"keep me\n\n{OPEN}\ntruncated reminder"), _text("sibling")]),
        ])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 1
        assert body["messages"][0]["content"] == [_text("keep me"), _text("sibling")]

    def test_closing_tag_alone_is_not_touched(self):
        text = f"prose {CLOSE} more"
        body = _body([_user([_text(text)])])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 0
        assert body["messages"][0]["content"] == [_text(text)]

    def test_text_without_any_tag_is_byte_identical(self):
        text = "   indented and  \n trailing spaces   \n"
        body = _body([_user([_text(text)])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"] == [_text(text)]


class TestToolResultStringContent:
    """The measured class-2/class-3 shape from the real captured bodies."""

    def test_embedded_span_removed_from_string_content(self):
        live = (
            "File content (25142 tokens) exceeds maximum allowed tokens (25000)."
            f"\n\n{_reminder('The task tools have not been used recently.')}"
        )
        replayed = "File content (25142 tokens) exceeds maximum allowed tokens (25000)."
        live_body = _body([_user([_tool_result(live)])])
        replayed_body = _body([_user([_tool_result(replayed)])])
        assert cache_proxy.strip_all_system_reminders(live_body) == 1
        assert cache_proxy.strip_all_system_reminders(replayed_body) == 0
        assert live_body == replayed_body

    def test_leading_whitespace_skew_converges(self):
        """Live form is trimmed, replayed form keeps the Read tool's leading
        spaces; both carry a reminder, so both get the same deterministic trim."""
        payload = "   151-> some file content"
        live = f"{payload.lstrip()}\n\n{_reminder('a')}\n\n{_reminder('b')}"
        replayed = f"{payload}\n\n{_reminder('b')}\n"
        live_body = _body([_user([_tool_result(live)])])
        replayed_body = _body([_user([_tool_result(replayed)])])
        cache_proxy.strip_all_system_reminders(live_body)
        cache_proxy.strip_all_system_reminders(replayed_body)
        assert live_body == replayed_body
        assert live_body["messages"][0]["content"][0]["content"] == payload.lstrip()

    def test_tool_result_that_empties_keeps_a_non_empty_placeholder(self):
        body = _body([_user([_tool_result(_reminder())])])
        cache_proxy.strip_all_system_reminders(body)
        content = body["messages"][0]["content"][0]["content"]
        assert content == cache_proxy.REMINDER_PLACEHOLDER
        assert content.strip()

    def test_tool_result_without_a_tag_is_untouched(self):
        payload = "  leading and trailing  "
        body = _body([_user([_tool_result(payload)])])
        assert cache_proxy.strip_all_system_reminders(body) == 0
        assert body["messages"][0]["content"][0]["content"] == payload


class TestToolResultListContent:
    def test_nested_text_block_span_removed(self):
        nested = [_text(f"real output\n\n{_reminder()}"), {"type": "image", "source": {}}]
        body = _body([_user([_tool_result(nested)])])
        count = cache_proxy.strip_all_system_reminders(body)
        assert count == 1
        content = body["messages"][0]["content"][0]["content"]
        assert content[0] == _text("real output")
        assert content[1] == {"type": "image", "source": {}}

    def test_nested_pure_reminder_block_is_dropped(self):
        nested = [_text("kept"), _text(_reminder())]
        body = _body([_user([_tool_result(nested)])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"][0]["content"] == [_text("kept")]

    def test_nested_list_that_empties_gets_a_placeholder_block(self):
        body = _body([_user([_tool_result([_text(_reminder())])])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"][0]["content"] == [
            _text(cache_proxy.REMINDER_PLACEHOLDER)
        ]


class TestNonTextBlocksUntouched:
    def test_image_and_tool_use_blocks_survive_byte_identical(self):
        image = {"type": "image", "source": {"type": "base64", "data": "AAA"}}
        tool_use = {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}}
        body = _body([_user([image, tool_use, _text(_reminder())])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][0]["content"] == [image, tool_use]

    def test_assistant_messages_are_never_inspected(self):
        assistant = {"role": "assistant", "content": [_text(_reminder())]}
        body = _body([assistant])
        assert cache_proxy.strip_all_system_reminders(body) == 0
        assert body["messages"][0] == assistant


class TestMessageSurvival:
    def test_only_message_survives_with_a_placeholder(self):
        body = _body([_user([_text(_reminder())])])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"] != []
        assert body["messages"][0]["content"] == [
            _text(cache_proxy.REMINDER_PLACEHOLDER)
        ]
        assert cache_proxy.stats["messages_placeholdered"] == 1

    def test_conversation_still_ends_on_a_user_turn(self):
        body = _body([
            _user([_text("first")]),
            {"role": "assistant", "content": [_text("reply")]},
            _user([_text(_reminder())]),
        ])
        cache_proxy.strip_all_system_reminders(body)
        assert body["messages"][-1]["role"] == "user"
        assert body["messages"][-1]["content"][0]["text"].strip()

    def test_message_indices_stay_aligned_between_live_and_replayed_forms(self):
        """The live form carries more reminder blocks than the replayed form;
        both must keep the same number of messages so nothing after them shifts."""
        live = _body([
            _user([_text(_reminder("a")), _text(_reminder("b")), _text(_reminder("c"))]),
            {"role": "assistant", "content": [_text("reply")]},
            _user([_text("tail")]),
        ])
        replayed = _body([
            _user([_text(_reminder("a"))]),
            {"role": "assistant", "content": [_text("reply")]},
            _user([_text("tail")]),
        ])
        cache_proxy.strip_all_system_reminders(live)
        cache_proxy.strip_all_system_reminders(replayed)
        assert len(live["messages"]) == len(replayed["messages"]) == 3
        assert live == replayed

    def test_message_with_empty_content_list_is_left_alone(self):
        body = _body([_user([])])
        assert cache_proxy.strip_all_system_reminders(body) == 0
        assert body["messages"][0]["content"] == []


class TestCountTokensBodyShape:
    """D1: a count_tokens body is one user message whose content is a plain
    string. Rule 2 makes it a single text block; rule 3 must not empty it."""

    def test_string_content_mentioning_the_tag_survives_normalization(self):
        prose = (
            "Summarise this log line: the proxy strips "
            f"{_reminder('injected')} from user messages."
        )
        body = {"model": "claude-sonnet-4-6", "messages": [_user(prose)], "tools": []}
        normalized, info = cache_proxy.normalize_request(body)
        assert len(normalized["messages"]) == 1
        content = normalized["messages"][0]["content"]
        assert content and content[0]["text"].strip()
        assert OPEN not in content[0]["text"]
        assert info["reminders"] == 1

    def test_string_content_that_is_only_a_reminder_still_yields_a_message(self):
        body = {"model": "claude-sonnet-4-6", "messages": [_user(_reminder())]}
        normalized, _ = cache_proxy.normalize_request(body)
        assert len(normalized["messages"]) == 1
        assert normalized["messages"][0]["content"] == [
            _text(cache_proxy.REMINDER_PLACEHOLDER)
        ]


class TestIdempotence:
    def test_normalizing_twice_is_byte_stable(self):
        def make():
            return {
                "model": "claude-sonnet-4-6",
                "messages": [
                    _user([
                        _text(f"prose\n\n{_reminder()}"),
                        _tool_result(f"  out  \n\n{_reminder('x')}"),
                        _tool_result([_text(f"nested\n{_reminder('y')}")]),
                        _text(_reminder("pure")),
                    ]),
                    {"role": "assistant", "content": [_text("ok")]},
                    _user([_text(_reminder("last"))]),
                ],
            }

        once, _ = cache_proxy.normalize_request(make())
        twice_body, _ = cache_proxy.normalize_request(make())
        twice, _ = cache_proxy.normalize_request(twice_body)
        assert json.dumps(once, separators=(",", ":")) == json.dumps(
            twice, separators=(",", ":")
        )

    def test_second_pass_removes_nothing(self):
        body = _body([_user([_text(f"prose {_reminder()}")])])
        assert cache_proxy.strip_all_system_reminders(body) == 1
        assert cache_proxy.strip_all_system_reminders(body) == 0


# ── (b) D2: SSE usage merging ─────────────────────────────────────────────


def _sse(*events):
    return [
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    ]


class TestParseSseUsage:
    def test_message_start_only_is_unchanged(self):
        chunks = _sse({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5,
                                  "cache_read_input_tokens": 800,
                                  "cache_creation_input_tokens": 0}},
        })
        assert cache_proxy.parse_sse_usage(chunks) == {
            "input_tokens": 5,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        }

    def test_delta_split_overrides_message_start(self):
        """The gate's shape: whole prompt as input_tokens up front, the cache
        split only in message_delta."""
        chunks = _sse(
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 90531,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
            {"type": "content_block_delta", "delta": {"text": "hi"}},
            {"type": "message_delta",
             "usage": {"input_tokens": 483,
                       "cache_read_input_tokens": 90048,
                       "cache_creation_input_tokens": 0,
                       "output_tokens": 12}},
        )
        usage = cache_proxy.parse_sse_usage(chunks)
        assert usage["cache_read_input_tokens"] == 90048
        assert usage["input_tokens"] == 483
        assert usage["output_tokens"] == 12

    def test_keys_absent_from_the_delta_are_preserved(self):
        chunks = _sse(
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 100,
                                   "cache_creation_input_tokens": 2400}}},
            {"type": "message_delta", "usage": {"output_tokens": 7}},
        )
        usage = cache_proxy.parse_sse_usage(chunks)
        assert usage == {"input_tokens": 100,
                         "cache_creation_input_tokens": 2400,
                         "output_tokens": 7}

    def test_no_usage_events_returns_empty(self):
        chunks = _sse({"type": "ping"})
        assert cache_proxy.parse_sse_usage(chunks) == {}

    def test_malformed_data_lines_are_skipped(self):
        chunks = [b"data: not json\n\n"] + _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}
        )
        assert cache_proxy.parse_sse_usage(chunks) == {"input_tokens": 3}


# ── (c) F1: _forward_simple routing ───────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_stub(recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _record_and_reply(self, method):
            length = int(self.headers.get("Content-Length", 0) or 0)
            recorder.append({
                "method": method,
                "path": self.path,
                "raw_body": self.rfile.read(length) if length else b"",
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._record_and_reply("GET")

        def do_POST(self):
            self._record_and_reply("POST")

        def log_message(self, *a):
            pass

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server.shutdown


def _run_proxy():
    port = _free_port()
    server = cache_proxy.ThreadedHTTPServer(("127.0.0.1", port), cache_proxy.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server.shutdown


@pytest.fixture
def stubs(monkeypatch):
    local_seen, hosted_seen = [], []
    local_url, stop_local = _make_stub(local_seen)
    hosted_url, stop_hosted = _make_stub(hosted_seen)
    monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", local_url)
    monkeypatch.setattr(cache_proxy, "ANTHROPIC_UPSTREAM", hosted_url)
    monkeypatch.setattr(cache_proxy, "LOCAL_AUTH_TOKEN", "unit-test-local-token")
    proxy_url, stop_proxy = _run_proxy()
    try:
        yield proxy_url, local_seen, hosted_seen
    finally:
        stop_proxy()
        stop_local()
        stop_hosted()


class TestForwardSimpleRouting:
    def test_local_credential_routes_to_the_local_upstream(self, stubs):
        proxy_url, local_seen, hosted_seen = stubs
        resp = httpx.get(f"{proxy_url}/v1/models",
                         headers={"Authorization": "Bearer unit-test-local-token"},
                         timeout=10)
        assert resp.status_code == 200
        assert len(local_seen) == 1 and not hosted_seen
        assert local_seen[0]["path"] == "/v1/models"
        # headers reach the gate untouched
        assert local_seen[0]["headers"]["authorization"] == "Bearer unit-test-local-token"

    def test_local_credential_via_x_api_key_routes_local(self, stubs):
        proxy_url, local_seen, hosted_seen = stubs
        httpx.get(f"{proxy_url}/v1/models",
                  headers={"x-api-key": "unit-test-local-token"}, timeout=10)
        assert len(local_seen) == 1 and not hosted_seen

    def test_hosted_credential_keeps_previous_behaviour(self, stubs):
        proxy_url, local_seen, hosted_seen = stubs
        resp = httpx.get(f"{proxy_url}/v1/models",
                         headers={"x-api-key": "sk-ant-hosted"}, timeout=10)
        assert resp.status_code == 200
        assert len(hosted_seen) == 1 and not local_seen

    def test_no_credential_keeps_previous_behaviour(self, stubs):
        proxy_url, local_seen, hosted_seen = stubs
        httpx.get(f"{proxy_url}/v1/models", timeout=10)
        assert len(hosted_seen) == 1 and not local_seen

    def test_local_credential_with_unconfigured_local_upstream_is_502(
        self, stubs, monkeypatch
    ):
        proxy_url, local_seen, hosted_seen = stubs
        monkeypatch.setattr(cache_proxy, "LOCAL_UPSTREAM", "")
        resp = httpx.get(f"{proxy_url}/v1/models",
                         headers={"Authorization": "Bearer unit-test-local-token"},
                         timeout=10)
        assert resp.status_code == 502
        # fail closed: the gate credential never reached the hosted upstream
        assert not hosted_seen and not local_seen

    def test_empty_local_token_never_matches(self, stubs, monkeypatch):
        proxy_url, local_seen, hosted_seen = stubs
        monkeypatch.setattr(cache_proxy, "LOCAL_AUTH_TOKEN", "")
        httpx.get(f"{proxy_url}/v1/models", headers={"x-api-key": ""}, timeout=10)
        assert len(hosted_seen) == 1 and not local_seen

    def test_health_is_still_served_locally(self, stubs):
        proxy_url, local_seen, hosted_seen = stubs
        resp = httpx.get(f"{proxy_url}/health", timeout=10)
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
        assert not local_seen and not hosted_seen
