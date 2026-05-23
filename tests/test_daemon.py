"""Tests for obs_agent.daemon - HTTP API contract.

- GET /health returns status
- POST /chat accepts messages and returns responses
- POST /chat/stream returns SSE events
- Session management (init, resume)
- Error handling for invalid requests

Uses FastAPI TestClient for synchronous testing.
Mock pattern: patch SessionManager.get_client to return a mock ClaudeSDKClient.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock
from fastapi.testclient import TestClient

from obs_agent.daemon import ChatRequest, ChatResponse, create_app, create_default_app


def _make_mock_client(messages: list) -> AsyncMock:
    """Create a mock ClaudeSDKClient that yields the given messages.

    Usage:
        mock_client = _make_mock_client([mock_msg1, mock_msg2])
        # mock_client.query(prompt) is a no-op AsyncMock
        # async for msg in mock_client.receive_response(): yields messages
    """
    client = AsyncMock()

    async def mock_receive():
        for msg in messages:
            yield msg

    client.receive_response = mock_receive
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    return client


# --- App Factory ---


class TestAppFactory:
    """create_app() builds a configured FastAPI application."""

    def test_create_app_returns_fastapi(self, config):
        """create_app returns a FastAPI app instance."""
        application = create_app(config)
        assert application is not None
        assert application.title == "OBS Agent"

    def test_create_app_has_health_route(self, config):
        """App has a /health endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/health" in routes

    def test_create_app_has_chat_route(self, config):
        """App has a /chat endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/chat" in routes

    def test_create_app_has_stream_route(self, config):
        """App has a /chat/stream endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/chat/stream" in routes

    def test_create_app_has_enqueue_route(self, config):
        """App has a /chat/enqueue endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/chat/enqueue" in routes

    def test_create_app_has_interrupt_route(self, config):
        """App has a /chat/interrupt endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/chat/interrupt" in routes

    def test_create_app_has_commands_route(self, config):
        """App has a /commands endpoint."""
        application = create_app(config)
        routes = [r.path for r in application.routes]
        assert "/commands" in routes

    @patch("obs_agent.config.OBSConfig.from_env")
    def test_create_default_app_uses_from_env(self, mock_from_env, config):
        """create_default_app() calls OBSConfig.from_env() and returns a configured app."""
        config.cache_proxy_enabled = False
        mock_from_env.return_value = config
        application = create_default_app()
        mock_from_env.assert_called_once()
        assert application is not None
        assert application.title == "OBS Agent"
        routes = [r.path for r in application.routes]
        assert "/health" in routes
        assert "/chat" in routes

    @patch("obs_agent.config.OBSConfig.from_env")
    def test_create_default_app_validates_config(self, mock_from_env, config):
        """create_default_app() calls config.validate() to fail fast on bad vault."""
        config.cache_proxy_enabled = False
        mock_from_env.return_value = config
        with patch.object(config, "validate") as mock_validate:
            create_default_app()
            mock_validate.assert_called_once()

    @patch("obs_agent.config.OBSConfig.from_env")
    def test_create_default_app_logs_startup_phases(self, mock_from_env, config, caplog):
        """create_default_app() emits bounded startup phase progress logs."""
        config.cache_proxy_enabled = False
        mock_from_env.return_value = config
        with caplog.at_level("INFO", logger="obs_agent.daemon"):
            create_default_app()

        messages = [record.getMessage() for record in caplog.records]
        assert any("startup phase_start component=http-daemon phase=bootstrap_runtime_env" in msg for msg in messages)
        assert any("startup phase_complete component=http-daemon phase=validate_config" in msg for msg in messages)
        assert any("startup complete component=http-daemon phase=startup" in msg for msg in messages)


# --- Health Endpoint ---


class TestHealthEndpoint:
    """GET /health returns service status."""

    def test_health_returns_ok(self, config):
        """Health check returns 200 with status ok."""
        application = create_app(config)
        client = TestClient(application)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_includes_version(self, config):
        """Health check includes the app version."""
        application = create_app(config)
        client = TestClient(application)
        response = client.get("/health")
        data = response.json()
        assert "version" in data


# --- Chat Endpoint ---


class TestChatEndpoint:
    """POST /chat accepts messages and returns responses."""

    @patch("obs_agent.session.SessionManager.get_client")
    def test_chat_accepts_message(self, mock_get_client, config):
        """POST /chat accepts a JSON message body."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello!")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "hi"})
        assert response.status_code == 200

    def test_chat_rejects_empty_message(self, config):
        """POST /chat rejects empty or missing message."""
        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={})
        assert response.status_code == 422 or response.status_code == 400

    @patch("obs_agent.session.SessionManager.get_client")
    def test_chat_returns_assistant_text(self, mock_get_client, config):
        """POST /chat returns the assistant's text response."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="I can help with that.")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "help me"})
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert len(data["response"]) > 0

    @patch("obs_agent.session.SessionManager.get_client")
    def test_chat_captures_session_id(self, mock_get_client, config):
        """POST /chat captures session_id from SDK messages."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello!")]
        mock_msg.session_id = "sess-new-123"

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "hi"})
        data = response.json()
        assert data.get("session_id") == "sess-new-123"

    @patch("obs_agent.session.SessionManager.get_client")
    def test_chat_touches_session_activity(self, mock_get_client, config):
        """POST /chat updates session last_activity timestamp."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Done.")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        client.post("/chat", json={"message": "test"})
        assert application.state.session_manager.last_activity is not None


# --- SSE Streaming ---


class TestChatStream:
    """POST /chat/stream returns Server-Sent Events."""

    @patch("obs_agent.session.SessionManager.get_client")
    def test_stream_returns_sse(self, mock_get_client, config):
        """POST /chat/stream returns text/event-stream content type."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Streaming!")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")

    @patch("obs_agent.session.SessionManager.get_client")
    def test_stream_contains_data_events(self, mock_get_client, config):
        """SSE stream contains data: lines with assistant content."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="chunk1")]
        mock_msg.session_id = None
        mock_msg2 = MagicMock()
        mock_msg2.content = [TextBlock(text="chunk2")]
        mock_msg2.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg, mock_msg2])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        body = response.text
        assert "data: chunk1" in body
        assert "data: chunk2" in body

    @patch("obs_agent.session.SessionManager.get_client")
    def test_stream_ends_with_done(self, mock_get_client, config):
        """SSE stream ends with [DONE] marker."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        assert "[DONE]" in response.text


# --- SSE Status Events ---


class TestStreamStatusEvents:
    """SSE stream includes status events for tool use and thinking."""

    @patch("obs_agent.session.SessionManager.get_client")
    def test_tool_use_status_event(self, mock_get_client, config):
        """SSE stream emits event: status for tool use blocks."""
        mock_msg = MagicMock()
        mock_msg.content = [
            ToolUseBlock(id="tu-1", name="Read", input={"file_path": "/tmp/test.md"}),
            TextBlock(text="File contents here."),
        ]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "read a file"})
        body = response.text

        assert "event: status" in body
        # Should have a tool_use status event
        assert '"type":"tool_use"' in body or '"type": "tool_use"' in body

    @patch("obs_agent.session.SessionManager.get_client")
    def test_thinking_status_event(self, mock_get_client, config):
        """SSE stream emits event: status for thinking blocks."""
        mock_msg = MagicMock()
        mock_msg.content = [
            ThinkingBlock(thinking="Let me think...", signature="sig"),
            TextBlock(text="Here is my answer."),
        ]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "think about this"})
        body = response.text

        assert "event: status" in body
        assert '"type":"thinking"' in body or '"type": "thinking"' in body

    @patch("obs_agent.session.SessionManager.get_client")
    def test_tool_use_summary_in_status(self, mock_get_client, config):
        """tool_use status event includes summarized tool info."""
        mock_msg = MagicMock()
        mock_msg.content = [
            ToolUseBlock(id="tu-1", name="Grep", input={"pattern": "hello"}),
            TextBlock(text="Found results."),
        ]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "search"})
        body = response.text

        # The summary should include the structured tool description
        assert "Grep:" in body
        assert "hello" in body

    @patch("obs_agent.session.SessionManager.get_client")
    def test_status_events_mixed_with_text(self, mock_get_client, config):
        """Status events and text data events coexist in the stream."""
        mock_msg = MagicMock()
        mock_msg.content = [
            ToolUseBlock(id="tu-1", name="Read", input={"file_path": "/tmp/x"}),
            TextBlock(text="The answer is 42."),
        ]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "test"})
        body = response.text

        # Both text data and status events should be present
        assert "data: The answer is 42." in body
        assert "event: status" in body
        assert "[DONE]" in body


# --- Session Integration ---


class TestDaemonSession:
    """Daemon integrates with SessionManager for session lifecycle."""

    def test_app_state_has_session_manager(self, config):
        """App state includes a SessionManager instance."""
        application = create_app(config)
        assert hasattr(application.state, "session_manager")

    def test_app_state_has_config(self, config):
        """App state includes the config."""
        application = create_app(config)
        assert hasattr(application.state, "config")
        assert application.state.config is config

    @patch("obs_agent.session.SessionManager.get_client")
    def test_session_resume_after_activity(self, mock_get_client, config):
        """Second chat message uses session resume if within cache window."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Response")]
        mock_msg.session_id = "sess-persist-1"

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)

        # First message sets the session_id
        client.post("/chat", json={"message": "first"})

        # Reset mock to capture second call
        mock_get_client.return_value = _make_mock_client([mock_msg])

        # Second message should use resume
        client.post("/chat", json={"message": "second"})

        # The session manager should have a session_id
        assert application.state.session_manager.session_id == "sess-persist-1"


# --- Request Models ---


class TestRequestModels:
    """Pydantic models for request/response validation."""

    def test_chat_request_requires_message(self):
        """ChatRequest requires a non-empty message field."""
        with pytest.raises(Exception):
            ChatRequest(message="")

    def test_chat_request_valid(self):
        """ChatRequest accepts valid messages."""
        req = ChatRequest(message="hello")
        assert req.message == "hello"

    def test_chat_response_model(self):
        """ChatResponse includes response and optional session_id."""
        resp = ChatResponse(response="hello back", session_id="sess-1")
        assert resp.response == "hello back"
        assert resp.session_id == "sess-1"


# --- Enqueue Endpoint ---


class TestEnqueueEndpoint:
    """POST /chat/enqueue queues messages for hook injection."""

    def test_enqueue_returns_200(self, config):
        """POST /chat/enqueue returns 200 with queued status."""
        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/enqueue", json={"message": "hello"})
        assert response.status_code == 200
        data = response.json()
        assert data["queued"] is True
        assert data["queue_size"] == 1

    def test_enqueue_rejects_empty(self, config):
        """POST /chat/enqueue rejects empty message."""
        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/enqueue", json={"message": ""})
        assert response.status_code == 422

    def test_enqueue_rejects_missing(self, config):
        """POST /chat/enqueue rejects missing message field."""
        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/enqueue", json={})
        assert response.status_code == 422

    def test_multiple_enqueues_accumulate(self, config):
        """Multiple enqueues increment queue size."""
        application = create_app(config)
        client = TestClient(application)
        client.post("/chat/enqueue", json={"message": "first"})
        response = client.post("/chat/enqueue", json={"message": "second"})
        data = response.json()
        assert data["queue_size"] == 2

    def test_enqueue_populates_hook_state(self, config):
        """Enqueued messages are in the hook state queue."""
        application = create_app(config)
        client = TestClient(application)
        client.post("/chat/enqueue", json={"message": "test msg"})
        assert application.state.hook_state.message_queue.qsize() == 1


# --- Interrupt Endpoint ---


class TestInterruptEndpoint:
    """POST /chat/interrupt sets the interrupt flag."""

    def test_interrupt_returns_200(self, config):
        """POST /chat/interrupt returns 200."""
        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/interrupt")
        assert response.status_code == 200
        data = response.json()
        assert data["interrupted"] is True

    def test_interrupt_sets_flag(self, config):
        """POST /chat/interrupt sets the hook state interrupt flag."""
        application = create_app(config)
        client = TestClient(application)
        client.post("/chat/interrupt")
        assert application.state.hook_state.interrupt_flag is True

    def test_interrupt_idempotent(self, config):
        """Multiple interrupts are idempotent."""
        application = create_app(config)
        client = TestClient(application)
        client.post("/chat/interrupt")
        client.post("/chat/interrupt")
        assert application.state.hook_state.interrupt_flag is True


# --- Commands Endpoint ---


class TestCommandsEndpoint:
    """GET /commands lists available commands."""

    def test_commands_returns_200(self, config):
        """GET /commands returns 200."""
        application = create_app(config)
        client = TestClient(application)
        response = client.get("/commands")
        assert response.status_code == 200

    def test_commands_returns_list(self, config):
        """GET /commands returns list of commands."""
        application = create_app(config)
        client = TestClient(application)
        response = client.get("/commands")
        data = response.json()
        assert "commands" in data
        names = [c["name"] for c in data["commands"]]
        assert "stop" in names
        assert "quit" in names
        assert "enqueue" in names


# --- App State Wiring ---


class TestAppStateWiring:
    """create_app wires HookState and CommandRegistry into app state."""

    def test_app_state_has_hook_state(self, config):
        """App state includes a HookState instance."""
        application = create_app(config)
        assert hasattr(application.state, "hook_state")

    def test_app_state_has_commands(self, config):
        """App state includes a CommandRegistry instance."""
        application = create_app(config)
        assert hasattr(application.state, "commands")

    def test_session_manager_shares_hook_state(self, config):
        """SessionManager uses the same HookState as app.state."""
        application = create_app(config)
        assert application.state.session_manager.hook_state is application.state.hook_state


# --- Queue Continuation ---


class TestQueueContinuation:
    """Queued messages are processed inline via continuation loop."""

    @patch("obs_agent.session.SessionManager.get_client")
    def test_stream_processes_queued_messages_inline(self, mock_get_client, config):
        """Queued messages get processed in the same SSE stream."""
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            if call_count == 1:
                mock_msg.content = [TextBlock(text="Main response")]
            else:
                mock_msg.content = [TextBlock(text="Continuation response")]
            mock_msg.session_id = None
            yield mock_msg

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        mock_get_client.return_value = mock_client

        application = create_app(config)
        # Pre-populate the queue BEFORE making the request
        application.state.hook_state.message_queue.put_nowait("queued msg")

        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hello"})
        body = response.text

        assert "queue_delivered" in body
        assert "Continuation response" in body

    @patch("obs_agent.session.SessionManager.get_client")
    def test_no_continuation_when_queue_empty(self, mock_get_client, config):
        """No continuation loop when queue is empty."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Only response")]
        mock_msg.session_id = None

        mock_get_client.return_value = _make_mock_client([mock_msg])

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hello"})
        body = response.text

        # Should have main response but no continuation
        assert "Only response" in body
        # get_client should only be called once
        assert mock_get_client.call_count == 1
