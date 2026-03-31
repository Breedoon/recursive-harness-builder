"""Staged integration audit of the Telegram team/messaging system.

This is intentionally broader than the deterministic live tests, but it is
still structured in explicit phases so the audit does not stall inside one
giant Sonnet turn.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from tests.evals.platform_telegram_forum import TelegramForumPlatform
from tests.live_test_vault import ensure_live_test_vault
from tests.test_telegram_live_forum_topics import (
    _extract_json_object,
    _has_telegram_credentials,
    _LiveForumHarness,
    _resolve_sender_tokens,
    _start_bot,
    _wait_for_message_after_containing,
    _warm_platform,
)


AUDIT_MODEL = "sonnet"
AUDIT_PHASE1_TIMEOUT_SECONDS = 12 * 60
AUDIT_PHASE2_TIMEOUT_SECONDS = 8 * 60
AUDIT_PHASE3_TIMEOUT_SECONDS = 10 * 60
AUDIT_MESSAGE_LIMIT = 120

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


def _phase1_prompt(tag: str) -> str:
    return f"""\
This is phase 1 of a messaging audit. Keep all outputs terse. Do not narrate tool calls.

1. Call `session_lineage` exactly once with `include_xml=true` and remember the result.
2. Build this exact real hierarchy sequentially, not in parallel:
   - you launch direct child `Alpha`
   - you launch direct child `Charlie`
   - `Alpha` launches child `Bravo`
   - `Charlie` launches child `Delta`
   - `Delta` launches child `Echo`
3. Use the exact display names `Alpha`, `Charlie`, `Bravo`, `Delta`, `Echo`.
4. In every child prompt require the child to:
   - call `session_lineage` exactly once
   - call `search_team` exactly once with `mode='parent'`
   - send exactly one inbox message upward to its parent with `needs_reply=false` and content `HELLO-<Name>-{tag}`
   - then reply with only `CHILD-READY-<Name>-{tag}|agent_name=<value>|parent_agent_name=<value>|root_team_key=<value>`
5. Do not let the root launch `Bravo`, `Delta`, or `Echo`.
6. When the full hierarchy exists, call `search_team` exactly once with `mode='tree'`.
7. Then reply with exactly `AUDIT-PHASE1-DONE-{tag}|` followed by only a JSON object with shape:
   `{{"root_agent":"...","tree":["..."],"count":6}}`

No extra text in that final message."""


def _phase2_prompt(tag: str) -> str:
    return f"""\
This is phase 2 of a messaging audit. Keep all outputs terse.

1. Call `search_team` exactly once with `mode='family'`.
2. Call `search_team` exactly once with `mode='tree'`.
3. Send one direct-child alias message to `Alpha` using `recipient="Alpha"`, `content="ROOT-ALIAS-{tag}"`, `needs_reply=false`.
4. Wake or prompt `Alpha` to confirm it received `ROOT-ALIAS-{tag}`.
5. Then resume or prompt the ORIGINAL `Alpha` task to do this exact sequence itself:
   - send one alias message to the ORIGINAL `Bravo` using `recipient="Bravo"` and `content="BRAVO-REQUEST-{tag}: reply to your discovered parent with content BRAVO-UP-{tag}"`, `needs_reply=false`
   - wait for Bravo's reply
   - read the upward inbox message from Bravo
   - record the exact `from` and `must_reply` values stored in that inbox entry
6. Do not create a replacement Bravo task for phase 2. The goal is to observe the original Bravo identity.
8. Then reply with exactly `AUDIT-PHASE2-DONE-{tag}|` followed by only a JSON object with shape:
   `{{"family_parent":<string-or-null>,"alias_root_to_alpha":"ok"|"fail","alias_alpha_to_bravo":"ok"|"fail","alpha_saw_from":"...","alpha_saw_must_reply":true|false}}`

No extra text in that final message."""


def _phase3_prompt(tag: str) -> str:
    return f"""\
This is phase 3 of a messaging audit. Keep all outputs terse.

1. Wait until `Alpha`, `Charlie`, `Bravo`, `Delta`, and `Echo` have all completed.
2. Use the exact full `agent_name` values from your phase 1 `tree` result for all five recipients. Do not use aliases in phase 3.
3. Send each of them exactly one inbox message with `needs_reply=true` asking for a one-line reply token:
   - `ASSESS-Alpha-{tag}`
   - `ASSESS-Charlie-{tag}`
   - `ASSESS-Bravo-{tag}`
   - `ASSESS-Delta-{tag}`
   - `ASSESS-Echo-{tag}`
4. Wait for replies from those completed agents.
5. If any existing descendant from the phase 1 tree reports `no current route binding` or otherwise cannot be reached by full `agent_name`, record that as a bug. Do not create a replacement task for that agent.
6. Then reply with exactly `AUDIT-FINAL-{tag}|` followed by only a JSON object with shape:
   `{{"completed_wake":{{"Alpha":true|false,"Charlie":true|false,"Bravo":true|false,"Delta":true|false,"Echo":true|false}},"missing":[...],"issues":[...]}}`

No extra text in that final message."""


def _extract_json_after_token(text: str, token: str) -> dict[str, object]:
    idx = text.find(token)
    if idx == -1:
        raise AssertionError(f"token {token!r} missing in:\n{text}")
    payload = text[idx + len(token):].strip()
    if payload.startswith("|"):
        payload = payload[1:].strip()
    try:
        return _extract_json_object(payload)
    except Exception as exc:  # pragma: no cover - test helper
        raise AssertionError(f"missing JSON after token {token!r} in:\n{text}") from exc


def _load_audit_topics(state_db_path: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            select thread_id, topic_title
            from route_state
            where thread_id is not null
            order by thread_id
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(thread_id), str(topic_title or f"Thread {thread_id}"))
        for thread_id, topic_title in rows
        if isinstance(thread_id, int)
    ]


@pytest_asyncio.fixture
async def audit_live_tg_forum(tmp_path: Path) -> _LiveForumHarness:
    """Dedicated live harness for the staged audit."""
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

    platform = TelegramForumPlatform(idle_quiescence_timeout=90.0)
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
        platform._chat_id = await platform.create_isolated_forum_chat()
        await _warm_platform(harness)
        yield harness
    finally:
        await platform.close()
        from tests.test_telegram_live_forum_topics import _stop_bot  # local import keeps test-only dependency scoped

        _stop_bot(proc)


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.timeout(2100)
class TestIntegrationAudit:
    """Broad but staged integration audit of the team/messaging system."""

    async def test_integration_audit(
        self,
        audit_live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        thread_id = await audit_live_tg_forum.platform.create_topic(f"Audit {tag}")

        phase1_token = f"AUDIT-PHASE1-DONE-{tag}|"
        phase2_token = f"AUDIT-PHASE2-DONE-{tag}|"
        final_token = f"AUDIT-FINAL-{tag}|"

        baseline_phase1 = await audit_live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await audit_live_tg_forum.platform.send(
            _phase1_prompt(tag),
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,
        )
        phase1_message = await _wait_for_message_after_containing(
            audit_live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline_phase1,
            token=phase1_token,
            timeout=AUDIT_PHASE1_TIMEOUT_SECONDS,
        )
        phase1_payload = _extract_json_after_token(phase1_message.text, phase1_token)
        assert phase1_payload.get("count") == 6, audit_live_tg_forum.failure_context()
        phase1_tree = phase1_payload.get("tree")
        assert isinstance(phase1_tree, list) and len(phase1_tree) == 6, audit_live_tg_forum.failure_context()
        assert len({str(item) for item in phase1_tree}) == 6, audit_live_tg_forum.failure_context()

        baseline_phase2 = await audit_live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await audit_live_tg_forum.platform.send(
            _phase2_prompt(tag),
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,
        )
        phase2_message = await _wait_for_message_after_containing(
            audit_live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline_phase2,
            token=phase2_token,
            timeout=AUDIT_PHASE2_TIMEOUT_SECONDS,
        )
        phase2_payload = _extract_json_after_token(phase2_message.text, phase2_token)
        assert phase2_payload.get("alias_root_to_alpha") == "ok", audit_live_tg_forum.failure_context()
        assert phase2_payload.get("alias_alpha_to_bravo") == "ok", audit_live_tg_forum.failure_context()
        assert phase2_payload.get("alpha_saw_must_reply") is False, audit_live_tg_forum.failure_context()

        baseline_phase3 = await audit_live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await audit_live_tg_forum.platform.send(
            _phase3_prompt(tag),
            thread_id=thread_id,
            require_done=False,
            timeout=30.0,
        )
        final_message = await _wait_for_message_after_containing(
            audit_live_tg_forum,
            thread_id=thread_id,
            after_message_id=baseline_phase3,
            token=final_token,
            timeout=AUDIT_PHASE3_TIMEOUT_SECONDS,
        )
        final_payload = _extract_json_after_token(final_message.text, final_token)
        completed_wake = final_payload.get("completed_wake")
        assert isinstance(completed_wake, dict), audit_live_tg_forum.failure_context()
        for agent_label in ("Alpha", "Charlie", "Bravo", "Delta", "Echo"):
            assert completed_wake.get(agent_label) is True, audit_live_tg_forum.failure_context()

        topic_rows = _load_audit_topics(audit_live_tg_forum.state_db_path)
        transcript_sections: list[str] = []
        aggregate_messages = []
        for topic_thread_id, topic_title in topic_rows:
            thread_messages = await audit_live_tg_forum.platform.get_recent_messages(
                thread_id=topic_thread_id,
                limit=AUDIT_MESSAGE_LIMIT,
            )
            aggregate_messages.extend(thread_messages)
            transcript_sections.append(f"[topic {topic_thread_id}] {topic_title}")
            if not thread_messages:
                transcript_sections.append("(no bot messages)")
            for message in thread_messages:
                transcript_sections.append(f"[msg {message.message_id}]")
                transcript_sections.append(message.text)
                transcript_sections.append("")

        full_text = "\n".join(transcript_sections)
        final_report = final_message.text
        full_lower = full_text.lower()

        broken_found = []
        for indicator in BROKEN_INDICATORS:
            if indicator in full_lower:
                idx = full_lower.index(indicator)
                context = full_text[max(0, idx - 100):idx + len(indicator) + 100]
                broken_found.append(f"  [{indicator}]: ...{context}...")

        successes = [ind for ind in SUCCESS_INDICATORS if ind in full_lower]

        report_path = Path("/tmp/obs-audit-report.txt")
        lines: list[str] = []

        def out(s: str = "") -> None:
            print(s)
            lines.append(s)

        out("\n" + "=" * 80)
        out("INTEGRATION AUDIT RESULTS")
        out("=" * 80)
        out(f"\nTopics inspected: {len(topic_rows)}")
        out(f"Total bot messages across topics: {len(aggregate_messages)}")
        out(f"Success indicators found: {successes}")

        if broken_found:
            out(f"\nBROKEN INDICATORS ({len(broken_found)}):")
            for item in broken_found:
                out(item)
        else:
            out("\nNo broken indicators found")

        out("\n" + "=" * 40)
        out("FINAL REPORT:")
        out("=" * 40)
        out(final_report if final_report else "(no final report found)")

        out("\n" + "=" * 40)
        out("FULL CONVERSATION (all topic messages):")
        out("=" * 40)
        out(full_text if full_text else "(no messages captured)")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        out(f"\nReport written to {report_path}")

        if any("no obs bootstrap found" in item.lower() for item in broken_found):
            pytest.fail(
                "CRITICAL: session_lineage/search_team returned 'no OBS bootstrap found'. "
                "The lineage bootstrap path is still broken."
            )
