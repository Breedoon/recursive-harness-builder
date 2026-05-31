"""Tests for obs_agent.runner - ConversationRunner.

Verifies the extracted orchestration loop produces the same events
as the original inline daemon code.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.queueing import QueuedMessage
from obs_agent.runner import (
    ConversationRunner,
    DoneEvent,
    TextEvent,
    TurnEndEvent,
    _RECOVERY_PROMPT,
    _drain_queue,
    _is_recoverable,
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
        assert _drain_queue(q) == [
            QueuedMessage(text="a"),
            QueuedMessage(text="b"),
        ]
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

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_accepts_coroutine_returning_response_stream(self, mock_get_client, config):
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello!")]
        mock_msg.session_id = None

        async def response_stream():
            yield mock_msg

        client = AsyncMock()
        client.receive_response = AsyncMock(return_value=response_stream())
        client.query = AsyncMock()
        client.interrupt = AsyncMock()
        mock_get_client.return_value = client

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hi")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert [event.text for event in text_events] == ["Hello!"]


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
        assert any(e.summary == "hmm..." for e in status_events)

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_system_task_started_yields_notification_status(self, mock_get_client, config):
        system_msg = MagicMock()
        system_msg.content = []
        system_msg.session_id = None
        system_msg.subtype = "task_started"
        system_msg.data = {
            "task_id": "task-123",
            "description": "worker-a background run",
            "task_type": "in_process_teammate",
        }
        mock_get_client.return_value = _make_mock_client([system_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "launch")
        status_events = [e for e in events if isinstance(e, StatusEvent)]
        notif = next((e for e in status_events if e.type == "notification"), None)
        assert notif is not None
        assert notif.summary == "notification: task_started"
        assert notif.messages is not None
        assert "task_id: task-123" in notif.messages

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_system_task_notification_yields_notification_status(self, mock_get_client, config):
        system_msg = MagicMock()
        system_msg.content = []
        system_msg.session_id = None
        system_msg.subtype = "task_notification"
        system_msg.data = {
            "task_id": "task-321",
            "status": "completed",
            "summary": "worker-a finished",
        }
        mock_get_client.return_value = _make_mock_client([system_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "wait")
        status_events = [e for e in events if isinstance(e, StatusEvent)]
        notif = next((e for e in status_events if e.type == "notification"), None)
        assert notif is not None
        assert notif.summary == "notification: task_notification"
        assert notif.messages is not None
        assert "status: completed" in notif.messages
        assert "worker-a finished" in notif.messages

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_system_future_task_subtype_yields_notification_status(self, mock_get_client, config):
        system_msg = MagicMock()
        system_msg.content = []
        system_msg.session_id = None
        system_msg.subtype = "task_idle"
        system_msg.data = {
            "task_id": "task-777",
            "status": "idle",
            "summary": "worker-a is idle",
        }
        mock_get_client.return_value = _make_mock_client([system_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "wait")
        status_events = [e for e in events if isinstance(e, StatusEvent)]
        notif = next((e for e in status_events if e.type == "notification"), None)
        assert notif is not None
        assert notif.summary == "notification: task_idle"
        assert notif.messages is not None
        assert "task_id: task-777" in notif.messages
        assert "status: idle" in notif.messages
        assert "worker-a is idle" in notif.messages


class TestRunnerUsageSnapshot:
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_last_result_data_prefers_latest_assistant_usage(self, mock_get_client, config):
        assistant_msg = MagicMock()
        assistant_msg.content = [TextBlock(text="ok")]
        assistant_msg.session_id = "sess-1"
        assistant_msg.usage = {
            "input_tokens": 4,
            "output_tokens": 20,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 2000,
        }

        result_msg = MagicMock()
        result_msg.content = []
        result_msg.session_id = "sess-1"
        result_msg.num_turns = 2
        result_msg.total_cost_usd = 0.12
        result_msg.duration_ms = 1234
        result_msg.usage = {
            "input_tokens": 4,
            "output_tokens": 20,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 9000,
        }

        mock_get_client.return_value = _make_mock_client([assistant_msg, result_msg])

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        await _collect_events(runner, "hello")

        data = hook_state.last_result_data
        assert data is not None
        assert data["session_id"] == "sess-1"
        assert data["num_turns"] == 2
        assert data["total_cost_usd"] == 0.12
        assert data["duration_ms"] == 1234
        # Usage should come from the latest assistant step, not aggregated ResultMessage usage.
        assert data["usage"]["cache_read_input_tokens"] == 2000
        assert data["usage"]["cache_creation_input_tokens"] == 100

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_last_result_data_includes_sdk_context_usage(self, mock_get_client, config):
        assistant_msg = MagicMock()
        assistant_msg.content = [TextBlock(text="ok")]
        assistant_msg.session_id = "sess-ctx"
        assistant_msg.usage = {"input_tokens": 4, "output_tokens": 20}

        result_msg = MagicMock()
        result_msg.content = []
        result_msg.session_id = "sess-ctx"
        result_msg.num_turns = 2
        result_msg.total_cost_usd = 0.12
        result_msg.duration_ms = 1234

        client = _make_mock_client([assistant_msg, result_msg])
        client.get_context_usage = AsyncMock(
            return_value={
                "totalTokens": 180_000,
                "maxTokens": 190_000,
                "rawMaxTokens": 200_000,
                "percentage": 90.0,
                "model": "gpt-5.5[200k]",
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 167_000,
            }
        )
        mock_get_client.return_value = client

        hook_state = HookState()
        from obs_agent.session import SessionManager
        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        await _collect_events(runner, "hello")

        data = hook_state.last_result_data
        assert data is not None
        assert data["context_usage"]["totalTokens"] == 180_000
        assert data["context_usage"]["rawMaxTokens"] == 200_000
        assert data["context_usage"]["autoCompactThreshold"] == 167_000


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

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_reply_target_queue_is_deferred_for_next_run(self, mock_get_client, config):
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            mock_msg.content = [TextBlock(text=f"Response {call_count}")]
            mock_msg.session_id = None
            yield mock_msg
            if call_count == 1:
                hook_state.message_queue.put_nowait(
                    QueuedMessage(
                        text="reply while busy",
                        telegram_message_id=42,
                        reply_to_message_id=7,
                    )
                )

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_get_client.return_value = mock_client

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        texts = [e.text for e in text_events]
        assert texts == ["Response 1"]
        assert mock_client.query.call_count == 1
        assert runner.remaining_pending == [
            QueuedMessage(
                text="reply while busy",
                telegram_message_id=42,
                reply_to_message_id=7,
            )
        ]

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_interrupt_requested_skips_continuation_queries(self, mock_get_client, config):
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            mock_msg.content = [TextBlock(text=f"Response {call_count}")]
            mock_msg.session_id = None
            yield mock_msg

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_get_client.return_value = mock_client

        hook_state = HookState(interrupt_requested=True)
        hook_state.message_queue.put_nowait("queued during response")
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert [e.text for e in text_events] == ["Response 1"]
        assert mock_client.query.call_count == 1
        assert runner.remaining_pending == [QueuedMessage(text="queued during response")]

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_interrupt_requested_skips_background_wakeup_queries(self, mock_get_client, config):
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            mock_msg = MagicMock()
            mock_msg.content = [TextBlock(text=f"Response {call_count}")]
            mock_msg.session_id = None
            yield mock_msg

        mock_client = AsyncMock()
        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        mock_get_client.return_value = mock_client

        hook_state = HookState(interrupt_requested=True)
        done_task = asyncio.create_task(asyncio.sleep(0))
        hook_state.background_tasks.add(done_task)
        hook_state.message_queue.put_nowait("bg callback")
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        await _collect_events(runner, "hello")

        assert mock_client.query.call_count == 1
        assert runner.remaining_pending == [QueuedMessage(text="bg callback")]


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


# --- Error classification ---


class TestIsRecoverable:
    """Test _is_recoverable error classification logic.

    Policy: everything is recoverable EXCEPT CLINotFoundError (claude binary missing).
    Even ProcessError (CLI died) is recoverable because resume=session_id starts
    a fresh process that loads conversation history from disk.
    """

    def test_process_error_is_recoverable(self):
        """ProcessError IS recoverable — reconnect with resume=session_id."""
        assert _is_recoverable(ProcessError("died", exit_code=1))

    def test_cli_json_decode_error_recoverable(self):
        assert _is_recoverable(
            CLIJSONDecodeError("big json", ValueError("too big"))
        )

    def test_cli_connection_error_recoverable(self):
        assert _is_recoverable(CLIConnectionError("lost"))

    def test_generic_buffer_overflow_recoverable(self):
        """SDK wraps CLIJSONDecodeError into generic Exception."""
        exc = Exception(
            "Failed to decode JSON: JSON message exceeded maximum buffer size"
        )
        assert _is_recoverable(exc)

    def test_generic_process_exit_recoverable(self):
        """Even generic process-exit messages are recoverable."""
        exc = Exception("Command failed with exit code 1 (process died)")
        assert _is_recoverable(exc)

    def test_unknown_error_defaults_recoverable(self):
        assert _is_recoverable(RuntimeError("something weird"))

    def test_cli_not_found_error_not_recoverable(self):
        """CLINotFoundError is the ONLY unrecoverable case — claude binary missing."""
        assert not _is_recoverable(CLINotFoundError("claude not found"))


# --- Reconnect on stream errors ---


class TestRunnerReconnectOnStreamError:
    """Test that the runner reconnects on recoverable errors during streaming."""

    @patch("obs_agent.session.SessionManager.reconnect")
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_reconnects_on_buffer_overflow(
        self, mock_get_client, mock_reconnect, config
    ):
        """When receive_response raises a recoverable error, runner reconnects."""
        # First client: query succeeds, but receive_response raises
        failing_client = AsyncMock()
        failing_client.query = AsyncMock()

        async def failing_receive():
            raise Exception("JSON message exceeded maximum buffer size")
            yield  # noqa: unreachable — makes this an async generator

        failing_client.receive_response = failing_receive
        mock_get_client.return_value = failing_client

        # Reconnect client: works normally
        recovery_msg = MagicMock()
        recovery_msg.content = [TextBlock(text="Recovered")]
        recovery_msg.session_id = "sess-99"
        recovery_client = _make_mock_client([recovery_msg])
        mock_reconnect.return_value = recovery_client

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert any("Recovered" in e.text for e in text_events)
        mock_reconnect.assert_called_once()
        recovery_client.query.assert_awaited_once_with(f"(System: {_RECOVERY_PROMPT})")


class TestRunnerGetClientRecovery:
    @patch("obs_agent.session.SessionManager.async_reset")
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_second_recoverable_get_client_failure_preserves_existing_session(
        self, mock_get_client, mock_async_reset, config
    ):
        failing = CLIJSONDecodeError("big json", ValueError("too big"))
        mock_get_client.side_effect = [failing, failing]

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        session_mgr.set_session_id("sess-existing")
        with (
            patch.object(session_mgr, "disconnect", new_callable=AsyncMock) as mock_disconnect,
            patch.object(session_mgr, "soft_reset", new_callable=AsyncMock) as mock_soft_reset,
        ):
            runner = ConversationRunner(session_mgr, hook_state, config)
            with pytest.raises(CLIJSONDecodeError, match="big json"):
                await _collect_events(runner, "hello")

        assert session_mgr.session_id == "sess-existing"
        mock_disconnect.assert_awaited_once()
        mock_soft_reset.assert_awaited_once()
        mock_async_reset.assert_not_awaited()

    @patch("obs_agent.session.SessionManager.async_reset")
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_second_recoverable_get_client_failure_before_session_starts_fresh(
        self, mock_get_client, mock_async_reset, config
    ):
        failing = CLIJSONDecodeError("big json", ValueError("too big"))

        recovery_msg = MagicMock()
        recovery_msg.content = [TextBlock(text="Fresh session")]
        recovery_msg.session_id = "sess-fresh"
        fresh_client = _make_mock_client([recovery_msg])

        mock_get_client.side_effect = [failing, failing, fresh_client]

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        with patch.object(session_mgr, "disconnect", new_callable=AsyncMock) as mock_disconnect:
            runner = ConversationRunner(session_mgr, hook_state, config)
            events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert any("Fresh session" in e.text for e in text_events)
        mock_disconnect.assert_awaited_once()
        mock_async_reset.assert_awaited_once()

    @patch("obs_agent.session.SessionManager.reconnect")
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_process_error_attempts_reconnect(
        self, mock_get_client, mock_reconnect, config
    ):
        """ProcessError is now recoverable — reconnect IS attempted."""
        failing_client = AsyncMock()
        failing_client.query = AsyncMock()

        async def failing_receive():
            raise ProcessError("CLI died", exit_code=1)
            yield  # noqa: unreachable

        failing_client.receive_response = failing_receive
        mock_get_client.return_value = failing_client

        # Reconnect succeeds and returns a working client
        recovery_msg = MagicMock()
        recovery_msg.content = [TextBlock(text="Recovered after crash")]
        recovery_msg.session_id = "sess-recovered"
        recovery_client = _make_mock_client([recovery_msg])
        mock_reconnect.return_value = recovery_client

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert any("Recovered" in e.text for e in text_events)
        mock_reconnect.assert_called_once()

    @patch("obs_agent.session.SessionManager.get_client")
    async def test_cli_not_found_raises_immediately(self, mock_get_client, config):
        """CLINotFoundError re-raises without reconnect attempt."""
        failing_client = AsyncMock()
        failing_client.query = AsyncMock()

        async def failing_receive():
            raise CLINotFoundError("claude not found")
            yield  # noqa: unreachable

        failing_client.receive_response = failing_receive
        mock_get_client.return_value = failing_client

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)

        with pytest.raises(CLINotFoundError):
            await _collect_events(runner, "hello")

    @patch("obs_agent.session.SessionManager.reconnect")
    @patch("obs_agent.session.SessionManager.get_client")
    async def test_reconnect_failure_propagates_original_error(
        self, mock_get_client, mock_reconnect, config
    ):
        """If reconnect fails, the ORIGINAL stream error propagates to the caller."""
        failing_client = AsyncMock()
        failing_client.query = AsyncMock()

        async def failing_receive():
            raise CLIConnectionError("stream died")
            yield  # noqa: unreachable

        failing_client.receive_response = failing_receive
        mock_get_client.return_value = failing_client
        mock_reconnect.side_effect = RuntimeError("Cannot reconnect: no session_id")

        hook_state = HookState()
        from obs_agent.session import SessionManager

        session_mgr = SessionManager(config=config, hook_state=hook_state)
        runner = ConversationRunner(session_mgr, hook_state, config)

        with pytest.raises(CLIConnectionError, match="stream died"):
            await _collect_events(runner, "hello")


# --- SessionManager reconnect / soft_reset ---


class TestSessionManagerReconnect:
    async def test_reconnect_preserves_session_id(self, config):
        from obs_agent.session import SessionManager

        hook_state = HookState()
        session_mgr = SessionManager(config=config, hook_state=hook_state)
        session_mgr.set_session_id("sess-42")

        with patch.object(session_mgr, "_build_options"), patch(
            "obs_agent.session.ClaudeSDKClient"
        ) as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance
            mock_instance.connect = AsyncMock()

            client = await session_mgr.reconnect()
            assert session_mgr.session_id == "sess-42"
            assert client is mock_instance

    async def test_reconnect_fails_without_session_id(self, config):
        from obs_agent.session import SessionManager

        hook_state = HookState()
        session_mgr = SessionManager(config=config, hook_state=hook_state)

        with pytest.raises(RuntimeError, match="no session_id"):
            await session_mgr.reconnect()

    async def test_soft_reset_preserves_session_id(self, config):
        from obs_agent.session import SessionManager

        hook_state = HookState()
        session_mgr = SessionManager(config=config, hook_state=hook_state)
        session_mgr.set_session_id("sess-42")

        await session_mgr.soft_reset()
        assert session_mgr.session_id == "sess-42"
        assert session_mgr._client is None

    async def test_async_reset_clears_session_id(self, config):
        from obs_agent.session import SessionManager

        hook_state = HookState()
        session_mgr = SessionManager(config=config, hook_state=hook_state)
        session_mgr.set_session_id("sess-42")

        await session_mgr.async_reset()
        assert session_mgr.session_id is None
