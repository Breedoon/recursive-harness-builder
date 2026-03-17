"""Tests for reply/messaging bugs discovered 2026-03-17.

Bug 1: Trunk agent_name in XML is just the slug, not the team key
Bug 2: needs_reply="false" (string) treated as truthy → all messages get must_reply:true
Bug 3: Poller nags for messages where replied:true (doesn't check replied field)
Bug 4: Consequence of Bug 2 — replying marks first sender's messages, rest stay unreplied

These tests should FAIL with current code and PASS after fixes.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from obs_agent.lineage import (
    build_obs_bootstrap_xml,
    parse_obs_bootstrap_xml,
    normalize_lineage_name,
    lineage_fingerprint,
)

try:
    from obs_agent.lineage import agent_name_for_lineage
except ImportError:
    from obs_agent.lineage import native_agent_name_for_lineage as agent_name_for_lineage


# ---------------------------------------------------------------------------
# Bug 1: Trunk agent_name in XML doesn't match team key
# ---------------------------------------------------------------------------

class TestBug1TrunkAgentNameInXML:
    """The trunk's agent_name in bootstrap XML must be the full team key."""

    def test_trunk_xml_agent_name_equals_team_key(self):
        """build_obs_bootstrap_xml should write the team key as trunk's agent_name."""
        team_key = "2026-03-17-21-06-test"
        xml = build_obs_bootstrap_xml(
            lineage=("test",),
            origin="trunk_start",
            is_fork=False,
            session_id="sid-1",
            root_team_key=team_key,
            agent_name=team_key,
        )
        root = ET.fromstring(xml)
        nodes = root.findall(".//obs-node")
        assert len(nodes) == 1
        trunk_agent_name = nodes[0].attrib.get("agent_name", "")
        assert trunk_agent_name == team_key, (
            f"BUG 1: Trunk agent_name in XML is '{trunk_agent_name}' "
            f"but should be '{team_key}' (the full team key). "
            f"Descendant agents will try to message '{trunk_agent_name}' "
            f"and fail because the inbox file is named '{team_key}.json'."
        )

    def test_child_xml_agent_name_uses_hash_prefix(self):
        """Non-trunk nodes should still use hash-prefix agent names."""
        team_key = "2026-03-17-21-06-myproject"
        xml = build_obs_bootstrap_xml(
            lineage=("MyProject", "Worker"),
            origin="agent_task_fork",
            is_fork=True,
            session_id="sid-2",
            root_team_key=team_key,
            agent_name="a94a8fe5cc-worker",
        )
        root = ET.fromstring(xml)
        nodes = root.findall(".//obs-node")
        assert len(nodes) == 2
        child_agent_name = nodes[1].attrib.get("agent_name", "")
        # Child should have hash prefix, NOT be the team key
        assert "-" in child_agent_name
        assert child_agent_name != team_key

    def test_parsed_trunk_agent_name_matches_team_key(self):
        """Round-trip: parse the XML back and trunk agent_name = team key."""
        team_key = "2026-03-17-21-06-test"
        xml = build_obs_bootstrap_xml(
            lineage=("test",),
            origin="trunk_start",
            is_fork=False,
            session_id="sid-3",
            root_team_key=team_key,
            agent_name=team_key,
        )
        bootstrap = parse_obs_bootstrap_xml(xml)
        assert bootstrap is not None
        # The bootstrap's agent_name should be the team key
        agent_name = getattr(bootstrap, "agent_name", None) or getattr(bootstrap, "native_agent_name", None)
        assert agent_name == team_key, (
            f"Parsed bootstrap agent_name is '{agent_name}', expected '{team_key}'"
        )


# ---------------------------------------------------------------------------
# Bug 2: needs_reply="false" (string) treated as truthy
# ---------------------------------------------------------------------------

class TestBug2NeedsReplyStringCoercion:
    """needs_reply="false" (a string) must be treated as False, not True."""

    def test_string_false_is_not_truthy(self):
        """bool('false') is True in Python — must use _coerce_bool_arg."""
        # Read the source to verify _coerce_bool_arg is used for needs_reply
        source = Path("/tmp/obs-reply-fix/src/obs_agent/tools.py").read_text()

        # Find the line that reads needs_reply/must_reply
        # It should use _coerce_bool_arg, NOT bare bool()
        import re
        # Look for the pattern: reading needs_reply or must_reply and converting
        needs_reply_lines = [
            line.strip() for line in source.splitlines()
            if ("needs_reply" in line or "must_reply" in line)
            and ("bool(" in line or "_coerce" in line)
            and "args.get" in line
        ]
        assert needs_reply_lines, "No line found that reads needs_reply/must_reply from args"

        # Check that _coerce_bool_arg is used, NOT bare bool()
        for line in needs_reply_lines:
            assert "bool(" not in line or "_coerce_bool_arg" in line, (
                f"BUG 2: Line uses bare bool() for needs_reply: {line!r}. "
                f"bool('false') is True in Python! Must use _coerce_bool_arg."
            )

    def test_detect_must_reply_with_false_string_in_inbox(self):
        """If an inbox message has must_reply as string 'false', it should be treated as False."""
        from obs_agent.tools import detect_must_reply_completions

        # Simulate inbox with string "false" must_reply (what actually gets stored
        # when agents send needs_reply="false" and the bug causes bool("false")=True)
        inbox = [
            {"from": "agent-a", "text": "hello", "must_reply": True, "replied": False},
            {"from": "agent-b", "text": "ack", "must_reply": False, "replied": False},
        ]
        # When B replies to A, only A's message should be marked (it has must_reply:True)
        updated, all_replied = detect_must_reply_completions(inbox, "agent-a")
        assert updated[0]["replied"] is True  # A's must_reply message → replied
        # B's message has must_reply:False — should not affect all_replied
        assert all_replied is True, (
            "Messages with must_reply=False should not count as unreplied obligations"
        )


# ---------------------------------------------------------------------------
# Bug 3: Poller nags for messages where replied:true
# ---------------------------------------------------------------------------

class TestBug3PollerIgnoresRepliedField:
    """The poller should not nag for messages that have been replied to."""

    def test_latest_unread_skips_replied_messages(self):
        """_latest_unread_team_inbox_message should skip messages where replied:true.

        The function iterates inbox entries and skips those with read=true.
        It must ALSO skip entries with must_reply=true AND replied=true.
        """
        source = Path("/tmp/obs-reply-fix/src/obs_agent/telegram.py").read_text()

        # Extract JUST the function body (up to the next method definition)
        start = source.index("def _latest_unread_team_inbox_message(")
        # Find the next "    def " or "    async def " at the same indentation
        rest = source[start:]
        lines = rest.split("\n")
        func_lines = [lines[0]]
        for line in lines[1:]:
            if (line.startswith("    def ") or line.startswith("    async def ")):
                break
            func_lines.append(line)
        func_source = "\n".join(func_lines)

        # The loop body should check `replied` — not just comments
        # Look for actual field access: item.get("replied") or entry.get("replied")
        # or ["replied"] — inside the for loop
        loop_section = func_source[func_source.index("for item in"):]
        assert '.get("replied")' in loop_section or '["replied"]' in loop_section, (
            "BUG 3: _latest_unread_team_inbox_message does not check the 'replied' "
            "field in its loop. Messages with must_reply:true and replied:true still "
            "trigger wake notifications, causing infinite nag loops.\n"
            f"Function loop section:\n{loop_section[:500]}"
        )

    def test_poller_loop_has_replied_check(self):
        """The poller's for-loop must have a `continue` for replied must_reply messages.

        Without this, messages with must_reply:true, replied:true, read:false
        will trigger wake notifications indefinitely.
        """
        source = Path("/tmp/obs-reply-fix/src/obs_agent/telegram.py").read_text()

        # Find _latest_unread_team_inbox_message
        start = source.index("def _latest_unread_team_inbox_message(")
        rest = source[start:]
        lines = rest.split("\n")
        func_lines = [lines[0]]
        for line in lines[1:]:
            if (line.startswith("    def ") or line.startswith("    async def ")):
                break
            func_lines.append(line)
        func_source = "\n".join(func_lines)

        # The function should have a continue statement that checks replied
        # Look for pattern: if ... replied ... continue
        has_replied_skip = (
            "replied" in func_source
            and "continue" in func_source.split("replied", 1)[1][:200]
        )
        assert has_replied_skip, (
            "BUG 3: _latest_unread_team_inbox_message has no 'continue' that "
            "checks 'replied'. The poller will keep waking agents for messages "
            "they already replied to."
        )


# ---------------------------------------------------------------------------
# Bug 4: Replying only marks first message per sender
# (Actually a consequence of Bug 2 — all messages have must_reply:true)
# ---------------------------------------------------------------------------

class TestBug4ReplyMarksAllMessages:
    """Replying to a sender should mark ALL their must_reply messages as replied."""

    def test_reply_marks_all_messages_from_sender(self):
        """When B replies to A, ALL must_reply messages from A should be replied."""
        from obs_agent.tools import detect_must_reply_completions

        inbox = [
            {"from": "agent-a", "text": "msg 1", "must_reply": True, "replied": False},
            {"from": "agent-b", "text": "ack 1", "must_reply": True, "replied": False},
            {"from": "agent-a", "text": "msg 2", "must_reply": True, "replied": False},
            {"from": "agent-a", "text": "msg 3", "must_reply": True, "replied": False},
        ]

        # Agent replies to agent-a → all 3 of A's messages should be marked replied
        updated, all_replied = detect_must_reply_completions(inbox, "agent-a")

        a_messages = [e for e in updated if e["from"] == "agent-a"]
        assert all(e["replied"] is True for e in a_messages), (
            f"BUG 4: Not all messages from agent-a were marked as replied. "
            f"States: {[e['replied'] for e in a_messages]}"
        )

        # B's message should NOT be marked
        b_messages = [e for e in updated if e["from"] == "agent-b"]
        assert all(e["replied"] is False for e in b_messages)

        # all_replied should be False (B still has unreplied)
        assert all_replied is False

    def test_reply_to_both_senders_clears_all(self):
        """After replying to all senders, all_replied should be True."""
        from obs_agent.tools import detect_must_reply_completions

        inbox = [
            {"from": "agent-a", "text": "msg 1", "must_reply": True, "replied": False},
            {"from": "agent-b", "text": "msg 2", "must_reply": True, "replied": False},
        ]

        # Reply to A first
        updated, _ = detect_must_reply_completions(inbox, "agent-a")
        # Then reply to B
        updated, all_replied = detect_must_reply_completions(updated, "agent-b")

        assert all_replied is True, "All obligations should be cleared after replying to both"
        assert all(e["replied"] is True for e in updated)


# ---------------------------------------------------------------------------
# Live smoke test: needs_reply=false messaging
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)
class TestLiveNeedsReplyBehavior:
    """Live test: verify needs_reply=false doesn't trigger phantom wakes."""

    async def test_live_needs_reply_false_no_phantom_wakes(
        self,
        live_tg_forum,
    ) -> None:
        """Send a message with needs_reply=false. Verify NO wake schedule is created."""
        from tests.test_telegram_live_forum_topics import (
            _reset_general,
            _send_and_wait_for_token,
            _wait_for_message_after_containing,
        )
        from tests.test_telegram_live_smoke import (
            _launch_lineage_worker,
            _extract_lineage_fact_line,
            _query_session_lineage,
        )

        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create root
        root_thread = await live_tg_forum.platform.create_topic(f"ReplyTest {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Reply with only REPLY-ROOT-{tag}.",
            thread_id=root_thread,
            token=f"REPLY-ROOT-{tag}",
            timeout=180.0,
        )

        # Get root's agent name
        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"REPLY-LIN-{tag}",
            timeout=240.0,
        )
        root_agent = _extract_lineage_fact_line(root_lineage, "agent_name")
        if not root_agent:
            root_agent = _extract_lineage_fact_line(root_lineage, "native_agent_name")
        assert root_agent, "Could not extract root agent name"

        # Launch child
        child_thread, child_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"ReplyChild-{tag}",
            launch_token=f"REPLY-LAUNCH-{tag}",
            lineage_token=f"REPLY-CHILD-LIN-{tag}",
            timeout=240.0,
        )
        child_agent = _extract_lineage_fact_line(child_lineage, "agent_name")
        if not child_agent:
            child_agent = _extract_lineage_fact_line(child_lineage, "native_agent_name")
        assert child_agent, "Could not extract child agent name"

        # Wait for child to settle
        await asyncio.sleep(15)

        # Root sends a message with needs_reply=false
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=root_thread,
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic test. "
                f"Use SendInboxMessage exactly once with recipient={child_agent}, "
                f"content='NOREPLY-PAYLOAD-{tag}', summary='no-reply-test', "
                "needs_reply=false. "
                f"Reply with only NOREPLY-SENT-{tag}."
            ),
            thread_id=root_thread,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread,
            after_message_id=baseline,
            token=f"NOREPLY-SENT-{tag}",
            timeout=360.0,
        )

        # Wait 30 seconds — enough for 10 poll cycles
        await asyncio.sleep(30)

        # Check child topic for phantom wake messages
        child_baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=child_thread,
        )

        # Wait another 15 seconds to see if phantom wakes arrive
        await asyncio.sleep(15)

        recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=child_thread,
            limit=20,
        )
        # Count messages that look like nag/wake notifications
        nag_messages = [
            m for m in recent
            if m.message_id > child_baseline
            and ("haven't replied" in m.text.lower()
                 or "unreplied" in m.text.lower()
                 or "must_reply" in m.text.lower()
                 or "needs_reply" in m.text.lower())
        ]

        assert len(nag_messages) == 0, (
            f"PHANTOM WAKE: {len(nag_messages)} nag messages appeared after sending "
            f"needs_reply=false. The message should NOT trigger a reply_wake schedule.\n"
            + "\n".join(f"  [{m.message_id}] {m.text[:120]}" for m in nag_messages)
        )
