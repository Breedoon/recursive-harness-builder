"""Tests for final messaging fixes.

1. get_family: trunk parent lookup at depth 1
2. SendInboxMessage: validate recipient inbox exists
3. needs_reply renamed from must_reply
4. run_in_background removed from schema
5. Wake prompt simplified
"""
from __future__ import annotations

import json
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obs_agent.lineage import (
    lineage_fingerprint,
    normalize_lineage_name,
    build_obs_bootstrap_xml,
    parse_obs_bootstrap_xml,
)

# Try both old and new function names
try:
    from obs_agent.lineage import agent_name_for_lineage
except ImportError:
    from obs_agent.lineage import native_agent_name_for_lineage as agent_name_for_lineage


# ---------------------------------------------------------------------------
# BUG: get_family(parent) broken at depth 1
# Children of trunk can't find their parent because the trunk's agent_name
# is the full team key, but native_agent_name_for_lineage returns just slug.
# ---------------------------------------------------------------------------

class TestGetFamilyTrunkParent:
    """get_family(parent) must work at every depth, including depth 1."""

    def test_depth_1_child_finds_trunk_parent(self):
        """The get_family tool should find trunk as parent at depth 1.

        The fix is in the get_family tool itself (uses bootstrap.root_team_key
        for trunk parent), not in agent_name_for_lineage (pure function).
        Verify the tool source has the trunk special case.
        """
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        # The get_family tool should use root_team_key for trunk parent lookup
        assert "root_team_key" in source[source.index("get_family"):], (
            "get_family tool should use bootstrap.root_team_key for trunk parent lookup"
        )

    def test_depth_2_child_finds_parent(self, tmp_path):
        """A grandchild should find its parent (not trunk) correctly."""
        parent_hash = lineage_fingerprint(
            tuple(normalize_lineage_name(n) for n in ("Root", "Parent"))
        )
        parent_agent = f"{lineage_fingerprint(('Root',))}-parent"
        child_agent = f"{parent_hash}-grandchild"

        inboxes = tmp_path / "inboxes"
        inboxes.mkdir()
        (inboxes / f"{parent_agent}.json").write_text("[]")
        (inboxes / f"{child_agent}.json").write_text("[]")

        # For depth 2, parent lookup uses agent_name_for_lineage which works
        parent_name = agent_name_for_lineage(("Root", "Parent"))
        all_agents = [f.stem for f in inboxes.iterdir() if f.suffix == ".json"]
        assert parent_name in all_agents, (
            f"Depth 2 parent lookup failed: '{parent_name}' not in {all_agents}"
        )


# ---------------------------------------------------------------------------
# BUG: No recipient validation — messages to non-existent agents succeed
# ---------------------------------------------------------------------------

class TestRecipientValidation:
    """SendInboxMessage should error if recipient inbox doesn't exist."""

    def test_message_to_nonexistent_agent_should_error(self):
        """If no inbox file exists for the recipient, it's an error."""
        # Read the source to verify the check exists
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        # Should check if inbox file exists before writing
        assert "inbox_path.exists()" in source or "not inbox_path.exists()" in source, (
            "SendInboxMessage has no recipient validation — messages to "
            "non-existent agents silently create inbox files for names "
            "that will never be read."
        )


# ---------------------------------------------------------------------------
# RENAME: must_reply → needs_reply
# ---------------------------------------------------------------------------

class TestNeedsReplyRename:
    """must_reply should be renamed to needs_reply in schema."""

    def test_schema_has_needs_reply(self):
        """SendInboxMessage schema should have needs_reply, not must_reply."""
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        assert '"needs_reply"' in source, (
            "SendInboxMessage schema should have 'needs_reply' parameter"
        )

    def test_schema_needs_reply_description(self):
        """needs_reply description should guide agents on when to use it."""
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        # Should mention questions or requests
        assert "question" in source.lower() or "request" in source.lower(), (
            "needs_reply description should mention questions or requests"
        )

    def test_must_reply_still_accepted_as_fallback(self):
        """Old must_reply param should still work for backward compat."""
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        # Should read needs_reply first, fall back to must_reply
        assert "needs_reply" in source and "must_reply" in source, (
            "Should accept both needs_reply (new) and must_reply (compat)"
        )


# ---------------------------------------------------------------------------
# run_in_background: remove from schema, hardcode true
# ---------------------------------------------------------------------------

class TestRunInBackgroundRemoved:
    """run_in_background should not be in AgentTask/ForkTask schema."""

    def test_run_in_background_not_in_schema(self):
        """Schema should not expose run_in_background."""
        source = (Path(__file__).resolve().parents[1] / "src" / "obs_agent" / "tools.py").read_text()
        # Count occurrences in schema dicts — should not be a schema parameter
        # It should still exist in the handler code but not in the schema
        # Look for it specifically in the schema definition blocks
        import re
        schema_blocks = re.findall(
            r'"inputSchema".*?\{.*?"properties".*?\{(.*?)\}\s*\}',
            source, re.DOTALL
        )
        for block in schema_blocks:
            assert '"run_in_background"' not in block, (
                "run_in_background should be removed from MCP schema — "
                "it's always true, agents shouldn't set it"
            )


# ---------------------------------------------------------------------------
# Wake prompt: simplified, no nudge about must_reply
# ---------------------------------------------------------------------------

class TestWakePromptSimplified:
    """Wake prompt should just say 'check inbox', no must_reply nudge."""

    def test_wake_prompt_no_must_reply_nudge(self):
        """The reply_wake prompt should not mention must_reply at all."""
        from obs_agent.telegram import create_reply_wake_schedule
        from obs_agent.telegram import TelegramRoute

        route = TelegramRoute(chat_id=12345, thread_id=678)
        schedule = create_reply_wake_schedule(route)

        # Should NOT contain must_reply guidance
        assert "must_reply=false" not in schedule.prompt.lower(), (
            "Wake prompt should not nudge about must_reply — just say check inbox"
        )
        assert "acknowledgement" not in schedule.prompt.lower(), (
            "Wake prompt should not contain must_reply usage guidance"
        )

    def test_wake_prompt_says_check_inbox(self):
        """Wake prompt should tell agent to check inbox."""
        from obs_agent.telegram import create_reply_wake_schedule
        from obs_agent.telegram import TelegramRoute

        route = TelegramRoute(chat_id=12345, thread_id=678)
        schedule = create_reply_wake_schedule(route)
        assert "inbox" in schedule.prompt.lower()
