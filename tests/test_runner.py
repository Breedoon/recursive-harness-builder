"""Tests for obs_agent.runner - ConversationRunner.

Verifies the extracted orchestration loop produces the same events
as the original inline daemon code.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.runner import (
    ConversationRunner,
    DoneEvent,
    TextEvent,
    TurnEndEvent,
    _drain_queue,
)


def _make_mock_client(messages: list) -> AsyncMock:
    """Create a mock ClaudeSDKClient that yields the given messages."""
    client = AsyncMock()

    async def mock_receive():
        for msg in messages:
            yield msg

    client.receive_response = mock_receive
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    return client


async def _collect_events(runner, message):
    """Collect all events from a runner.run() call."""
    events = []
    async for event in runner.run(message):
        events.append(event)
    return events


# --- drain_queue ---


class TestDrainQueue:
    def test_drain_empty_queue(self):
        q = asyncio.Queue()
        assert _drain_queue(q) == []

    def test_drain_with_messages(self):
        q = asyncio.Queue()
        q.put_nowait("a")
        q.put_nowait("b")
        assert _drain_queue(q) == ["a", "b"]
        assert q.empty()


# --- Basic text response ---


class TestRunnerBasicResponse:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_yields_text_event(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello!")]
        mock_msg.session_id = None
        mock_get_client.return_value = _make_mock_client([mock_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hi")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert len(text_events) == 1
        assert text_events[0].text == "Hello!"

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_yields_done_event_last(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Done.")]
        mock_msg.session_id = None
        mock_get_client.return_value = _make_mock_client([mock_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "test")

        assert isinstance(events[-1], DoneEvent)
        assert any(isinstance(e, TurnEndEvent) for e in events)

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_captures_session_id(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Ok")]
        mock_msg.session_id = "sess-42"
        mock_get_client.return_value = _make_mock_client([mock_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        await _collect_events(runner, "test")

        assert session_mgr.session_id == "sess-42"


# --- Status events ---


class TestRunnerStatusEvents:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_tool_use_yields_status(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [
            ToolUseBlock(id="tu-1", name="Read", input={"file_path": "/tmp/x"}),
            TextBlock(text="Contents."),
        ]
        mock_msg.session_id = None
        mock_get_client.return_value = _make_mock_client([mock_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "read file")

        status_events = [e for e in events if isinstance(e, StatusEvent)]
        assert any(e.type == "tool_use" for e in status_events)

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_thinking_yields_status(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [
            ThinkingBlock(thinking="hmm...", signature="sig"),
            TextBlock(text="Answer."),
        ]
        mock_msg.session_id = None
        mock_get_client.return_value = _make_mock_client([mock_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "think")

        status_events = [e for e in events if isinstance(e, StatusEvent)]
        assert any(e.type == "thinking" for e in status_events)


# --- Pending messages ---


class TestRunnerPendingMessages:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_pending_messages_injected(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Reply")]
        mock_msg.session_id = None

        mock_client = _make_mock_client([mock_msg])
        mock_get_client.return_value = mock_client

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(
            session_mgr, hook_state, config,
            pending_messages=["queued msg"],
        )
        events = await _collect_events(runner, "hello")

        # Should have a queue_delivered status event
        status_events = [e for e in events if isinstance(e, StatusEvent)]
        assert any(e.type == "queue_delivered" for e in status_events)

        # query should have been called with the combined message
        call_args = mock_client.query.call_args_list[0][0][0]
        assert "[Queued message from user]: queued msg" in call_args
        assert "hello" in call_args

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_no_pending_no_injection(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Reply")]
        mock_msg.session_id = None

        mock_client = _make_mock_client([mock_msg])
        mock_get_client.return_value = mock_client

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        # No queue_delivered events
        status_events = [e for e in events if isinstance(e, StatusEvent)]
        assert not any(e.type == "queue_delivered" for e in status_events)

        # query called with plain message
        call_args = mock_client.query.call_args_list[0][0][0]
        assert call_args == "hello"


# --- Continuation loop ---


class TestRunnerContinuation:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_continuation_processes_queued(self, mock_get_client, config):
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            if call_count == 1:
                mock_msg.content = [TextBlock(text="Main response")]
            else:
                mock_msg.content = [TextBlock(text="Continuation")]
            mock_msg.session_id = None
            yield mock_msg

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_get_client.return_value = mock_client

        hook_state = HookState()
        hook_state.message_queue.put_nowait("queued during response")

        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        texts = [e.text for e in text_events]
        assert "Main response" in texts
        assert "Continuation" in texts


# --- Remaining pending ---


class TestRunnerRemainingPending:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_remaining_pending_saved(self, mock_get_client, config):
        """After max continuations, remaining queue items saved to pending."""
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            mock_msg.content = [TextBlock(text=f"Response {call_count}")]
            mock_msg.session_id = None
            yield mock_msg
            # Keep adding messages to exceed max_queue_continuations
            if call_count <= 3:
                hook_state.message_queue.put_nowait(f"overflow {call_count}")

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_get_client.return_value = mock_client

        hook_state = HookState()
        hook_state.message_queue.put_nowait("first queued")

        from obs_agent.session import SessionManager
        from obs_agent.config import OBSConfig
        cfg = OBSConfig(vault_path=config.vault_path, max_queue_continuations=1)
        session_mgr = SessionManager(config=cfg, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, cfg)
        await _collect_events(runner, "hello")

        # Should have remaining pending from the overflow
        assert len(runner.remaining_pending) > 0


class TestRunnerTurnBoundaries:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_turn_end_emitted_once_per_sdk_message(self, mock_get_client, config):
        msg1 = MagicMock()
        msg1.content = [TextBlock(text="first")]
        msg1.session_id = None

        msg2 = MagicMock()
        msg2.content = [TextBlock(text="second")]
        msg2.session_id = None

        mock_get_client.return_value = _make_mock_client([msg1, msg2])

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        turn_end_events = [e for e in events if isinstance(e, TurnEndEvent)]
        assert len(turn_end_events) == 2
