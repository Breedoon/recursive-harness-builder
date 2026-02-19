"""Tests for obs_agent.cli - Step 10 TDD.

These tests define the CLI client contract:
- Entry point exists and is callable
- Sends messages to daemon HTTP API
- Displays responses from daemon
- Handles connection errors gracefully
- check_daemon hits /health
- start_daemon auto-starts the daemon subprocess
- _render_status renders through channel abstraction
"""

import subprocess
import sys
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from obs_agent.cli import (
    _render_status,
    check_daemon,
    main,
    parse_slash_command,
    send_message,
    start_daemon,
    stream_message,
    stream_with_input,
)
from obs_agent.input import SimpleChannel


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


# --- Stream Message ---


class TestStreamMessage:
    """stream_message() streams SSE from daemon and prints tokens."""

    def test_stream_message_exists(self):
        """stream_message function exists and is callable."""
        assert callable(stream_message)

    @pytest.mark.asyncio
    async def test_handles_connection_error(self, config, capsys):
        """stream_message prints error on connection failure."""
        channel = SimpleChannel()
        await stream_message("hello", base_url="http://127.0.0.1:19999", channel=channel)
        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "connection" in captured.out.lower()

    @pytest.mark.asyncio
    async def test_handles_non_200_status(self, config, capsys):
        """stream_message prints error for non-200 status."""
        import httpx as real_httpx

        # Mock the async client to return a non-200 response
        class FakeResponse:
            status_code = 500
            async def aiter_lines(self):
                return
                yield  # make it an async generator

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeStream:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *args):
                pass

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def stream(self, *args, **kwargs):
                return FakeStream()

        channel = SimpleChannel()
        with patch("obs_agent.cli.httpx.AsyncClient", return_value=FakeClient()):
            await stream_message("hello", base_url=config.base_url, channel=channel)
        captured = capsys.readouterr()
        assert "error" in captured.out.lower()


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


# --- Slash Command Parsing ---


class TestParseSlashCommand:
    """parse_slash_command parses input for slash commands vs regular messages."""

    def test_stop_command(self):
        """/stop is parsed as a command."""
        command, text = parse_slash_command("/stop")
        assert command == "/stop"
        assert text is None

    def test_quit_command(self):
        """/quit is parsed as a command."""
        command, text = parse_slash_command("/quit")
        assert command == "/quit"
        assert text is None

    def test_regular_message(self):
        """Regular text is returned as message, not command."""
        command, text = parse_slash_command("hello world")
        assert command is None
        assert text == "hello world"

    def test_empty_input(self):
        """Empty input returns (None, None)."""
        command, text = parse_slash_command("")
        assert command is None
        assert text is None

    def test_whitespace_only(self):
        """Whitespace-only input returns (None, None)."""
        command, text = parse_slash_command("   ")
        assert command is None
        assert text is None

    def test_command_with_whitespace(self):
        """Commands with surrounding whitespace are trimmed."""
        command, text = parse_slash_command("  /stop  ")
        assert command == "/stop"
        assert text is None

    def test_unknown_command(self):
        """Unknown slash commands are still parsed as commands."""
        command, text = parse_slash_command("/unknown")
        assert command == "/unknown"
        assert text is None


# --- Concurrent Input Slash Commands ---


class TestSlashCommandIntegration:
    """Slash commands in REPL trigger correct HTTP calls."""

    @patch("obs_agent.cli.httpx")
    def test_stop_parses_as_command(self, mock_httpx):
        """/stop is parsed as a slash command, not a regular message."""
        command, text = parse_slash_command("/stop")
        assert command == "/stop"
        assert text is None

    @patch("obs_agent.cli.httpx")
    def test_enqueue_parses_as_text(self, mock_httpx):
        """Regular text is parsed as a message for enqueue."""
        command, text = parse_slash_command("follow up question")
        assert command is None
        assert text == "follow up question"

    def test_stream_with_input_exists(self):
        """stream_with_input function exists and is callable."""
        assert callable(stream_with_input)


# --- Status Event Rendering ---


class TestRenderStatus:
    """_render_status parses JSON status events and renders through channel."""

    def test_renders_summary_as_dim_text(self, capsys):
        """_render_status prints summary in dim ANSI text."""
        channel = SimpleChannel()
        _render_status(['{"type":"tool_use","summary":"Read: CLAUDE.md"}'], channel)
        captured = capsys.readouterr()
        assert "(Read: CLAUDE.md)" in captured.out
        # ANSI dim code
        assert "\033[2m" in captured.out
        # ANSI reset code
        assert "\033[0m" in captured.out

    def test_renders_thinking_status(self, capsys):
        """_render_status handles thinking status."""
        channel = SimpleChannel()
        _render_status(['{"type":"thinking","summary":"thinking..."}'], channel)
        captured = capsys.readouterr()
        assert "(thinking...)" in captured.out

    def test_renders_classify_status(self, capsys):
        """_render_status handles skill_classify status."""
        channel = SimpleChannel()
        _render_status(['{"type":"skill_classify","summary":"classifying skills..."}'], channel)
        captured = capsys.readouterr()
        assert "(classifying skills...)" in captured.out

    def test_renders_queue_delivered_with_messages(self, capsys):
        """_render_status renders actual message content for queue_delivered with messages."""
        channel = SimpleChannel()
        _render_status(
            ['{"type":"queue_delivered","summary":"queued message delivered","count":1,"messages":["hello world"]}'],
            channel,
        )
        captured = capsys.readouterr()
        assert "(queued: hello world)" in captured.out

    def test_renders_queue_delivered_without_messages(self, capsys):
        """_render_status falls back to summary when no messages field."""
        channel = SimpleChannel()
        _render_status(
            ['{"type":"queue_delivered","summary":"queued message delivered","count":2}'],
            channel,
        )
        captured = capsys.readouterr()
        assert "(queued message delivered)" in captured.out

    def test_ignores_invalid_json(self, capsys):
        """_render_status silently ignores invalid JSON."""
        channel = SimpleChannel()
        _render_status(["not json at all"], channel)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_ignores_missing_summary(self, capsys):
        """_render_status prints nothing when summary is empty."""
        channel = SimpleChannel()
        _render_status(['{"type":"test","summary":""}'], channel)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_handles_multiline_data(self, capsys):
        """_render_status joins multiline data before parsing."""
        channel = SimpleChannel()
        _render_status(['{"type":"tool_use",', '"summary":"Read: foo"}'], channel)
        captured = capsys.readouterr()
        assert "(Read: foo)" in captured.out

    def test_render_status_exists(self):
        """_render_status function exists and is callable."""
        assert callable(_render_status)


# --- Daemon Start CWD ---


class TestStartDaemonCWD:
    """start_daemon() passes vault CWD to subprocess."""

    @patch("obs_agent.cli.check_daemon", return_value=False)
    @patch("obs_agent.cli.subprocess.Popen")
    def test_start_daemon_uses_vault_cwd(self, mock_popen, mock_check, config):
        """start_daemon passes cwd=vault_path to Popen."""
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        # Make check_daemon return True on second call (daemon started)
        mock_check.side_effect = [False, True]

        start_daemon(config)

        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert "cwd" in call_kwargs
        assert call_kwargs["cwd"] == str(config.vault_path)
