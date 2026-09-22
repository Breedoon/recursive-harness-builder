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

import asyncio
import copy
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


class TestNativeHookJSONLFidelity:
    @pytest.mark.parametrize("event", ["PreToolUse:Bash", "PostToolUse:Bash", "SessionStart:startup", "FutureHook:unknown"])
    @pytest.mark.parametrize("location", ["text", "tool_string", "tool_list"])
    def test_transient_hook_context_is_stripped_in_all_locations(self, event, location):
        # Native additionalContext is absent from JSONL and cannot survive replay.
        text = _reminder(f"{event} hook additional context: [Queued message from user]: Revised instruction; read the inbox.")
        block = _text(text) if location == "text" else _tool_result(text if location == "tool_string" else [_text(text)])
        body = _body([_user([_text("persisted input"), block])])
        normalized, _ = cache_proxy.normalize_request(body)
        assert "hook additional context:" not in json.dumps(normalized)
        assert "persisted input" in json.dumps(normalized)
        again, _ = cache_proxy.normalize_request(copy.deepcopy(normalized))
        assert again == normalized

    def test_hook_context_does_not_exempt_colocated_reminders(self):
        live = _reminder("PostToolUse:Bash hook additional context: transient notice")
        text = "persisted input\n" + live + "\n" + _reminder()
        body = _body([_user([_text(text)])])
        assert cache_proxy.strip_all_system_reminders(body) == 2
        assert body["messages"][0]["content"] == [_text("persisted input")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    @pytest.mark.parametrize("delivery", ["transient_hook", "canonical_runner"])
    async def test_real_sdk_hook_parent_fork_and_resume_share_jsonl_prefix(self, tmp_path, monkeypatch, event, delivery):
        """Live CLI vs JSONL replay must normalize identically, with no provider calls."""
        from dataclasses import replace

        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
        from obs_agent.context_jsonl import find_session_jsonl
        from obs_agent.config import OBSConfig
        from obs_agent.hooks import HookState, create_hook_matchers
        from obs_agent.queueing import QueuedMessage
        from obs_agent.runner import ConversationRunner
        from obs_agent.session import SessionManager
        from unittest.mock import AsyncMock

        marker = "native-hook-wire: exact queued correction"
        captured = []
        state = HookState()
        state.message_queue.put_nowait(QueuedMessage(text=marker))

        def endpoint(path, body):
            if path.split("?")[0].endswith("/count_tokens"):
                return "application/json", json.dumps({"input_tokens": 100})
            if not path.split("?")[0].endswith("/messages"):
                return "application/json", "{}"
            normalized, _ = cache_proxy.normalize_request(copy.deepcopy(body))
            captured.append((body, normalized))
            has_result = any(
                block.get("type") == "tool_result" and block.get("tool_use_id") == "toolu_probe"
                for msg in body.get("messages", [])
                for block in (msg.get("content", []) if isinstance(msg.get("content"), list) else [])
            )
            start = {
                "id": f"msg_probe_{len(captured)}", "type": "message", "role": "assistant",
                "model": body.get("model", "claude-sonnet-4-6"), "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
            block = {"type": "text", "text": ""} if has_result else {"type": "tool_use", "id": "toolu_probe", "name": "Bash", "input": {}}
            delta = {"type": "text_delta", "text": "Done."} if has_result else {"type": "input_json_delta", "partial_json": json.dumps({"command": "true", "description": "Loopback protocol test"})}
            events = [
                ("message_start", {"type": "message_start", "message": start}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn" if has_result else "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 10}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            return "text/event-stream", "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)

        async def handle(reader, writer):
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                lines = header.split("\r\n")
                headers = {k.lower(): v for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
                data = await reader.readexactly(int(headers.get("content-length", "0")))
                content_type, text = endpoint(lines[0].split()[1], json.loads(data) if data else {})
                payload = text.encode()
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.delenv("CLAUDECODE", raising=False)
        config = OBSConfig(vault_path=tmp_path)

        async def transient_hook(hook_input, tool_use_id, context):
            # Historical negative control only; production hooks must not do this.
            notice = state.message_queue.get_nowait()
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice.text}}

        hooks = create_hook_matchers(config, state) if delivery == "canonical_runner" else {
            event: [HookMatcher(hooks=[transient_hook])],
        }
        options = ClaudeAgentOptions(
            cwd=str(tmp_path), model="claude-sonnet-4-6", max_turns=2,
            permission_mode="bypassPermissions", setting_sources=[], hooks=hooks,
            env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_API_KEY": "loopback-test-only", "ANTHROPIC_AUTH_TOKEN": "loopback-test-only", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"},
        )
        def without_cache_control(value):
            if isinstance(value, dict):
                return {key: without_cache_control(item) for key, item in value.items() if key != "cache_control"}
            if isinstance(value, list):
                return [without_cache_control(item) for item in value]
            return value

        try:
            session_id = None
            async with asyncio.timeout(90):
                async with ClaudeSDKClient(options=options) as client:
                    if delivery == "canonical_runner":
                        manager = SessionManager(config=config, hook_state=state)
                        manager.get_client = AsyncMock(return_value=client)
                        manager.recover_poisoned_session_if_needed = AsyncMock(return_value=None)
                        runner = ConversationRunner(manager, state, config)
                        events = [message async for message in runner.run("Run Bash true once and finish.")]
                        session_id = manager.session_id
                        assert runner.remaining_pending == []
                        assert sum(getattr(item, "type", None) == "queue_delivered" for item in events) == 1
                        assert marker not in json.dumps(captured[0][0])
                    else:
                        await client.query("Run Bash true once and finish.")
                        async for message in client.receive_response():
                            session_id = getattr(message, "session_id", None) or session_id
                assert session_id
                assert state.message_queue.empty()
                matching = [(raw, normalized) for raw, normalized in captured if marker in json.dumps(raw)]
                assert matching, "The CLI must submit the notice on the live wire"
                raw_parent, normalized_parent = matching[-1]
                session_path = find_session_jsonl(session_id=session_id, cwd=tmp_path)
                assert session_path is not None
                if delivery == "canonical_runner":
                    assert marker in json.dumps(normalized_parent)
                    assert "hook additional context:" not in json.dumps(raw_parent)
                    persisted = [json.loads(line) for line in session_path.read_text().splitlines()]
                    assert sum(record.get("type") == "user" and marker in json.dumps(record.get("message", {})) for record in persisted) == 1
                else:
                    assert f"{event}:Bash hook additional context:" in json.dumps(raw_parent)
                    assert marker not in json.dumps(normalized_parent)
                    assert marker not in session_path.read_text(), "Transient hooks are not canonical JSONL history"
                parent_prefix = without_cache_control(normalized_parent["messages"])

                for fork_session in (True, False):
                    capture_start = len(captured)
                    replay_options = replace(options, hooks={}, resume=session_id, fork_session=fork_session)
                    async with ClaudeSDKClient(options=replay_options) as replay:
                        await replay.query("Continue briefly without tools.")
                        async for _ in replay.receive_response():
                            pass
                    replay_requests = captured[capture_start:]
                    assert replay_requests
                    raw_replay, normalized_replay = replay_requests[0]
                    if delivery == "canonical_runner":
                        assert marker in json.dumps(raw_replay)
                        assert marker in json.dumps(normalized_replay)
                    else:
                        assert marker not in json.dumps(raw_replay)
                        # Keeping transient hooks would break the shared historical prefix.
                        assert without_cache_control(raw_parent["messages"]) != without_cache_control(raw_replay["messages"][:len(parent_prefix)])
                    assert parent_prefix == without_cache_control(normalized_replay["messages"][:len(parent_prefix)])
        finally:
            server.close()
            await server.wait_closed()


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
