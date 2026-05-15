"""Tests for GPT 408 synthetic error detection and recovery.

Covers: detection logic, exponential backoff calculation, counter
behavior, health probe integration, and full recovery flow through
ConversationRunner.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.hooks import HookState
from obs_agent.runner import (
    ConversationRunner,
    DoneEvent,
    StatusEvent,
    TextEvent,
    TurnEndEvent,
)
from obs_agent.session import SessionManager


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_obs_tools():
    with patch("obs_agent.session.create_obs_tools", return_value=MagicMock()):
        yield


# -----------------------------------------------------------------------
# SessionManager: API error tracking
# -----------------------------------------------------------------------

class TestApiErrorTracking:
    def test_initial_state(self, config):
        mgr = SessionManager(config=config)
        assert mgr.consecutive_api_errors == 0
        assert mgr.backoff_seconds == 0.0

    def test_first_error_no_backoff(self, config):
        mgr = SessionManager(config=config)
        count = mgr.record_api_error()
        assert count == 1
        assert mgr.backoff_seconds == 0.0

    def test_second_error_initial_backoff(self, config):
        mgr = SessionManager(config=config)
        mgr.record_api_error()
        count = mgr.record_api_error()
        assert count == 2
        assert mgr.backoff_seconds == 30.0

    def test_exponential_backoff_doubles(self, config):
        mgr = SessionManager(config=config)
        mgr.record_api_error()  # 1: no backoff
        mgr.record_api_error()  # 2: 30s
        mgr.record_api_error()  # 3: 60s
        assert mgr.backoff_seconds == 60.0
        mgr.record_api_error()  # 4: 120s
        assert mgr.backoff_seconds == 120.0
        mgr.record_api_error()  # 5: 240s
        assert mgr.backoff_seconds == 240.0

    def test_backoff_caps_at_max(self, config):
        mgr = SessionManager(config=config)
        for _ in range(20):
            mgr.record_api_error()
        assert mgr.backoff_seconds <= 300.0

    def test_clear_resets_everything(self, config):
        mgr = SessionManager(config=config)
        mgr.record_api_error()
        mgr.record_api_error()
        mgr.record_api_error()
        mgr.clear_api_errors()
        assert mgr.consecutive_api_errors == 0
        assert mgr.backoff_seconds == 0.0

    def test_clear_then_new_errors_restart_sequence(self, config):
        mgr = SessionManager(config=config)
        mgr.record_api_error()
        mgr.record_api_error()
        mgr.clear_api_errors()
        mgr.record_api_error()
        assert mgr.consecutive_api_errors == 1
        assert mgr.backoff_seconds == 0.0
        mgr.record_api_error()
        assert mgr.consecutive_api_errors == 2
        assert mgr.backoff_seconds == 30.0


# -----------------------------------------------------------------------
# ConversationRunner: synthetic error detection
# -----------------------------------------------------------------------

def _make_synthetic_message():
    msg = MagicMock()
    msg.model = "<synthetic>"
    msg.isApiErrorMessage = True
    msg.content = []
    msg.session_id = None
    msg.num_turns = None
    msg.total_cost_usd = None
    msg.role = "assistant"
    type(msg).__name__ = "AssistantMessage"
    return msg


def _make_normal_message(text="Hello"):
    msg = MagicMock()
    msg.model = "gpt-5.5"
    msg.isApiErrorMessage = False
    from claude_agent_sdk import TextBlock
    block = TextBlock(text=text)
    msg.content = [block]
    msg.session_id = "test-session"
    msg.num_turns = 1
    msg.total_cost_usd = 0.01
    msg.role = "assistant"
    msg.usage = {"input_tokens": 100, "output_tokens": 50}
    type(msg).__name__ = "AssistantMessage"
    return msg


def _make_mock_client(messages):
    client = AsyncMock()

    async def mock_receive():
        for m in messages:
            yield m

    client.receive_response = mock_receive
    client.query = AsyncMock()
    client.session_id = "test-session"
    return client


async def _collect_events(runner, message):
    events = []
    async for event in runner.run(message):
        events.append(event)
    return events


class TestSyntheticErrorDetection:
    @pytest.mark.asyncio
    async def test_detects_synthetic_model(self, config):
        """Synthetic message with model='<synthetic>' sets detection flag."""
        mgr = SessionManager(config=config)
        hook_state = HookState()
        client = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client)

        runner = ConversationRunner(mgr, hook_state, config)
        events = await _collect_events(runner, "hello")

        assert mgr.consecutive_api_errors == 1

    @pytest.mark.asyncio
    async def test_normal_message_clears_errors(self, config):
        """A successful turn resets the error counter."""
        mgr = SessionManager(config=config)
        hook_state = HookState()

        # First: synthetic error
        client1 = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client1)
        runner1 = ConversationRunner(mgr, hook_state, config)
        await _collect_events(runner1, "hello")
        assert mgr.consecutive_api_errors == 1

        # Second: normal message
        client2 = _make_mock_client([_make_normal_message()])
        mgr.get_client = AsyncMock(return_value=client2)
        runner2 = ConversationRunner(mgr, hook_state, config)
        await _collect_events(runner2, "hello")
        assert mgr.consecutive_api_errors == 0

    @pytest.mark.asyncio
    async def test_detects_api_error_flag(self, config):
        """Message with isApiErrorMessage=True is detected even without synthetic model."""
        msg = MagicMock()
        msg.model = "gpt-5.5"
        msg.isApiErrorMessage = True
        msg.content = []
        msg.session_id = None
        msg.num_turns = None
        msg.total_cost_usd = None
        type(msg).__name__ = "AssistantMessage"

        mgr = SessionManager(config=config)
        hook_state = HookState()
        client = _make_mock_client([msg])
        mgr.get_client = AsyncMock(return_value=client)

        runner = ConversationRunner(mgr, hook_state, config)
        await _collect_events(runner, "hello")
        assert mgr.consecutive_api_errors == 1


# -----------------------------------------------------------------------
# ConversationRunner: recovery flow
# -----------------------------------------------------------------------

class TestRecoveryFlow:
    @pytest.mark.asyncio
    async def test_recovery_triggers_after_two_errors(self, config):
        """After 2 consecutive synthetic errors, recovery fires with backoff."""
        mgr = SessionManager(config=config)
        hook_state = HookState()

        # First error — no recovery
        client1 = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client1)
        runner1 = ConversationRunner(mgr, hook_state, config)
        events1 = await _collect_events(runner1, "hello")
        status_events1 = [e for e in events1 if isinstance(e, StatusEvent)]
        degraded1 = [e for e in status_events1 if getattr(e, "type", None) == "api_degraded"]
        assert len(degraded1) == 0

        # Second error — recovery fires
        client2 = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client2)
        mgr.soft_reset = AsyncMock()
        mgr.probe_api_health = AsyncMock(return_value=True)

        runner2 = ConversationRunner(mgr, hook_state, config)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            events2 = await _collect_events(runner2, "hello")

        status_events2 = [e for e in events2 if isinstance(e, StatusEvent)]
        degraded2 = [e for e in status_events2 if getattr(e, "type", None) == "api_degraded"]
        recovered = [e for e in status_events2 if getattr(e, "type", None) == "api_recovered"]
        assert len(degraded2) >= 1
        assert len(recovered) == 1
        mock_sleep.assert_awaited_once_with(30.0)
        mgr.soft_reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recovery_skips_continuations(self, config):
        """During recovery, continuation loop is skipped and DoneEvent returned early."""
        mgr = SessionManager(config=config)
        hook_state = HookState()

        # Queue a message during the "turn"
        from obs_agent.queueing import QueuedMessage
        hook_state.message_queue.put_nowait(
            QueuedMessage(text="queued msg", reply_to_message_id=None)
        )

        mgr.record_api_error()  # first error
        client = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client)
        mgr.soft_reset = AsyncMock()
        mgr.probe_api_health = AsyncMock(return_value=True)

        runner = ConversationRunner(mgr, hook_state, config)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            events = await _collect_events(runner, "hello")

        # DoneEvent should be present (early return)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1

        # Queued message should be in pending (deferred, not processed)
        assert len(runner.remaining_pending) == 1
        assert runner.remaining_pending[0].text == "queued msg"

    @pytest.mark.asyncio
    async def test_failed_probe_extends_backoff(self, config):
        """If health probe fails, backoff increases without soft_reset."""
        mgr = SessionManager(config=config)
        hook_state = HookState()

        mgr.record_api_error()  # first error
        client = _make_mock_client([_make_synthetic_message()])
        mgr.get_client = AsyncMock(return_value=client)
        mgr.soft_reset = AsyncMock()
        mgr.probe_api_health = AsyncMock(return_value=False)

        runner = ConversationRunner(mgr, hook_state, config)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            events = await _collect_events(runner, "hello")

        # soft_reset should NOT be called when probe fails
        mgr.soft_reset.assert_not_awaited()

        # Error count should have incremented again (record_api_error called for probe failure)
        assert mgr.consecutive_api_errors == 3
        assert mgr.backoff_seconds == 60.0  # escalated from 30 to 60

    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self, config):
        """Backoff delays escalate: 30 → 60 → 120."""
        mgr = SessionManager(config=config)
        hook_state = HookState()
        sleep_times = []

        async def track_sleep(seconds):
            sleep_times.append(seconds)

        # Run 3 consecutive error turns with failed probes
        for i in range(3):
            client = _make_mock_client([_make_synthetic_message()])
            mgr.get_client = AsyncMock(return_value=client)
            mgr.soft_reset = AsyncMock()
            mgr.probe_api_health = AsyncMock(return_value=False)
            runner = ConversationRunner(mgr, hook_state, config)
            with patch("asyncio.sleep", side_effect=track_sleep):
                await _collect_events(runner, f"hello {i}")

        # First error (count=1): no backoff
        # Second error (count=2): 30s backoff
        # Third error: probe fail bumps to count=3, 60s... but wait
        # Let me think about this more carefully:
        # Turn 1: record_api_error → count=1, no recovery
        # Turn 2: record_api_error → count=2, backoff=30, sleep(30), probe fails → record_api_error → count=3
        # Turn 3: record_api_error → count=4, backoff=120, sleep(120), probe fails → record_api_error → count=5
        assert sleep_times[0] == 30.0   # turn 2
        assert sleep_times[1] == 120.0  # turn 3 (count was 3→4, backoff=60*2=120)
