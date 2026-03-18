"""Integration audit test — open-ended probe of team/messaging system.

NOT a deterministic pass/fail test. Sends a single open-ended prompt to a
fresh agent, lets it build a team, exercise messaging, and produce a report.
The test captures the report and flags broken functionality.

Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.test_telegram_live_forum_topics import (
    _reset_general,
    _LiveForumHarness,
    live_tg_forum,  # fixture import
)


AUDIT_PROMPT = """\
We recently shipped a multi-level team system with agent identities and messaging. I want you to stress-test it.

First, show me the raw bootstrap XML from your system prompt.

Then find your lineage and identity using whatever tools are available.

Launch 5 sub-agents at 3 different depths — you decide the structure. In their prompts, tell each one to: (1) check their own lineage/identity, (2) send a message to their parent, (3) report whether they received a system notification about unread messages when they were woken up.

Once they've all had time to work, wait 30 seconds without doing anything, then check your inbox. Note whether you received a system notification about new teammate messages (it would appear as a system message like "New teammate messages arrived"). Report: "Did I receive a system wake notification? Yes/No."

Also try sending a message to one of your children using just their alias (e.g. just "Alpha" instead of the full hash-prefix name). Report whether alias-based messaging worked.

After that, wait for ALL your agents to complete their tasks. Once they've completed, send each of them a message asking for their honest assessment and asking them to reply. This is critical — completed agents MUST be woken up and respond. If a completed agent doesn't respond to your message, that's a bug.

Then give me a final report. Specifically note:
(a) Whether you received system notifications about unread messages while actively running (not just after completing a turn)
(b) Whether your agents reported receiving system notifications when messages arrived in their inbox
(c) Whether COMPLETED agents were successfully woken up and responded to your assessment request
(d) Whether alias-based messaging worked (sending to just the alias vs the full hash-prefix name)
(e) Any tools that returned errors
(f) Which messages were delivered and which weren't
(g) Any surprising or broken behavior\
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
@pytest.mark.timeout(1200)  # 20 minutes max (audit needs 15, buffer for setup)
class TestIntegrationAudit:
    """Open-ended integration audit of the team/messaging system."""

    async def test_integration_audit(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Send an open-ended probe prompt, collect the final report."""
        tag = uuid.uuid4().hex[:8]

        # Create a fresh topic for the audit (no /clear — preserves identity)
        thread_id = await live_tg_forum.platform.create_topic(f"Audit {tag}")

        # Send the audit prompt — don't require "done" marker,
        # just let the agent work
        await live_tg_forum.platform.send(
            AUDIT_PROMPT,
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,  # just send, don't wait for full completion
        )

        # Wait 10 minutes — no early exit, let the agent work fully
        await asyncio.sleep(600)

        # Collect ALL bot messages from the topic
        all_messages = await live_tg_forum.platform.get_recent_messages(
            thread_id=thread_id,
            limit=60,
        )

        # Build the full conversation text
        full_text = "\n\n---\n\n".join(m.text for m in all_messages if m.text.strip())

        # Find the final report — last 3 substantial messages (report may be split)
        final_parts = []
        for m in reversed(all_messages):
            if len(m.text) > 100:
                final_parts.append(m.text)
                if len(final_parts) >= 3:
                    break
        final_parts.reverse()
        final_report = "\n\n---\n\n".join(final_parts)

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

        # --- OUTPUT (to file and stdout) ---
        report_path = Path("/tmp/obs-audit-report.txt")
        lines: list[str] = []
        def out(s: str = "") -> None:
            print(s)
            lines.append(s)

        out("\n" + "=" * 80)
        out("INTEGRATION AUDIT RESULTS")
        out("=" * 80)

        out(f"\nTotal bot messages: {len(all_messages)}")
        out(f"Success indicators found: {successes}")

        if broken_found:
            out(f"\n⚠️  BROKEN INDICATORS ({len(broken_found)}):")
            for b in broken_found:
                out(b)
        else:
            out("\n✅ No broken indicators found")

        out(f"\n{"=" * 40}")
        out("FINAL REPORT (last 3 substantial messages):")
        out(f"{'=' * 40}")
        out(final_report if final_report else "(no final report found)")

        out(f"\n{"=" * 40}")
        out("FULL CONVERSATION (all messages):")
        out(f"{'=' * 40}")
        for i, m in enumerate(all_messages):
            if m.text.strip():
                out(f"\n[msg {i+1}, id={m.message_id}]")
                out(m.text)
                out("---")

        # Write report to file for background runs
        report_path.write_text("\n".join(lines), encoding="utf-8")
        out(f"\n📄 Report written to {report_path}")

        # The test always "passes" — it's an audit, not a pass/fail.
        # The output is what matters. But flag if there are critical errors.
        if any("no obs bootstrap found" in b.lower() for b in broken_found):
            pytest.fail(
                "CRITICAL: session_lineage/get_family returned 'no OBS bootstrap found'. "
                "The lineage regex is still broken."
            )
