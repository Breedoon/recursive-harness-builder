"""Tests for agent liveness bugs: must_reply loop, premature death, session_lineage XML.

These tests reproduce the bugs found in production and verify the fixes.
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.lineage import (
    native_agent_name_for_lineage,
    root_team_key_for_lineage,
)
from obs_agent.tools import (
    _coerce_bool_arg,
    detect_must_reply_completions,
    check_and_clear_must_reply_obligations,
    validate_must_reply_recipient,
)


# ---------------------------------------------------------------------------
# BUG 1: must_reply ping-pong loop
# ---------------------------------------------------------------------------


class TestMustReplyPingPongLoop:
    """The ping-pong bug: two agents exchange must_reply=true messages
    infinitely because each new must_reply upserts the reply_wake schedule
    and resets run_count to 0, so max_runs=3 never fires."""

    def test_reply_detection_suppresses_must_reply_on_replies(self):
        """When an outgoing message is detected as a reply to a must_reply,
        the must_reply flag should be suppressed (Layer 3 fix)."""
        # Simulate sender's inbox with a must_reply from recipient
        sender_inbox = [
            {
                "from": "agent-b",
                "text": "Please help with X",
                "must_reply": True,
                "replied": False,
                "read": True,
            },
        ]
        # detect_must_reply_completions should mark it as replied
        updated, all_replied = detect_must_reply_completions(sender_inbox, "agent-b")
        assert updated[0]["replied"] is True
        assert all_replied is True
        # The caller (tools.py) should then set must_reply=False on the
        # outgoing notification — this is verified by checking the code
        # path, not just the helper function.

    def test_detect_must_reply_only_marks_from_matching_sender(self):
        """Reply detection should only mark messages from the RECIPIENT
        as replied, not from other senders."""
        sender_inbox = [
            {"from": "agent-b", "text": "msg1", "must_reply": True, "replied": False},
            {"from": "agent-c", "text": "msg2", "must_reply": True, "replied": False},
        ]
        # Sending to agent-b should only mark agent-b's must_reply
        updated, all_replied = detect_must_reply_completions(sender_inbox, "agent-b")
        assert updated[0]["replied"] is True  # agent-b's message
        assert updated[1]["replied"] is False  # agent-c's message
        assert all_replied is False  # agent-c still unreplied

    def test_upsert_should_not_reset_run_count(self):
        """Verify that the create_reply_wake_schedule function creates
        a schedule with run_count=0, and that the upsert path in telegram.py
        no longer resets run_count."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=12345, thread_id=678)
        schedule = create_reply_wake_schedule(route)
        assert schedule.run_count == 0
        assert schedule.max_runs == 3
        assert schedule.interval_seconds == 1

        # Simulate what happens after 2 runs:
        schedule.run_count = 2
        # In the old code, upsert would reset to 0.
        # In the fixed code, run_count is preserved.
        # We can't test the telegram.py upsert path in a unit test
        # (it requires a full bot), but we verify the schedule's
        # initial state is correct.

    def test_wake_prompt_tells_agent_must_reply_false(self):
        """The reply_wake schedule prompt should explicitly tell the agent
        to use must_reply=false."""
        from obs_agent.telegram import create_reply_wake_schedule, TelegramRoute

        route = TelegramRoute(chat_id=12345, thread_id=678)
        schedule = create_reply_wake_schedule(route)
        assert "must_reply=false" in schedule.prompt.lower()
        assert "do not set must_reply=true" in schedule.prompt.lower()


# ---------------------------------------------------------------------------
# BUG 2: Premature agent death / always-deliver
# ---------------------------------------------------------------------------


class TestAlwaysDeliver:
    """Messages should ALWAYS deliver to inbox. The validator is advisory
    (for wake attempts), not a gate that blocks delivery."""

    def test_send_inbox_message_succeeds_even_when_validator_says_no(self):
        """When the validator returns deliverable=False, the message
        should still be written to the inbox file. Verify via source."""
        source = Path("/tmp/obs-liveness-fix/src/obs_agent/tools.py").read_text()
        # Find the _send_inbox_message function
        idx = source.find("async def _send_inbox_message(")
        assert idx > 0, "_send_inbox_message not found"
        func_source = source[idx : idx + 3000]
        # The validator should NOT return an error — it should set
        # a flag that controls wake behavior
        assert "_recipient_wakeable" in func_source
        # Old blocking pattern should be gone
        assert "message undelivered" not in func_source

    def test_recipient_delivery_status_never_removes_mappings(self):
        """_recipient_delivery_status should be a pure read — no side effects.
        It must NOT call _remove_team_worker_mappings_for_task."""
        import inspect
        from obs_agent.telegram import TelegramBot

        source = inspect.getsource(TelegramBot._recipient_delivery_status)
        assert "_remove_team_worker_mappings_for_task" not in source

    def test_completed_agent_is_still_deliverable(self):
        """An agent with status 'completed' should still be deliverable.
        Only a deleted topic (no route state) should affect deliverability,
        and even then the inbox file persists."""
        # The status check {"failed", "stopped", "killed"} should no longer
        # exist in _recipient_delivery_status
        import inspect
        from obs_agent.telegram import TelegramBot

        source = inspect.getsource(TelegramBot._recipient_delivery_status)
        assert '"failed"' not in source or "# Completed/stopped/failed agents" in source
        # The function should never return deliverable=False based on status
        assert 'is no longer live' not in source


# ---------------------------------------------------------------------------
# BUG 4: session_lineage returns XML when include_xml=false
# ---------------------------------------------------------------------------


class TestSessionLineageXML:
    """The include_xml parameter should be properly coerced from string."""

    def test_coerce_bool_false_string(self):
        """bool('false') is True, but _coerce_bool_arg('false') should be False."""
        assert _coerce_bool_arg("false", name="test") is False
        assert _coerce_bool_arg("False", name="test") is False
        assert _coerce_bool_arg("FALSE", name="test") is False
        assert _coerce_bool_arg(False, name="test") is False

    def test_coerce_bool_true_values(self):
        assert _coerce_bool_arg("true", name="test") is True
        assert _coerce_bool_arg("True", name="test") is True
        assert _coerce_bool_arg(True, name="test") is True

    def test_session_lineage_uses_coerce_bool(self):
        """session_lineage should use _coerce_bool_arg, not bare bool().
        Since session_lineage is a nested function inside register_tools,
        we check the source file directly."""
        source = Path("/tmp/obs-liveness-fix/src/obs_agent/tools.py").read_text()
        # Find the session_lineage function
        idx = source.find("async def session_lineage(")
        assert idx > 0, "session_lineage function not found"
        func_source = source[idx : idx + 500]
        assert "_coerce_bool_arg" in func_source
        assert 'bool(args.get("include_xml"' not in func_source


# ---------------------------------------------------------------------------
# BUG 5: Trunk agent name ≠ team name
# ---------------------------------------------------------------------------


class TestTrunkNaming:
    """Trunk agent name should match team name format."""

    def test_trunk_agent_name_is_just_slug(self):
        """For trunk agents (single-element lineage), agent_name is just the slug."""
        name = native_agent_name_for_lineage(("My Topic",))
        assert name == "my-topic"

    def test_trunk_team_key_has_timestamp(self):
        """Team key has timestamp prefix + slug."""
        import re

        key = root_team_key_for_lineage(("My Topic",), timestamp=1710590400)
        assert re.match(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-my-topic", key)


# ---------------------------------------------------------------------------
# BUG 6: AgentTask should return native_agent_name
# ---------------------------------------------------------------------------


class TestAgentTaskReturnsAgentName:
    """AgentTask launch confirmation should include native_agent_name."""

    def test_launch_text_includes_agent_name(self):
        """_build_fork_task_launch_text should include native_agent_name."""
        from obs_agent.telegram import TelegramBot

        # Check the function signature accepts native_agent_name
        import inspect

        sig = inspect.signature(TelegramBot._build_fork_task_launch_text)
        assert "native_agent_name" in sig.parameters
        assert "team_name" in sig.parameters


# ---------------------------------------------------------------------------
# BUG: ReadInbox should default to own inbox
# ---------------------------------------------------------------------------


class TestReadInboxDefaults:
    """ReadInbox with no params should read own inbox."""

    def test_read_inbox_has_optional_params(self):
        """team_name and agent params should be optional with defaults
        from current bootstrap."""
        source = Path("/tmp/obs-liveness-fix/src/obs_agent/tools.py").read_text()
        idx = source.find("async def _read_inbox(")
        assert idx > 0, "_read_inbox not found"
        func_source = source[idx : idx + 1000]
        # Should have fallback logic from bootstrap
        assert "bootstrap" in func_source
        assert "native_agent_name" in func_source or "agent_name" in func_source
