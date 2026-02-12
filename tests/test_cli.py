"""Tests for obs_agent.cli - Step 10 TDD.

These tests define the CLI client contract:
- Entry point exists and is callable
- Sends messages to daemon HTTP API
- Displays responses from daemon
- Handles connection errors gracefully
- check_daemon hits /health
- start_daemon auto-starts the daemon subprocess
"""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from obs_agent.cli import check_daemon, main, send_message, start_daemon


# --- Entry Point ---


class TestCLIEntryPoint:
    """CLI has a callable main() entry point."""

    def test_main_exists(self):
        """main function exists and is callable."""
        assert callable(main)

    def test_send_message_exists(self):
        """send_message function exists and is callable."""
        assert callable(send_message)

    def test_check_daemon_exists(self):
        """check_daemon function exists and is callable."""
        assert callable(check_daemon)

    def test_start_daemon_exists(self):
        """start_daemon function exists and is callable."""
        assert callable(start_daemon)


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

    @patch("obs_agent.cli.httpx")
    def test_handles_non_200_status(self, mock_httpx, config):
        """send_message handles non-200 status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_httpx.post.return_value = mock_response

        result = send_message("hello", base_url=config.base_url)
        assert "error" in result.lower()


# --- Daemon Health Check ---


class TestCheckDaemon:
    """check_daemon() verifies the daemon is running."""

    @patch("obs_agent.cli.httpx")
    def test_returns_true_when_healthy(self, mock_httpx):
        """check_daemon returns True when /health returns 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx.get.return_value = mock_response

        assert check_daemon("http://127.0.0.1:7832") is True

    @patch("obs_agent.cli.httpx")
    def test_returns_false_on_error(self, mock_httpx):
        """check_daemon returns False when daemon is unreachable."""
        mock_httpx.get.side_effect = Exception("Connection refused")

        assert check_daemon("http://127.0.0.1:7832") is False

    @patch("obs_agent.cli.httpx")
    def test_returns_false_on_non_200(self, mock_httpx):
        """check_daemon returns False for non-200 responses."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_httpx.get.return_value = mock_response

        assert check_daemon("http://127.0.0.1:7832") is False


# --- CLI Script ---


class TestCLIScript:
    """CLI is runnable as a module."""

    def test_module_runnable(self):
        """obs_agent.cli module can be invoked with --help."""
        result = subprocess.run(
            [sys.executable, "-m", "obs_agent.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Should exit cleanly with help text
        assert result.returncode == 0
        assert "obs-agent" in result.stdout.lower() or "usage" in result.stdout.lower()

    def test_help_flag(self):
        """--help prints usage and exits."""
        result = subprocess.run(
            [sys.executable, "-m", "obs_agent.cli", "-h"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
