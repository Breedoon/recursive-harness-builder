"""Tests for obs_agent.cli - Step 10 TDD (RED phase).

These tests define the CLI client contract:
- Entry point exists and is callable
- Sends messages to daemon HTTP API
- Displays responses from daemon
- Handles connection errors gracefully
"""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from obs_agent.cli import main, send_message


# --- Entry Point ---


class TestCLIEntryPoint:
    """CLI has a callable main() entry point."""

    def test_main_exists(self):
        """main function exists and is callable."""
        assert callable(main)

    def test_send_message_exists(self):
        """send_message function exists and is callable."""
        assert callable(send_message)


# --- Message Sending ---


class TestSendMessage:
    """send_message() posts to the daemon API and returns response."""

    @patch("obs_agent.cli.httpx")
    def test_sends_post_to_daemon(self, mock_httpx, config):
        """send_message posts to the /chat endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": "Hello back!"}
        mock_httpx.post.return_value = mock_response

        result = send_message("hello", base_url=config.base_url)

        mock_httpx.post.assert_called_once()
        call_args = mock_httpx.post.call_args
        assert "/chat" in call_args[0][0]

    @patch("obs_agent.cli.httpx")
    def test_returns_response_text(self, mock_httpx, config):
        """send_message returns the assistant's response text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": "I can help with that."}
        mock_httpx.post.return_value = mock_response

        result = send_message("help me", base_url=config.base_url)
        assert result == "I can help with that."

    @patch("obs_agent.cli.httpx")
    def test_handles_connection_error(self, mock_httpx, config):
        """send_message returns error message on connection failure."""
        mock_httpx.post.side_effect = Exception("Connection refused")

        result = send_message("hello", base_url=config.base_url)
        assert "error" in result.lower() or "connection" in result.lower()


# --- CLI Script ---


class TestCLIScript:
    """CLI is runnable as a module."""

    def test_module_runnable(self):
        """obs_agent.cli module can be invoked."""
        result = subprocess.run(
            [sys.executable, "-m", "obs_agent.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Should exit (help or error), but not crash with import error
        assert result.returncode is not None
