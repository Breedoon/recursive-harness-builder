"""Integration tests for message queuing, interrupt, and hook pipeline.

Layer 1: Endpoint + state (TestClient) — verifies HTTP endpoints populate shared state
Layer 2: Hook pipeline with real queue — verifies hooks drain queue / check interrupt
Layer 3: Live HTTP — covered in test_http_integration.py extensions

See plan Step 5 for full test matrix.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock
from fastapi.testclient import TestClient

from obs_agent.daemon import create_app
from obs_agent.hooks import HookState, HookPipeline, _make_interrupt_check, _make_queue_check, create_hook_matchers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pre_tool_use_input(**overrides) -> dict:
    """Build a minimal PreToolUseHookInput dict."""
    base = {
        "hook_event_name": "PreToolUse",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "tool_name": "Read",
        "tool_input": {"file_path": "/some/file.md"},
        "tool_use_id": "tu-123",
    }
    base.update(overrides)
    return base


_EMPTY_CONTEXT = {"signal": None}


# ---------------------------------------------------------------------------
# Layer 1: Endpoint + State (TestClient)
# ---------------------------------------------------------------------------


class TestEnqueueEndpointIntegration:
    """POST /chat/enqueue populates the shared HookState queue."""

    def test_enqueue_populates_queue(self, config):
        """Enqueued message appears in hook state queue."""
        app = create_app(config)
        client = TestClient(app)

        resp = client.post("/chat/enqueue", json={"message": "follow up"})
        assert resp.status_code == 200

        state: HookState = app.state.hook_state
        assert state.message_queue.qsize() == 1
        assert state.message_queue.get_nowait() == "follow up"

    def test_enqueue_rejects_empty(self, config):
        """Empty message is rejected with 422."""
        app = create_app(config)
        client = TestClient(app)

        resp = client.post("/chat/enqueue", json={"message": ""})
        assert resp.status_code == 422

    def test_multiple_enqueues(self, config):
        """Multiple enqueues accumulate in the queue."""
        app = create_app(config)
        client = TestClient(app)

        client.post("/chat/enqueue", json={"message": "first"})
        client.post("/chat/enqueue", json={"message": "second"})
        resp = client.post("/chat/enqueue", json={"message": "third"})

        data = resp.json()
        assert data["queue_size"] == 3

        state: HookState = app.state.hook_state
        assert state.message_queue.qsize() == 3


class TestInterruptEndpointIntegration:
    """POST /chat/interrupt sets the interrupt flag in shared HookState."""

    def test_interrupt_sets_flag(self, config):
        """Interrupt sets the hook state flag."""
        app = create_app(config)
        client = TestClient(app)

        resp = client.post("/chat/interrupt")
        assert resp.status_code == 200

        state: HookState = app.state.hook_state
        assert state.interrupt_flag is True

    def test_interrupt_idempotent(self, config):
        """Multiple interrupts are idempotent."""
        app = create_app(config)
        client = TestClient(app)

        client.post("/chat/interrupt")
        resp = client.post("/chat/interrupt")
        assert resp.status_code == 200

        state: HookState = app.state.hook_state
        assert state.interrupt_flag is True


# ---------------------------------------------------------------------------
# Layer 2: Hook Pipeline with Real Queue
# ---------------------------------------------------------------------------


class TestPipelineWithRealQueue:
    """Hook pipeline integration with real asyncio.Queue and interrupt flag."""

    @pytest.mark.asyncio
    async def test_queue_check_drains_into_context(self):
        """Queue check drains real queue into additionalContext."""
        state = HookState()
        state.message_queue.put_nowait("message 1")
        state.message_queue.put_nowait("message 2")

        check = _make_queue_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        assert result is not None
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "message 1" in ctx
        assert "message 2" in ctx
        assert state.message_queue.empty()

    @pytest.mark.asyncio
    async def test_interrupt_check_returns_stop(self):
        """Interrupt check returns continue_: False and clears flag."""
        state = HookState(interrupt_flag=True)
        check = _make_interrupt_check(state)

        result = await check(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        assert result is not None
        assert result["continue_"] is False
        assert result["stopReason"] == "Interrupted by user"
        assert state.interrupt_flag is False

    @pytest.mark.asyncio
    async def test_interrupt_short_circuits_queue(self):
        """When interrupt is set, pipeline stops before draining queue."""
        state = HookState(interrupt_flag=True)
        state.message_queue.put_nowait("should not be drained")

        interrupt_check = _make_interrupt_check(state)
        queue_check = _make_queue_check(state)
        pipeline = HookPipeline([interrupt_check, queue_check])

        result = await pipeline(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        assert result["continue_"] is False
        # Queue should still have the message (not drained)
        assert state.message_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_full_pipeline_enqueue_then_drain(self, config):
        """Full create_hook_matchers pipeline: enqueue then drain at hook boundary."""
        state = HookState()
        state.message_queue.put_nowait("user follow-up")

        matchers = create_hook_matchers(config, state)
        pre_pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pre_pipeline(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        # Should have drained the queue into additionalContext
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "user follow-up" in ctx
        assert state.message_queue.empty()

    @pytest.mark.asyncio
    async def test_full_pipeline_interrupt_priority(self, config):
        """Full pipeline: interrupt takes priority over queued messages."""
        state = HookState(interrupt_flag=True)
        state.message_queue.put_nowait("queued msg")

        matchers = create_hook_matchers(config, state)
        pre_pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pre_pipeline(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        assert result["continue_"] is False
        assert state.interrupt_flag is False
        # Queue NOT drained (short-circuited)
        assert state.message_queue.qsize() == 1


class TestSharedStateBetweenEndpointAndPipeline:
    """Verify that daemon endpoints and hook pipelines share the same state."""

    @pytest.mark.asyncio
    async def test_enqueue_endpoint_feeds_pipeline(self, config):
        """Message enqueued via HTTP endpoint is drained by hook pipeline."""
        app = create_app(config)
        client = TestClient(app)

        # Enqueue via HTTP
        client.post("/chat/enqueue", json={"message": "via http"})

        # Get the pipeline from the same app
        state: HookState = app.state.hook_state
        matchers = create_hook_matchers(config, state)
        pre_pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pre_pipeline(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "via http" in ctx

    @pytest.mark.asyncio
    async def test_interrupt_endpoint_feeds_pipeline(self, config):
        """Interrupt set via HTTP endpoint is seen by hook pipeline."""
        app = create_app(config)
        client = TestClient(app)

        # Interrupt via HTTP
        client.post("/chat/interrupt")

        # Get the pipeline from the same app
        state: HookState = app.state.hook_state
        matchers = create_hook_matchers(config, state)
        pre_pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pre_pipeline(_make_pre_tool_use_input(), "tu-1", _EMPTY_CONTEXT)

        assert result["continue_"] is False


class TestQueueContinuationIntegration:
    """Integration test: queued messages processed inline."""

    @patch("obs_agent.session.SessionManager.get_client")
    def test_enqueue_before_stream_gets_continuation(self, mock_get_client, config):
        """Message in queue before streaming triggers continuation."""
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
        mock_client.interrupt = AsyncMock()

        mock_get_client.return_value = mock_client

        app = create_app(config)
        # Put a message in the queue
        app.state.hook_state.message_queue.put_nowait("follow up")

        client = TestClient(app)
        response = client.post("/chat/stream", json={"message": "initial"})
        body = response.text

        # Should see both responses
        assert "Response 1" in body
        assert "Response 2" in body
        assert "queue_delivered" in body
