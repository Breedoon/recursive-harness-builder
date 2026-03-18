"""Integration audit test — open-ended probe of team/messaging system.

NOT a deterministic pass/fail test. Sends a single open-ended prompt to a
fresh agent, lets it build a team, exercise messaging, and produce a report.
The test captures the report and flags broken functionality.

Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.test_telegram_live_forum_topics import (
    _reset_general,
    _LiveForumHarness,
    live_tg_forum,  # fixture import
)


AUDIT_PROMPT = """\
Show me the raw bootstrap XML from your system prompt first.

Then find your lineage and identity — figure out which tools are available to you for this.

Launch 5 sub-agents at 3 different depths — you decide the structure. Have each one check their own lineage and identity too.

Once they're all running, have them message each other — cross-branch, different depths, leaf to root, whatever coverage you can get.

After everyone has communicated, send each of them a message asking for their honest assessment of what worked and what didn't. Ask them to reply.

Then give me a final report: which tools worked, which didn't, which messages were delivered, which agents got woken up properly, and any errors or surprising behavior.\
"""

# Keywords that indicate broken functionality (not just preferences)
BROKEN_INDICATORS = [
    "recipient not found",
    "no inbox exists",
    "no obs bootstrap found",
    "stream closed",
    "error in hook callback",
    "message undelivered",
    "tool_use_error",
    "not a live agent",
    "no task found",
]

# Keywords that indicate successful functionality
SUCCESS_INDICATORS = [
    "message delivered",
    "successfully",
    "received",
    "lineage",
    "agent_name",
    "team_key",
]


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(900)  # 15 minutes max
class TestIntegrationAudit:
    """Open-ended integration audit of the team/messaging system."""

    async def test_integration_audit(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Send an open-ended probe prompt, collect the final report."""
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        # Create a fresh topic for the audit
        thread_id = await live_tg_forum.platform.create_topic(f"Audit {tag}")

        # Send the audit prompt — don't require "done" marker,
        # just let the agent work
        await live_tg_forum.platform.send(
            AUDIT_PROMPT,
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,  # just send, don't wait for full completion
        )

        # Wait for the agent to work — check periodically for the
        # "final report" or timeout after 10 minutes
        deadline = asyncio.get_running_loop().time() + 600  # 10 min
        last_message_id = 0
        stable_count = 0
        final_messages: list[str] = []

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(30)  # check every 30s

            recent = await live_tg_forum.platform.get_recent_messages(
                thread_id=thread_id,
                limit=60,
            )

            if not recent:
                continue

            current_last = recent[-1].message_id
            if current_last == last_message_id:
                stable_count += 1
                # If no new messages for 90s (3 checks), agent is probably done
                if stable_count >= 3:
                    break
            else:
                stable_count = 0
                last_message_id = current_last

        # Collect ALL bot messages from the topic
        all_messages = await live_tg_forum.platform.get_recent_messages(
            thread_id=thread_id,
            limit=60,
        )

        # Build the full conversation text
        full_text = "\n\n---\n\n".join(m.text for m in all_messages if m.text.strip())

        # Find the final report (last substantial message)
        final_report = ""
        for m in reversed(all_messages):
            if len(m.text) > 200:  # substantial message, not just a short ack
                final_report = m.text
                break

        # --- ANALYSIS ---
        full_lower = full_text.lower()

        # Check for broken indicators
        broken_found = []
        for indicator in BROKEN_INDICATORS:
            if indicator in full_lower:
                # Find the context around the indicator
                idx = full_lower.index(indicator)
                context = full_text[max(0, idx - 100):idx + len(indicator) + 100]
                broken_found.append(f"  [{indicator}]: ...{context}...")

        # Check for success indicators
        successes = [ind for ind in SUCCESS_INDICATORS if ind in full_lower]

        # --- OUTPUT ---
        print("\n" + "=" * 80)
        print("INTEGRATION AUDIT RESULTS")
        print("=" * 80)

        print(f"\nTotal bot messages: {len(all_messages)}")
        print(f"Success indicators found: {successes}")

        if broken_found:
            print(f"\n⚠️  BROKEN INDICATORS ({len(broken_found)}):")
            for b in broken_found:
                print(b)
        else:
            print("\n✅ No broken indicators found")

        print(f"\n{'=' * 40}")
        print("FINAL REPORT FROM AGENT:")
        print(f"{'=' * 40}")
        print(final_report[:3000] if final_report else "(no final report found)")

        print(f"\n{'=' * 40}")
        print("FULL CONVERSATION:")
        print(f"{'=' * 40}")
        print(full_text[:8000])

        # The test always "passes" — it's an audit, not a pass/fail.
        # The output is what matters. But flag if there are critical errors.
        if any("no obs bootstrap found" in b.lower() for b in broken_found):
            pytest.fail(
                "CRITICAL: session_lineage/get_family returned 'no OBS bootstrap found'. "
                "The lineage regex is still broken."
            )
