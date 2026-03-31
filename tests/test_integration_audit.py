"""Integration audit test — open-ended probe of team/messaging system.

NOT a deterministic pass/fail test. Sends a single open-ended prompt to a
fresh agent, lets it build a team, exercise messaging, and produce a report.
The test captures the report and flags broken functionality.

Mark: @pytest.mark.telegram_smoke
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from tests.evals.platform_telegram_forum import TelegramForumPlatform
from tests.live_test_vault import ensure_live_test_vault
from tests.test_telegram_live_forum_topics import (
    _ensure_cached_forum_chat_id,
    _has_telegram_credentials,
    _LiveForumHarness,
    _resolve_sender_tokens,
    _start_bot,
    _warm_platform,
)


AUDIT_MODEL = "sonnet"
AUDIT_COLLECTION_SECONDS = 30 * 60
AUDIT_MESSAGE_LIMIT = 120

AUDIT_PROMPT = """\
We recently shipped a multi-level team system with agent identities and messaging. I want you to stress-test it.

First, show me the raw bootstrap XML from your system prompt.

Then find your lineage and identity using whatever tools are available.

Launch exactly 5 sub-agents in this exact nested structure so the hierarchy is real rather than flat:

1. You launch direct child `Alpha`.
2. You launch direct child `Charlie`.
3. `Alpha` launches child `Bravo`.
4. `Charlie` launches child `Delta`.
5. `Delta` launches child `Echo`.

That gives at least 3 different depths. Do not let the root agent launch all 5.

In every child prompt, require the child to:
(1) inspect its own lineage/identity
(2) use team discovery to find its parent if available
(3) send a message to its parent
(4) report whether it received a system notification about unread teammate messages while active or when woken
(5) report the exact agent name it used for the parent

Also require:

1. `Alpha` must try sending to direct child `Bravo` by alias only first, then by full agent name if alias fails.
2. `Bravo` must use its discovered parent to reply upward.
3. `Echo` must message `Delta`, and `Delta` must message `Charlie`.
4. At least one message should be sent while the recipient is still actively running.

Once they've all had time to work, wait 30 seconds without doing anything, then check your inbox. Note whether you received a system notification about new teammate messages (it would appear as a system message like "New teammate messages arrived"). Report: "Did I receive a system wake notification? Yes/No."

Also use the teammate discovery tool yourself to inspect both `family` and `tree` views, and report whether they matched the actual hierarchy.

Also try sending a message to one of your direct children using just their alias (e.g. just "Alpha" instead of the full hash-prefix name). Report whether alias-based messaging worked. Do not treat alias messaging to parents or siblings as expected to work.

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


@pytest_asyncio.fixture
async def audit_live_tg_forum(tmp_path: Path) -> _LiveForumHarness:
    """Dedicated live harness for the open-ended audit.

    The audit is deliberately broader and more synthesis-heavy than the
    deterministic live tests, so keep it on Sonnet by default while the rest of
    the test profile remains Haiku.
    """
    if not _has_telegram_credentials():
        pytest.skip("Telegram forum credentials not configured in environment")

    os.environ["OBS_TELEGRAM_BOT_TOKENS"] = ",".join(_resolve_sender_tokens())
    vault_path = ensure_live_test_vault()
    temp_root = tmp_path / "obs-agent-temp"
    state_db_path = tmp_path / "telegram-state.sqlite3"

    previous_model = os.environ.get("OBS_AGENT_MODEL")
    os.environ["OBS_AGENT_MODEL"] = AUDIT_MODEL
    try:
        proc, log_file = _start_bot(vault_path, temp_root, state_db_path=state_db_path)
    finally:
        if previous_model is None:
            os.environ.pop("OBS_AGENT_MODEL", None)
        else:
            os.environ["OBS_AGENT_MODEL"] = previous_model

    shared_chat_id = await _ensure_cached_forum_chat_id()
    platform = TelegramForumPlatform(chat_id=shared_chat_id, idle_quiescence_timeout=90.0)
    harness = _LiveForumHarness(
        platform=platform,
        proc=proc,
        log_file=log_file,
        vault_path=vault_path,
        bot_username=os.environ["OBS_TEST_TELEGRAM_BOT_USERNAME"],
        temp_root=temp_root,
        state_db_path=state_db_path,
    )
    await platform.connect()
    try:
        await _warm_platform(harness)
        yield harness
    finally:
        await platform.close()
        from tests.test_telegram_live_forum_topics import _stop_bot  # local import keeps test-only dependency scoped

        _stop_bot(proc)


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(2100)  # 30-minute collection window plus setup/teardown buffer
class TestIntegrationAudit:
    """Open-ended integration audit of the team/messaging system."""

    async def test_integration_audit(
        self,
        audit_live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Send an open-ended probe prompt, collect the final report."""
        tag = uuid.uuid4().hex[:8]

        # Create a fresh topic for the audit (no /clear — preserves identity)
        thread_id = await audit_live_tg_forum.platform.create_topic(f"Audit {tag}")

        # Send the audit prompt — don't require "done" marker,
        # just let the agent work
        await audit_live_tg_forum.platform.send(
            AUDIT_PROMPT,
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,  # just send, don't wait for full completion
        )

        # Wait 30 minutes — no early exit, let the agent work fully.
        await asyncio.sleep(AUDIT_COLLECTION_SECONDS)

        # Collect ALL bot messages from the topic
        all_messages = await audit_live_tg_forum.platform.get_recent_messages(
            thread_id=thread_id,
            limit=AUDIT_MESSAGE_LIMIT,
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

        out("\n" + "=" * 40)
        out("FINAL REPORT (last 3 substantial messages):")
        out("=" * 40)
        out(final_report if final_report else "(no final report found)")

        out("\n" + "=" * 40)
        out("FULL CONVERSATION (all messages):")
        out("=" * 40)
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
                "CRITICAL: session_lineage/search_team returned 'no OBS bootstrap found'. "
                "The lineage regex is still broken."
            )
