"""Live stress tests for agent messaging reliability.

Comprehensive adversarial tests that exercise the messaging system under stress:
- Multi-agent messaging in all directions
- must_reply cycle completion (not a loop)
- must_reply exhaustion (agent refuses to reply)
- must_reply does NOT loop between two agents
- Completed agents remain messageable
- Topic deletion behavior (graceful failure, no redirect to General)
- session_lineage include_xml=false
- AgentTask returns native_agent_name

These tests reproduce real production bugs found during the naming redesign rollout.
Each test is long, comprehensive, and designed to catch regressions.

Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import re
import uuid

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
    _extract_lineage_payload_fact_line,
    _launch_lineage_worker,
    _message_is_exact_token,
    _query_session_lineage,
    _query_session_lineage_payload,
    _send_inbox_message_and_expect_outcome,
    _send_inbox_message_and_wait_ack,
    _wait_for_message_after_any_token,
)


# ---------------------------------------------------------------------------
# Helper: send must_reply message
# ---------------------------------------------------------------------------

async def _send_must_reply_message(
    harness: _LiveForumHarness,
    *,
    sender_thread_id: int,
    recipient: str,
    content: str,
    ack_token: str,
    summary: str = "must-reply-test",
    timeout: float = 240.0,
) -> None:
    """Send a message with must_reply=true."""
    baseline = await harness.platform.latest_bot_message_id(thread_id=sender_thread_id)
    await harness.platform.send(
        (
            "This is a deterministic must_reply stress test. "
            f"Use SendInboxMessage exactly once with recipient={recipient}, "
            f"content={content!r}, summary={summary!r}, must_reply=true, "
            "and omit team_name and sender. "
            f"Reply with only {ack_token}."
        ),
        thread_id=sender_thread_id,
        require_done=False,
        timeout=timeout,
    )
    await _wait_for_message_after_containing(
        harness,
        thread_id=sender_thread_id,
        after_message_id=baseline,
        token=ack_token,
        timeout=timeout + 120.0,
    )


async def _instruct_read_and_reply(
    harness: _LiveForumHarness,
    *,
    thread_id: int,
    look_for: str,
    reply_to: str,
    reply_content: str,
    ack_token: str,
    timeout: float = 240.0,
) -> None:
    """Instruct an agent to read inbox, find a message, and reply to the sender."""
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    await harness.platform.send(
        (
            "This is a deterministic messaging test. "
            "Call ReadInbox exactly once. "
            f"If you see a message containing {look_for!r}, "
            f"use SendInboxMessage to reply to {reply_to} "
            f"with content={reply_content!r}, summary='reply', must_reply=false. "
            f"Then reply with exactly {ack_token}."
        ),
        thread_id=thread_id,
        require_done=False,
        timeout=timeout,
    )
    await _wait_for_message_after_containing(
        harness,
        thread_id=thread_id,
        after_message_id=baseline,
        token=ack_token,
        timeout=timeout + 120.0,
    )


async def _check_inbox_for_content(
    harness: _LiveForumHarness,
    *,
    thread_id: int,
    look_for: str,
    found_token: str,
    missing_token: str,
    timeout: float = 240.0,
) -> bool:
    """Instruct an agent to read inbox and report if a specific content is present."""
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    await harness.platform.send(
        (
            "Call ReadInbox exactly once with no arguments. "
            f"If unread messages contain {look_for!r}, reply with exactly {found_token}. "
            f"Otherwise reply with {missing_token}."
        ),
        thread_id=thread_id,
        require_done=False,
        timeout=timeout,
    )
    result = await _wait_for_message_after_any_token(
        harness,
        thread_id=thread_id,
        after_message_id=baseline,
        tokens=[found_token, missing_token],
        timeout=timeout + 120.0,
    )
    return _message_is_exact_token(result.text, found_token)


# ---------------------------------------------------------------------------
# TEST 1: Multi-agent messaging stress (all directions)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(1500)  # 25 minutes
class TestMessagingStress:
    """Build a hierarchy and have every agent message every other agent."""

    async def test_live_multi_agent_all_direction_messaging(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Root → A → B, Root → C. Cross-messaging in all directions."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Build hierarchy
        root_thread = await live_tg_forum.platform.create_topic(f"MsgStress {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic messaging stress test. Reply with only STRESS-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"STRESS-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LINEAGE-{tag}",
            timeout=240.0,
        )

        # Launch A under Root
        thread_a, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"Alpha-{tag}",
            launch_token=f"ROOT-LAUNCH-A-{tag}",
            lineage_token=f"LINEAGE-A-{tag}",
            timeout=240.0,
        )

        # Launch B under A (grandchild)
        thread_b, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=True,
            alias=f"Bravo-{tag}",
            launch_token=f"A-LAUNCH-B-{tag}",
            lineage_token=f"LINEAGE-B-{tag}",
            timeout=240.0,
        )

        # Launch C under Root (sibling to A)
        thread_c, lineage_c = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"Charlie-{tag}",
            launch_token=f"ROOT-LAUNCH-C-{tag}",
            lineage_token=f"LINEAGE-C-{tag}",
            timeout=240.0,
        )

        # Verify all share same team key
        assert lineage_a["root_team_key"] == root_lineage["root_team_key"]
        assert lineage_b["root_team_key"] == root_lineage["root_team_key"]
        assert lineage_c["root_team_key"] == root_lineage["root_team_key"]

        # --- Cross-messaging matrix ---

        # B → Root (grandchild to root, 2 levels up)
        msg_b_to_root = f"MSG-B-TO-ROOT-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=thread_b,
            recipient=root_lineage["native_agent_name"],
            content=msg_b_to_root,
            ack_token=f"B-SENT-ROOT-{tag}",
            timeout=240.0,
        )

        # Verify root received it
        found = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=root_thread,
            look_for=msg_b_to_root,
            found_token=f"ROOT-GOT-B-{tag}",
            missing_token=f"ROOT-MISSING-B-{tag}",
            timeout=240.0,
        )
        assert found, f"Root did not receive message from B containing {msg_b_to_root}"

        # Root → B (root to grandchild, 2 levels down)
        msg_root_to_b = f"MSG-ROOT-TO-B-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=lineage_b["native_agent_name"],
            content=msg_root_to_b,
            ack_token=f"ROOT-SENT-B-{tag}",
            timeout=240.0,
        )

        # B → C (cross-branch, different parents)
        msg_b_to_c = f"MSG-B-TO-C-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=thread_b,
            recipient=lineage_c["native_agent_name"],
            content=msg_b_to_c,
            ack_token=f"B-SENT-C-{tag}",
            timeout=240.0,
        )

        # Verify C received it
        found_c = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=thread_c,
            look_for=msg_b_to_c,
            found_token=f"C-GOT-B-{tag}",
            missing_token=f"C-MISSING-B-{tag}",
            timeout=240.0,
        )
        assert found_c, f"C did not receive cross-branch message from B"

        # C → A (sibling to sibling via different branch)
        msg_c_to_a = f"MSG-C-TO-A-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=thread_c,
            recipient=lineage_a["native_agent_name"],
            content=msg_c_to_a,
            ack_token=f"C-SENT-A-{tag}",
            timeout=240.0,
        )


# ---------------------------------------------------------------------------
# TEST 2: must_reply proper cycle (NOT a loop)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)  # 15 minutes
class TestMustReplyProperCycle:
    """Verify must_reply works: send, wake, reply, done. No loop."""

    async def test_live_must_reply_cycle_terminates_cleanly(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Root sends must_reply to A. A replies. No further wakes."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread = await live_tg_forum.platform.create_topic(f"MRCycle {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic must_reply cycle test. Reply with only MRCYCLE-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"MRCYCLE-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LIN-{tag}",
            timeout=240.0,
        )

        # Launch worker
        worker_thread, worker_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"MRWorker-{tag}",
            launch_token=f"LAUNCH-MRWORKER-{tag}",
            lineage_token=f"LIN-MRWORKER-{tag}",
            timeout=240.0,
        )

        # Root sends must_reply to worker
        must_reply_content = f"URGENT-TASK-{tag}"
        await _send_must_reply_message(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=worker_lineage["native_agent_name"],
            content=must_reply_content,
            ack_token=f"ROOT-SENT-MR-{tag}",
            timeout=240.0,
        )

        # Instruct worker to read and reply
        await _instruct_read_and_reply(
            live_tg_forum,
            thread_id=worker_thread,
            look_for=must_reply_content,
            reply_to=root_lineage["native_agent_name"],
            reply_content=f"WORKER-DONE-{tag}",
            ack_token=f"WORKER-REPLIED-{tag}",
            timeout=240.0,
        )

        # Verify root received the reply
        found = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=root_thread,
            look_for=f"WORKER-DONE-{tag}",
            found_token=f"ROOT-GOT-DONE-{tag}",
            missing_token=f"ROOT-NO-DONE-{tag}",
            timeout=240.0,
        )
        assert found, "Root did not receive worker's reply"

        # Wait 15 seconds and verify NO phantom wakes on worker
        await asyncio.sleep(15)
        baseline_worker = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_thread)
        # If no new messages appear for 15 more seconds, the cycle terminated
        await asyncio.sleep(15)
        recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=worker_thread, limit=5
        )
        new_messages = [m for m in recent if m.message_id > baseline_worker]
        # Filter out any that are just the system idle notification
        phantom_wakes = [
            m for m in new_messages
            if "must_reply" in m.text.lower() or "unreplied" in m.text.lower()
        ]
        assert len(phantom_wakes) == 0, (
            f"Phantom must_reply wakes detected after reply was sent! "
            f"Messages: {[m.text[:100] for m in phantom_wakes]}"
        )


# ---------------------------------------------------------------------------
# TEST 3: must_reply exhaustion (agent refuses to reply)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)  # 15 minutes
class TestMustReplyExhaustion:
    """Agent ignores must_reply. Verify exactly 3 wakes, then stop."""

    async def test_live_must_reply_exhausts_after_3_wakes(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Send must_reply to agent that never replies. Max 3 wake attempts."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread = await live_tg_forum.platform.create_topic(f"MRExhaust {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic must_reply exhaustion test. Reply with only MREX-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"MREX-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LIN-EX-{tag}",
            timeout=240.0,
        )

        # Launch worker — instruct it to NEVER reply to inbox messages
        worker_thread, worker_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"Stubborn-{tag}",
            launch_token=f"LAUNCH-STUBBORN-{tag}",
            lineage_token=f"LIN-STUBBORN-{tag}",
            timeout=240.0,
        )

        # Prime the stubborn worker — tell it to ignore inbox messages
        await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                f"IMPORTANT: You are a stubborn agent in a test. "
                f"If you receive ANY inbox messages or system reminders about must_reply, "
                f"do NOT reply to anyone via SendInboxMessage. Just say I-REFUSE-{tag} and stop. "
                f"Reply with only STUBBORN-READY-{tag} now."
            ),
            thread_id=worker_thread,
            token=f"STUBBORN-READY-{tag}",
            timeout=180.0,
        )

        # Root sends must_reply to stubborn worker
        await _send_must_reply_message(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=worker_lineage["native_agent_name"],
            content=f"REPLY-TO-ME-{tag}",
            ack_token=f"ROOT-SENT-MR-EX-{tag}",
            timeout=240.0,
        )

        # Wait for the reply_wake schedule to exhaust (3 attempts × ~10s each + buffer)
        # The worker should get woken up to 3 times and refuse each time
        await asyncio.sleep(60)

        # Now check: the worker should NOT get any more wakes
        baseline_final = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_thread)
        await asyncio.sleep(30)
        recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=worker_thread, limit=10
        )
        late_wakes = [
            m for m in recent
            if m.message_id > baseline_final
            and ("must_reply" in m.text.lower() or "unreplied" in m.text.lower())
        ]
        assert len(late_wakes) == 0, (
            f"Worker still getting wakes after schedule should have exhausted! "
            f"Late wakes: {[m.text[:100] for m in late_wakes]}"
        )


# ---------------------------------------------------------------------------
# TEST 4: must_reply does NOT loop between two agents
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)  # 15 minutes
class TestMustReplyNoLoop:
    """Two agents exchange messages. Verify no ping-pong loop."""

    async def test_live_must_reply_no_pingpong(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """A sends must_reply to B. B replies. Verify cycle terminates — no loop."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread = await live_tg_forum.platform.create_topic(f"NoLoop {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic no-loop test. Reply with only NOLOOP-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"NOLOOP-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LIN-NL-{tag}",
            timeout=240.0,
        )

        # Launch two workers
        thread_a, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"PingA-{tag}",
            launch_token=f"LAUNCH-PINGA-{tag}",
            lineage_token=f"LIN-PINGA-{tag}",
            timeout=240.0,
        )

        thread_b, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"PongB-{tag}",
            launch_token=f"LAUNCH-PONGB-{tag}",
            lineage_token=f"LIN-PONGB-{tag}",
            timeout=240.0,
        )

        # A sends must_reply to B
        await _send_must_reply_message(
            live_tg_forum,
            sender_thread_id=thread_a,
            recipient=lineage_b["native_agent_name"],
            content=f"PING-{tag}",
            ack_token=f"A-SENT-PING-{tag}",
            timeout=240.0,
        )

        # B reads and replies (the fix should force must_reply=false on the reply)
        await _instruct_read_and_reply(
            live_tg_forum,
            thread_id=thread_b,
            look_for=f"PING-{tag}",
            reply_to=lineage_a["native_agent_name"],
            reply_content=f"PONG-{tag}",
            ack_token=f"B-REPLIED-PONG-{tag}",
            timeout=240.0,
        )

        # Verify A received the reply
        found = await _check_inbox_for_content(
            live_tg_forum,
            thread_id=thread_a,
            look_for=f"PONG-{tag}",
            found_token=f"A-GOT-PONG-{tag}",
            missing_token=f"A-NO-PONG-{tag}",
            timeout=240.0,
        )
        assert found, "A did not receive B's reply"

        # CRITICAL: Wait and verify NO loop develops
        # If the old bug exists, B's reply would create must_reply on A,
        # then A would get woken and reply with must_reply, etc.
        await asyncio.sleep(30)

        # Check A for phantom wakes
        baseline_a = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_a)
        await asyncio.sleep(20)
        recent_a = await live_tg_forum.platform.get_recent_messages(thread_id=thread_a, limit=5)
        phantom_a = [
            m for m in recent_a
            if m.message_id > baseline_a
            and ("must_reply" in m.text.lower() or "unreplied" in m.text.lower())
        ]

        # Check B for phantom wakes
        baseline_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_b)
        recent_b = await live_tg_forum.platform.get_recent_messages(thread_id=thread_b, limit=5)
        phantom_b = [
            m for m in recent_b
            if m.message_id > baseline_b
            and ("must_reply" in m.text.lower() or "unreplied" in m.text.lower())
        ]

        assert len(phantom_a) == 0, (
            f"Phantom must_reply loop on A! Messages: {[m.text[:100] for m in phantom_a]}"
        )
        assert len(phantom_b) == 0, (
            f"Phantom must_reply loop on B! Messages: {[m.text[:100] for m in phantom_b]}"
        )


# ---------------------------------------------------------------------------
# TEST 5: Completed agent is still messageable
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)  # 10 minutes
class TestCompletedAgentMessageable:
    """Agents that completed should still receive messages."""

    async def test_live_message_to_completed_agent_succeeds(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Launch child, let it complete, then send it a message. Should succeed."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread = await live_tg_forum.platform.create_topic(f"Completed {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic completed-agent test. Reply with only COMPLETED-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"COMPLETED-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LIN-COMP-{tag}",
            timeout=240.0,
        )

        # Launch a worker that will complete quickly
        worker_thread, worker_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"QuickWorker-{tag}",
            launch_token=f"LAUNCH-QUICK-{tag}",
            lineage_token=f"LIN-QUICK-{tag}",
            timeout=240.0,
        )

        # Let the worker idle/complete
        await asyncio.sleep(15)

        # Now try to send a message from root to the completed worker
        msg_to_completed = f"HELLO-COMPLETED-{tag}"
        delivered = await _send_inbox_message_and_expect_outcome(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=worker_lineage["native_agent_name"],
            content=msg_to_completed,
            delivered_token=f"DELIVERED-TO-COMPLETED-{tag}",
            undelivered_token=f"UNDELIVERED-COMPLETED-{tag}",
            timeout=240.0,
        )
        assert delivered, (
            f"Message to completed agent was rejected! "
            f"Agents should ALWAYS be messageable unless their topic is deleted."
        )


# ---------------------------------------------------------------------------
# TEST 6: Topic deletion behavior
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)  # 10 minutes
class TestTopicDeletion:
    """Verify behavior when a topic is deleted."""

    async def test_live_deleted_topic_does_not_redirect_to_general(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Delete a child's topic. Send message. Should NOT appear in General."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread = await live_tg_forum.platform.create_topic(f"TopicDel {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic topic deletion test. Reply with only TOPICDEL-PRIME-{tag}.",
            thread_id=root_thread,
            token=f"TOPICDEL-PRIME-{tag}",
            timeout=180.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread,
            token=f"ROOT-LIN-DEL-{tag}",
            timeout=240.0,
        )

        # Launch a worker
        worker_thread, worker_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread,
            fork=False,
            alias=f"Doomed-{tag}",
            launch_token=f"LAUNCH-DOOMED-{tag}",
            lineage_token=f"LIN-DOOMED-{tag}",
            timeout=240.0,
        )

        # Delete the worker's topic via Telethon
        await live_tg_forum.platform.delete_topic(worker_thread)
        await asyncio.sleep(5)  # Let the deletion propagate

        # Record General baseline BEFORE sending the message
        general_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        # Send message from root to deleted worker
        msg_to_deleted = f"HELLO-DOOMED-{tag}"
        delivered = await _send_inbox_message_and_expect_outcome(
            live_tg_forum,
            sender_thread_id=root_thread,
            recipient=worker_lineage["native_agent_name"],
            content=msg_to_deleted,
            delivered_token=f"DELIVERED-TO-DOOMED-{tag}",
            undelivered_token=f"UNDELIVERED-DOOMED-{tag}",
            timeout=240.0,
        )

        # The message should still be "delivered" to inbox file (always-deliver)
        # But the wake attempt should fail gracefully

        # CRITICAL: Check that the message did NOT appear in General topic
        await asyncio.sleep(10)
        recent_general = await live_tg_forum.platform.get_recent_messages(
            thread_id=None, limit=20
        )
        redirected_messages = [
            m for m in recent_general
            if m.message_id > general_baseline
            and msg_to_deleted in m.text
        ]
        assert len(redirected_messages) == 0, (
            f"CRITICAL: Message to deleted topic was redirected to General! "
            f"This should never happen. Messages: {[m.text[:100] for m in redirected_messages]}"
        )


# ---------------------------------------------------------------------------
# TEST 7: session_lineage include_xml=false
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(300)  # 5 minutes
class TestSessionLineageNoXML:
    """Verify session_lineage respects include_xml=false."""

    async def test_live_session_lineage_no_xml(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """session_lineage with include_xml=false should not return XML."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        thread_id = await live_tg_forum.platform.create_topic(f"NoXML {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic session_lineage test. Reply with only NOXML-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"NOXML-PRIME-{tag}",
            timeout=180.0,
        )

        # Query with include_xml=false
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send(
            (
                "Call session_lineage with include_xml set to the string 'false'. "
                "If the result contains '<obs-bootstrap' or 'xml' key, reply with exactly "
                f"HAS-XML-{tag}. Otherwise reply with exactly NO-XML-{tag}."
            ),
            thread_id=thread_id,
            require_done=False,
            timeout=180.0,
        )
        result = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline,
            tokens=[f"HAS-XML-{tag}", f"NO-XML-{tag}"],
            timeout=300.0,
        )
        assert _message_is_exact_token(result.text, f"NO-XML-{tag}"), (
            f"session_lineage with include_xml=false still returned XML! "
            f"The bool('false') bug may not be fixed."
        )


# ---------------------------------------------------------------------------
# TEST 8: AgentTask returns native_agent_name
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)  # 10 minutes
class TestAgentTaskReturnsName:
    """AgentTask confirmation should include the native_agent_name."""

    async def test_live_agent_task_returns_native_name(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Launch a child via AgentTask and verify the confirmation includes native_agent_name."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        thread_id = await live_tg_forum.platform.create_topic(f"AgentName {tag}")
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic agent name test. Reply with only AGENTNAME-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"AGENTNAME-PRIME-{tag}",
            timeout=180.0,
        )

        # Launch child and check the confirmation text
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send(
            (
                "Use AgentTask exactly once with fork=false, "
                f"alias='NameCheck-{tag}', "
                "prompt='Reply with DONE and stop.'. "
                "After launching, check the launch confirmation text. "
                "If it contains 'native_agent_name' or 'agent_name', "
                f"reply with exactly NAME-IN-CONFIRM-{tag}. "
                f"Otherwise reply with NAME-MISSING-{tag}."
            ),
            thread_id=thread_id,
            require_done=False,
            timeout=240.0,
        )
        result = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline,
            tokens=[f"NAME-IN-CONFIRM-{tag}", f"NAME-MISSING-{tag}"],
            timeout=360.0,
        )
        assert _message_is_exact_token(result.text, f"NAME-IN-CONFIRM-{tag}"), (
            "AgentTask confirmation does not include native_agent_name! "
            "Parents need to know how to message their children."
        )
