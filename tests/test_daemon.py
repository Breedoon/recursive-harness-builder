"""Tests for obs_agent.daemon - Steps 9-10 TDD (RED phase).

These tests define the Daemon HTTP API contract:
- GET /health returns status
- POST /chat accepts messages and streams SSE responses
- Session management (init, resume)
- Error handling for invalid requests

Uses FastAPI TestClient for synchronous testing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from obs_agent.daemon import app, create_app


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
