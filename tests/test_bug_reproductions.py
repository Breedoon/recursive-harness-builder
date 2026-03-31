"""Bug reproduction tests — these should FAIL until the bugs are fixed.

Each test reproduces a specific production bug discovered during the
naming redesign + must_reply rollout. Tests are marked xfail with
strict=True so they FAIL LOUDLY if the bug is accidentally "fixed"
without proper verification.

When the implementer fixes a bug, the corresponding xfail should be
removed and the test should pass cleanly.

Bugs reproduced:
1. Phantom notification loop — poller re-triggers for same unread message
2. /delete_all freezes daemon
3. Deleted topics redirect messages to General
4. Completed agents not messageable (premature death)
5. Trunk agent name != team name

Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.evals.platform_telegram_forum import TelegramForumPlatform
from tests.test_telegram_live_forum_topics import (
    _extract_topic_link,
    _message_containing,
    _reset_general,
    _send_and_wait_for_token,
    _start_bot,
    _stop_bot,
    _wait_for_message_after_containing,
    _wait_for_message_containing,
    _LiveForumHarness,
    live_tg_forum,  # fixture import
)
from tests.test_telegram_live_smoke import (
    _extract_lineage_fact_line,
    _launch_lineage_worker,
    _query_session_lineage,
    _send_inbox_message_and_wait_ack,
    _wait_for_message_after_any_token,
)


# ---------------------------------------------------------------------------
# BUG 1: Phantom notification loop
# The inbox poller (_poll_team_worker_inbox_wakes) runs every 3s and
# re-triggers "New teammate messages arrived" for the SAME unread message
# until the SDK marks it read. No deduplication.
#
# Expected behavior: a single inbox message should produce exactly ONE
# wake notification, not repeated notifications every 3 seconds.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)
class TestBug1PhantomNotificationLoop:
    """Reproduce: inbox poller re-triggers same notification repeatedly."""

    @pytest.mark.xfail(
        reason="BUG: _poll_team_worker_inbox_wakes has no deduplication — "
               "re-triggers for same unread message every 3s poll cycle",
        strict=True,
    )
    async def test_single_message_produces_single_wake(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Send one inbox message to a child. Count wake notifications.

        Should be exactly 1 wake. Bug causes multiple (one per poll cycle).
        """
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create root
        root_thread = await live_tg_forum.platform.create_topic(f"Phantom {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic phantom test. Reply with only PHANTOM-ROOT-{tag}.",
            thread_id=root_thread,
            token=f"PHANTOM-ROOT-{tag}",
            timeout=180.0,
        )

        # Launch child
        child_thread, child_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"PhantomChild-{tag}",
            launch_token=f"PHANTOM-LAUNCH-{tag}",
            lineage_token=f"PHANTOM-LIN-{tag}",
            timeout=240.0,
        )
        child_agent = _extract_lineage_fact_line(child_lineage, "agent_name")
        assert child_agent, "Could not extract child agent_name"

        # Wait for child to finish initial turn and go idle
        await asyncio.sleep(15)

        # Record baseline — how many messages exist in child topic now
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=child_thread,
        )

        # Root sends ONE inbox message to child
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=child_agent,
            content=f"SINGLE-MSG-{tag}",
            ack_token=f"MSG-SENT-{tag}",
            timeout=240.0,
        )

        # Wait 30 seconds — enough for 10 poll cycles at 3s each
        # If the bug exists, we'll see multiple wake notifications
        await asyncio.sleep(30)

        # Count new bot messages in child topic since baseline
        recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=child_thread,
            limit=40,
        )
        new_messages = [m for m in recent if m.message_id > baseline]

        # Count messages that contain the "teammate messages arrived" wake pattern
        wake_messages = [
            m for m in new_messages
            if "teammate" in m.text.lower()
            or "new teammate" in m.text.lower()
            or "unreplied must_reply" in m.text.lower()
        ]

        # ASSERTION: Should be at most 1 wake notification for 1 message.
        # The bug causes this to be 5-10+ (one per 3s poll cycle).
        assert len(wake_messages) <= 1, (
            f"PHANTOM BUG REPRODUCED: Got {len(wake_messages)} wake notifications "
            f"for a single inbox message (expected ≤1). "
            f"The poller has no deduplication.\n"
            + "\n".join(f"  [{m.message_id}] {m.text[:120]}" for m in wake_messages)
        )


# ---------------------------------------------------------------------------
# BUG 2: /delete_all freezes the daemon
# Running /delete_all reportedly causes the daemon to hang.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(300)
class TestBug2DeleteAllFreeze:
    """Reproduce: /delete_all command freezes the daemon."""

    @pytest.mark.xfail(
        reason="BUG: /delete_all may freeze the daemon — needs investigation",
        strict=True,
    )
    async def test_delete_all_does_not_freeze_daemon(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Create several topics, run /delete_all, verify daemon survives."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create 3 topics
        threads = []
        for i in range(3):
            t = await live_tg_forum.platform.create_topic(f"DelAll-{i}-{tag}")
            await _send_and_wait_for_token(
                live_tg_forum,
                text=f"Reply with only DELALL-{i}-{tag}.",
                thread_id=t,
                token=f"DELALL-{i}-{tag}",
                timeout=120.0,
            )
            threads.append(t)

        # Run /delete_all from the general topic
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            "/delete_all",
            thread_id=None,
            require_done=False,
            timeout=60.0,
        )

        # Wait for a response — if daemon freezes, this times out
        await asyncio.sleep(30)

        # Verify daemon is still responsive by sending a message
        alive_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            f"If you can read this, reply with exactly ALIVE-{tag}.",
            thread_id=None,
            require_done=False,
            timeout=60.0,
        )

        try:
            await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=None,
                after_message_id=alive_baseline,
                token=f"ALIVE-{tag}",
                timeout=60.0,
            )
        except (asyncio.TimeoutError, Exception):
            pytest.fail(
                "DAEMON FREEZE REPRODUCED: After /delete_all, the daemon "
                "stopped responding to messages in General. The daemon is hung."
            )


# ---------------------------------------------------------------------------
# BUG 3: Deleted topics redirect messages to General
# When a user deletes a topic, Telegram returns "message thread not found"
# but the code doesn't catch it — the error propagates and messages
# end up in General instead of being blocked.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)
class TestBug3DeletedTopicRedirectsToGeneral:
    """Reproduce: messages to deleted topics appear in General."""

    @pytest.mark.xfail(
        reason="BUG: 'message thread not found' not caught — messages redirect to General",
        strict=True,
    )
    async def test_deleted_topic_does_not_redirect_to_general(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Delete a child topic, send it a message, verify it does NOT
        appear in General.
        """
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create root and child
        root_thread = await live_tg_forum.platform.create_topic(f"Redirect {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Reply with only REDIRECT-ROOT-{tag}.",
            thread_id=root_thread,
            token=f"REDIRECT-ROOT-{tag}",
            timeout=180.0,
        )

        child_thread, child_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"RedirectChild-{tag}",
            launch_token=f"REDIRECT-LAUNCH-{tag}",
            lineage_token=f"REDIRECT-LIN-{tag}",
            timeout=240.0,
        )
        child_agent = _extract_lineage_fact_line(child_lineage, "agent_name")
        assert child_agent, "Could not extract child agent_name"

        # Wait for child to be active
        await asyncio.sleep(10)

        # Delete the child's topic via Telethon
        await live_tg_forum.platform.delete_topic(child_thread)
        await asyncio.sleep(5)

        # Record General baseline
        general_baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=None,
        )

        # Root sends inbox message to the (now deleted) child
        root_baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=root_thread,
        )
        await live_tg_forum.platform.send(
            (
                f"Use SendInboxMessage with recipient={child_agent}, "
                f"content='POST-DELETE-{tag}', summary='test', must_reply=false. "
                f"Reply with REDIRECT-SENT-{tag}."
            ),
            thread_id=root_thread,
            require_done=False,
            timeout=120.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread,
            after_message_id=root_baseline,
            token=f"REDIRECT-SENT-{tag}",
            timeout=180.0,
        )

        # Wait for any redirect to propagate
        await asyncio.sleep(15)

        # Check General for any messages from the bot that mention the payload
        general_recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=None,
            limit=20,
        )
        general_new = [
            m for m in general_recent
            if m.message_id > general_baseline
        ]
        redirected = [
            m for m in general_new
            if f"POST-DELETE-{tag}" in m.text
            or "teammate" in m.text.lower()
        ]

        assert len(redirected) == 0, (
            f"REDIRECT BUG REPRODUCED: {len(redirected)} messages appeared in General "
            f"after sending to a deleted topic. Messages should NOT redirect.\n"
            + "\n".join(f"  [{m.message_id}] {m.text[:120]}" for m in redirected)
        )


# ---------------------------------------------------------------------------
# BUG 3b: Bounce-back notification for dead agents
# When a topic is deleted and the bot discovers it (via "message thread not
# found"), it should notify all agents who sent unread messages to the dead
# agent. The notification should say the agent is dead and the message may
# not be read. must_reply obligations should be voided.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)  # 15 minutes — complex multi-agent scenario
class TestBug3bBounceBackForDeadAgent:
    """When a topic is deleted, senders get notified that the agent is dead."""

    @pytest.mark.xfail(
        reason="NOT IMPLEMENTED: bounce-back notifications for dead agents",
        strict=True,
    )
    async def test_senders_notified_when_agent_topic_deleted(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Build Root → Child, Root → Sibling. Both message Child.
        Delete Child's topic. Both Root and Sibling should get a bounce-back
        notification in their inboxes saying Child is dead.
        """
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create root
        root_thread = await live_tg_forum.platform.create_topic(f"Bounce {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Reply with only BOUNCE-ROOT-{tag}.",
            thread_id=root_thread,
            token=f"BOUNCE-ROOT-{tag}",
            timeout=180.0,
        )

        # Launch Child and Sibling
        child_thread, child_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"BounceChild-{tag}",
            launch_token=f"BOUNCE-LAUNCH-CHILD-{tag}",
            lineage_token=f"BOUNCE-LIN-CHILD-{tag}",
            timeout=240.0,
        )
        child_agent = _extract_lineage_fact_line(child_lineage, "agent_name")
        assert child_agent, "Could not extract child agent_name"

        sibling_thread, sibling_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"BounceSibling-{tag}",
            launch_token=f"BOUNCE-LAUNCH-SIB-{tag}",
            lineage_token=f"BOUNCE-LIN-SIB-{tag}",
            timeout=240.0,
        )

        # Wait for agents to settle
        await asyncio.sleep(10)

        # Root sends message to Child
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=child_agent,
            content=f"BOUNCE-FROM-ROOT-{tag}",
            ack_token=f"BOUNCE-ROOT-SENT-{tag}",
            timeout=240.0,
        )

        # Sibling sends message to Child
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=sibling_thread,
            recipient=child_agent,
            content=f"BOUNCE-FROM-SIB-{tag}",
            ack_token=f"BOUNCE-SIB-SENT-{tag}",
            timeout=240.0,
        )

        # Delete Child's topic
        await live_tg_forum.platform.delete_topic(child_thread)
        await asyncio.sleep(5)

        # Trigger the bounce-back by having Root try to wake Child
        # (the bot will try to send to the deleted topic and discover it's gone)
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=child_agent,
            content=f"BOUNCE-TRIGGER-{tag}",
            ack_token=f"BOUNCE-TRIGGER-SENT-{tag}",
            timeout=240.0,
        )

        # Wait for bounce-back to propagate
        await asyncio.sleep(15)

        # Check Root's inbox for a bounce-back notification
        from tests.test_telegram_live_stress import _check_inbox_for_content
        root_got_bounce = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=root_thread,
            look_for="dead",
            found_token=f"ROOT-BOUNCE-YES-{tag}",
            missing_token=f"ROOT-BOUNCE-NO-{tag}",
            timeout=120.0,
        )

        # Check Sibling's inbox for a bounce-back notification
        sib_got_bounce = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=sibling_thread,
            look_for="dead",
            found_token=f"SIB-BOUNCE-YES-{tag}",
            missing_token=f"SIB-BOUNCE-NO-{tag}",
            timeout=120.0,
        )

        assert root_got_bounce, (
            "BOUNCE-BACK MISSING: Root sent messages to Child, Child's topic was "
            "deleted, but Root did NOT receive a bounce-back notification."
        )
        assert sib_got_bounce, (
            "BOUNCE-BACK MISSING: Sibling sent messages to Child, Child's topic was "
            "deleted, but Sibling did NOT receive a bounce-back notification."
        )


# ---------------------------------------------------------------------------
# BUG 4: Trunk agent name != team name
# Design spec says they should be identical.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(300)
class TestBug4TrunkNameEqualsTeamName:
    """Verify trunk agent_name matches the team name slug."""

    @pytest.mark.xfail(
        reason="BUG: trunk agent_name and team name diverge — should be identical slug",
        strict=True,
    )
    async def test_trunk_agent_name_matches_team_name_slug(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        thread_id = await live_tg_forum.platform.create_topic(f"TrunkName {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Reply with only TRUNK-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"TRUNK-PRIME-{tag}",
            timeout=180.0,
        )

        lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=thread_id,
            token=f"TRUNK-LIN-{tag}",
            timeout=240.0,
        )

        agent_name = _extract_lineage_fact_line(lineage, "agent_name")
        team_name = _extract_lineage_fact_line(lineage, "root_team_key")
        assert agent_name, "Could not extract agent_name"
        assert team_name, "Could not extract team_name"

        # Extract the slug part from team name (after timestamp prefix)
        # Format: YYYY-MM-DD-HH-MM-{slug}
        team_slug = re.sub(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-", "", team_name)

        assert agent_name == team_slug, (
            f"TRUNK NAME BUG: trunk agent_name '{agent_name}' != "
            f"team name slug '{team_slug}' (from team_name '{team_name}'). "
            f"They should be identical."
        )


# ---------------------------------------------------------------------------
# BUG 5: Daemon restart creates new team instead of restoring old one
# After daemon restart, the trunk agent gets a NEW team key (new timestamp)
# instead of restoring the persisted one from the JSONL. This breaks all
# inbox routing — messages go to the wrong team directory.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)
class TestBug5RestartChangesTeam:
    """Reproduce: daemon restart creates new team instead of restoring."""

    @pytest.mark.xfail(
        reason="BUG: daemon restart generates new timestamp team key "
               "instead of restoring from JSONL — breaks inbox routing",
        strict=True,
    )
    async def test_team_key_survives_daemon_restart(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Create a session, record team key, restart bot, verify same key."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create a topic and get its team key
        thread_id = await live_tg_forum.platform.create_topic(f"Restart {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Reply with only RESTART-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"RESTART-PRIME-{tag}",
            timeout=180.0,
        )

        lineage_before = await _query_session_lineage(
            live_tg_forum,
            thread_id=thread_id,
            token=f"RESTART-LIN1-{tag}",
            timeout=240.0,
        )
        team_before = _extract_lineage_fact_line(lineage_before, "root_team_key")
        agent_before = _extract_lineage_fact_line(lineage_before, "agent_name")
        assert team_before, "Could not extract team key before restart"
        assert agent_before, "Could not extract agent name before restart"

        # Restart the bot
        _stop_bot(live_tg_forum.proc)
        await asyncio.sleep(3)
        new_proc, new_log = _start_bot(
            live_tg_forum.vault_path,
            live_tg_forum.temp_root,
            state_db_path=live_tg_forum.state_db_path,
        )
        live_tg_forum.proc = new_proc
        live_tg_forum.log_file = new_log
        await asyncio.sleep(10)  # Let bot reconnect

        # Send a message to the same topic — should restore the session
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"After restart. Reply with only RESTART-AFTER-{tag}.",
            thread_id=thread_id,
            token=f"RESTART-AFTER-{tag}",
            timeout=180.0,
        )

        lineage_after = await _query_session_lineage(
            live_tg_forum,
            thread_id=thread_id,
            token=f"RESTART-LIN2-{tag}",
            timeout=240.0,
        )
        team_after = _extract_lineage_fact_line(lineage_after, "root_team_key")
        agent_after = _extract_lineage_fact_line(lineage_after, "agent_name")

        assert team_after == team_before, (
            f"RESTART TEAM BUG: team key changed after restart! "
            f"Before: {team_before}, After: {team_after}. "
            f"Team key should be deterministically restored from JSONL."
        )
        assert agent_after == agent_before, (
            f"RESTART AGENT BUG: agent name changed after restart! "
            f"Before: {agent_before}, After: {agent_after}."
        )
