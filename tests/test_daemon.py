"""Tests for obs_agent.daemon - Steps 9-10 TDD.

These tests define the Daemon HTTP API contract:
- GET /health returns status
- POST /chat accepts messages and returns responses
- POST /chat/stream returns SSE events
- Session management (init, resume)
- Hook integration (UserPromptSubmit skill injection)
- Error handling for invalid requests

Uses FastAPI TestClient for synchronous testing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from obs_agent.daemon import ChatRequest, ChatResponse, app, create_app


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

    @patch("obs_agent.daemon.query")
    def test_chat_accepts_message(self, mock_query, config):
        """POST /chat accepts a JSON message body."""
        mock_msg = MagicMock()
        mock_msg.content = "Hello!"
        mock_msg.type = "assistant"
        mock_msg.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

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

    @patch("obs_agent.daemon.query")
    def test_chat_returns_assistant_text(self, mock_query, config):
        """POST /chat returns the assistant's text response."""
        mock_msg = MagicMock()
        mock_msg.content = "I can help with that."
        mock_msg.type = "assistant"
        mock_msg.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "help me"})
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert len(data["response"]) > 0

    @patch("obs_agent.daemon.query")
    def test_chat_captures_session_id(self, mock_query, config):
        """POST /chat captures session_id from SDK messages."""
        mock_msg = MagicMock()
        mock_msg.content = "Hello!"
        mock_msg.session_id = "sess-new-123"

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "hi"})
        data = response.json()
        assert data.get("session_id") == "sess-new-123"

    @patch("obs_agent.daemon.query")
    def test_chat_touches_session_activity(self, mock_query, config):
        """POST /chat updates session last_activity timestamp."""
        mock_msg = MagicMock()
        mock_msg.content = "Done."
        mock_msg.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        client.post("/chat", json={"message": "test"})
        assert application.state.session_manager.last_activity is not None


# --- SSE Streaming ---


class TestChatStream:
    """POST /chat/stream returns Server-Sent Events."""

    @patch("obs_agent.daemon.query")
    def test_stream_returns_sse(self, mock_query, config):
        """POST /chat/stream returns text/event-stream content type."""
        mock_msg = MagicMock()
        mock_msg.content = "Streaming!"
        mock_msg.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")

    @patch("obs_agent.daemon.query")
    def test_stream_contains_data_events(self, mock_query, config):
        """SSE stream contains data: lines with assistant content."""
        mock_msg = MagicMock()
        mock_msg.content = "chunk1"
        mock_msg.session_id = None
        mock_msg2 = MagicMock()
        mock_msg2.content = "chunk2"
        mock_msg2.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg
            yield mock_msg2

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        body = response.text
        assert "data: chunk1" in body
        assert "data: chunk2" in body

    @patch("obs_agent.daemon.query")
    def test_stream_ends_with_done(self, mock_query, config):
        """SSE stream ends with [DONE] marker."""
        mock_msg = MagicMock()
        mock_msg.content = "Hello"
        mock_msg.session_id = None

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)
        response = client.post("/chat/stream", json={"message": "hi"})
        assert "[DONE]" in response.text


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

    @patch("obs_agent.daemon.query")
    def test_session_resume_after_activity(self, mock_query, config):
        """Second chat message uses session resume if within cache window."""
        mock_msg = MagicMock()
        mock_msg.content = "Response"
        mock_msg.session_id = "sess-persist-1"

        async def mock_gen(*args, **kwargs):
            yield mock_msg

        mock_query.side_effect = mock_gen

        application = create_app(config)
        client = TestClient(application)

        # First message sets the session_id
        client.post("/chat", json={"message": "first"})

        # Reset mock to capture second call
        mock_query.side_effect = mock_gen

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
