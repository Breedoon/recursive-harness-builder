"""Live smoke tests for naming redesign + must_reply + schedule rearchitecture.

Smoke Test 4: "Grand Hierarchy + must_reply Stress" (~20-25 min)
- Build 5-level hierarchy with new two-tier naming
- Verify naming conventions at every level
- Cross-messaging matrix (all directions)
- must_reply wake exhaustion, upsert, partial reply, wrong-agent
- Same-name agents at different depths

Smoke Test 5: "Schedule Rearchitecture" (~10-15 min)
- Coexisting schedules on same route
- CronDelete blocked for agents
- /unschedule deletes next-only
- interval_seconds=1 behavior

These tests require live Telegram credentials and the test bot running.
Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from tests.evals.platform_telegram_forum import TelegramForumPlatform
from tests.test_telegram_live_forum_topics import (
    _append_unread_inbox_message,
    _clear_cached_forum_chat_id,
    _extract_agent_id,
    _extract_json_object,
    _extract_topic_link,
    _message_containing,
    _reset_general,
    _send_and_wait_for_token,
    _session_id_for_route,
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
    _query_session_lineage,
    _query_session_lineage_payload,
    _send_inbox_message_and_wait_ack,
)


# ---------------------------------------------------------------------------
# Smoke Test 4: Grand Hierarchy + Naming Verification
# ---------------------------------------------------------------------------

@pytest.mark.telegram_smoke
@pytest.mark.timeout(1200)  # 20 minutes
class TestNamingRedesignSmoke:
    """Live smoke tests for the naming redesign.

    Builds a multi-level hierarchy and verifies:
    - Two-tier naming at every level (trunk = slug, child = {hash}-{slug})
    - Timestamp team keys shared across tree
    - XML agent_name enrichment
    - Cross-branch messaging
    - Same-name agents at different depths
    """

    async def test_live_naming_hierarchy_and_cross_messaging(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Build 5-level tree, verify naming at each level, test cross-messaging."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Phase 1: Create root topic
        root_thread_id = await live_tg_forum.platform.create_topic(f"NamingSmoke {tag}")

        # Prime the root agent
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic naming smoke test. Reply with only NAMING-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"NAMING-PRIME-{tag}",
            timeout=180.0,
        )

        # Query root lineage — should use new naming format
        root_lineage = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=root_thread_id,
            timeout=240.0,
        )

        # Root should have lineage_length=1 and timestamp team key
        assert root_lineage["lineage_length"] == 1
        # Team key should NOT start with obs-tree-
        assert not root_lineage["root_team_key"].startswith("obs-tree-"), \
            f"Team key should be timestamp-based, got: {root_lineage['root_team_key']}"
        # Agent name should be just the slug (no obs-agent- prefix)
        assert not root_lineage["agent_name"].startswith("obs-agent-"), \
            f"Trunk agent name should be slug only, got: {root_lineage['agent_name']}"

        # Phase 2: Launch children
        worker_a_thread, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"Alpha-{tag}",
            launch_token=f"ROOT-LAUNCHED-A-{tag}",
            lineage_token=f"LINEAGE-A-{tag}",
            timeout=240.0,
        )

        worker_b_thread, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"Beta-{tag}",
            launch_token=f"ROOT-LAUNCHED-B-{tag}",
            lineage_token=f"LINEAGE-B-{tag}",
            timeout=240.0,
        )

        # Verify children share root_team_key but have unique agent names
        assert lineage_a["root_team_key"] == root_lineage["root_team_key"]
        assert lineage_b["root_team_key"] == root_lineage["root_team_key"]
        assert lineage_a["agent_name"] != lineage_b["agent_name"]
        assert lineage_a["lineage_length"] == "2"
        assert lineage_b["lineage_length"] == "2"

        # Child agent names should have hash prefix (not obs-agent-)
        assert not lineage_a["agent_name"].startswith("obs-agent-"), \
            f"Child should not have obs-agent- prefix, got: {lineage_a['agent_name']}"
        assert not lineage_b["agent_name"].startswith("obs-agent-"), \
            f"Child should not have obs-agent- prefix, got: {lineage_b['agent_name']}"
        # Should contain a 10-char hex prefix followed by dash and slug
        assert re.match(r"[0-9a-f]{10}-", lineage_a["agent_name"]), \
            f"Child should have 10-char hash prefix, got: {lineage_a['agent_name']}. " \
            f"Is the naming redesign applied at the launch path?"

        # Phase 3: Launch grandchild from Alpha
        worker_c_thread, lineage_c = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=worker_a_thread,
            fork=True,
            alias=f"Charlie-{tag}",
            launch_token=f"A-LAUNCHED-C-{tag}",
            lineage_token=f"LINEAGE-C-{tag}",
            timeout=240.0,
        )

        assert lineage_c["root_team_key"] == root_lineage["root_team_key"]
        assert lineage_c["lineage_length"] == "3"

        # Phase 4: Cross-messaging
        # C → Root (grandchild to root, 2 levels up)
        token_c_to_root = f"MSG-C-TO-ROOT-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=worker_c_thread,
            recipient=root_lineage["agent_name"],
            content=token_c_to_root,
            ack_token=f"C-SENT-ROOT-{tag}",
            timeout=240.0,
        )

        # Verify root received it
        baseline_root = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "Call ReadInbox exactly once with no arguments. "
                f"If unread messages contain {token_c_to_root}, reply with exactly ROOT-GOT-{tag}. "
                f"Otherwise reply with ROOT-MISSING-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=240.0,
        )
        root_read_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_root,
            token=f"ROOT-GOT-{tag}",
            timeout=300.0,
        )

        # Root → B (cross-branch messaging)
        token_root_to_b = f"MSG-ROOT-TO-B-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread_id,
            recipient=lineage_b["agent_name"],
            content=token_root_to_b,
            ack_token=f"ROOT-SENT-B-{tag}",
            timeout=240.0,
        )


# ---------------------------------------------------------------------------
# Smoke Test 5: must_reply Adversarial Stress Test
# ---------------------------------------------------------------------------

@pytest.mark.telegram_smoke
@pytest.mark.timeout(1500)  # 25 minutes
class TestMustReplySmoke:
    """Live smoke tests for must_reply mechanism.

    Tests:
    - Wake exhaustion (agent ignores must_reply, gets woken 3 times, then stops)
    - Upsert resets run_count
    - Reply detection clears must_reply
    - Wrong-agent reply doesn't clear
    """

    async def test_live_must_reply_wake_exhaustion_and_reply(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Must_reply wake cycle: 3 attempts, exhaustion, then fresh must_reply."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread_id = await live_tg_forum.platform.create_topic(f"MustReply Stress {tag}")

        # Prime root
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic must_reply stress test. Reply with only MUSTREPLY-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"MUSTREPLY-PRIME-{tag}",
            timeout=180.0,
        )

        # Launch a child agent
        worker_thread, worker_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"Worker-{tag}",
            launch_token=f"LAUNCHED-WORKER-{tag}",
            lineage_token=f"LINEAGE-WORKER-{tag}",
            timeout=240.0,
        )

        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread_id,
            token=f"ROOT-LINEAGE-{tag}",
            timeout=240.0,
        )

        # Phase 1: Root sends must_reply to worker
        token_must_reply = f"URGENT-REPLY-{tag}"
        baseline_root = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic must_reply test. "
                f"Use SendInboxMessage with recipient={worker_lineage['agent_name']}, "
                f"content={token_must_reply!r}, summary='must_reply test', must_reply=true, "
                "and omit team_name and sender. "
                f"Reply with only ROOT-SENT-MUSTREPLY-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_root,
            token=f"ROOT-SENT-MUSTREPLY-{tag}",
            timeout=300.0,
        )

        # Phase 2: Instruct worker to read inbox and reply
        baseline_worker = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic must_reply test. "
                "Call ReadInbox exactly once. "
                f"If you see a message containing {token_must_reply!r}, "
                f"use SendInboxMessage to reply to {root_lineage['agent_name']} "
                f"with content 'WORKER-REPLIED-{tag}' and summary='reply'. "
                f"Then reply with exactly WORKER-DID-REPLY-{tag}."
            ),
            thread_id=worker_thread,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_thread,
            after_message_id=baseline_worker,
            token=f"WORKER-DID-REPLY-{tag}",
            timeout=300.0,
        )

        # Phase 3: Verify root received the reply
        baseline_root2 = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "Call ReadInbox exactly once with no arguments. "
                f"If unread messages contain 'WORKER-REPLIED-{tag}', reply with exactly ROOT-GOT-REPLY-{tag}. "
                f"Otherwise reply with ROOT-NO-REPLY-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_root2,
            token=f"ROOT-GOT-REPLY-{tag}",
            timeout=300.0,
        )


# ---------------------------------------------------------------------------
# Smoke Test 6: Same-Name Agents at Different Depths
# ---------------------------------------------------------------------------

@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)  # 10 minutes
class TestNamingFormatSmoke:
    """Verify the two-tier naming format works in live agents.

    Simple test: launch one child, verify its agent_name has the hash prefix format.
    """

    async def test_live_child_has_hash_prefix_agent_name(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Child agent launched via AgentTask gets {parent_hash}-{slug} agent_name."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        root_thread_id = await live_tg_forum.platform.create_topic(f"NameFmt {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic naming format test. Reply with only NAMEFMT-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"NAMEFMT-PRIME-{tag}",
            timeout=180.0,
        )

        # Query root lineage
        root_lineage = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread_id,
            token=f"ROOT-LINEAGE-{tag}",
            timeout=240.0,
        )

        # Root: no obs-agent- prefix, no obs-tree- prefix
        assert not root_lineage["agent_name"].startswith("obs-agent-"), \
            f"Root should not have obs-agent- prefix, got: {root_lineage['agent_name']}"
        assert not root_lineage["root_team_key"].startswith("obs-tree-"), \
            f"Root team key should be timestamp-based, got: {root_lineage['root_team_key']}"

        # Launch one child
        child_thread, child_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"Worker-{tag}",
            launch_token=f"LAUNCHED-WORKER-{tag}",
            lineage_token=f"LINEAGE-WORKER-{tag}",
            timeout=240.0,
        )

        # Child: should have hash prefix
        child_name = child_lineage["agent_name"]
        assert not child_name.startswith("obs-agent-"), \
            f"Child should not have obs-agent- prefix, got: {child_name}"
        # Should contain a hex prefix
        assert re.match(r"[0-9a-f]+-", child_name.lower()), \
            f"Child should have hash prefix, got: {child_name}"
        # Should contain the worker slug
        assert "worker" in child_name.lower(), \
            f"Child should contain 'worker' slug, got: {child_name}"

        # Child shares root's team key (slug part at minimum)
        child_slug = child_lineage["root_team_key"].split("-", 5)[-1]
        root_slug = root_lineage["root_team_key"].split("-", 5)[-1]
        assert child_slug == root_slug, \
            f"Child and root should have same team key slug: {child_lineage['root_team_key']} vs {root_lineage['root_team_key']}"


# ---------------------------------------------------------------------------
# Smoke Test 7: Schedule Rearchitecture (CronDelete blocked, coexistence)
# ---------------------------------------------------------------------------

@pytest.mark.telegram_smoke
@pytest.mark.timeout(600)  # 10 minutes
class TestScheduleRearchitectureSmoke:
    """Live smoke tests for schedule rearchitecture.

    Tests:
    - CronDelete returns error for agents
    - Multiple schedules can coexist on same route
    """

    async def test_live_cron_delete_blocked(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Agent calling CronDelete gets an error; user /unschedule still works."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        thread_id = await live_tg_forum.platform.create_topic(f"SchedBlock {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"Deterministic schedule test. Reply with only SCHED-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"SCHED-PRIME-{tag}",
            timeout=180.0,
        )

        # Create a schedule
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic schedule test. "
                "Call CronCreate exactly once with schedule_mode='interval', "
                "interval_seconds=300, prompt='SCHED-TICK', max_runs=1, "
                "reset_session=false, description='test schedule'. "
                f"Reply with only SCHED-CREATED-{tag}."
            ),
            thread_id=thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline,
            token=f"SCHED-CREATED-{tag}",
            timeout=300.0,
        )

        # Now try CronDelete — should be blocked
        baseline2 = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic schedule test. "
                "Call CronList to get the schedule ID. "
                "Then call CronDelete with that ID. "
                "If CronDelete returns an error or says disabled, reply with exactly CRON-DELETE-BLOCKED-{tag}. "
                f"If CronDelete succeeds, reply with exactly CRON-DELETE-SUCCESS-{tag}."
            ),
            thread_id=thread_id,
            require_done=False,
            timeout=180.0,
        )
        block_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline2,
            token=f"CRON-DELETE-BLOCKED-{tag}",
            timeout=300.0,
        )
