"""Dense live Telegram smoke scenarios for fast dev-loop confidence.

These tests intentionally pack multiple behaviors into 2 long scenarios so day-to-day
validation is substantially faster than running the full granular suite.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from obs_agent.lineage import agent_name_for_lineage
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


_LINEAGE_FACT_RE = re.compile(
    r"root_team_key=(?P<root_team_key>[^|\n]+)\|"
    r"agent_name=(?P<agent_name>[^|\n]+)\|"
    r"lineage_length=(?P<lineage_length>\d+)"
)

_LINEAGE_PAYLOAD_FACT_RE = re.compile(
    r"root_team_key=(?P<root_team_key>[^|\n]+)\|"
    r"agent_name=(?P<agent_name>[^|\n]+)\|"
    r"lineage_length=(?P<lineage_length>\d+)\|"
    r"origin=(?P<origin>[^|\n]+)\|"
    r"session_id=(?P<session_id>[^|\n]*)\|"
    r"lineage=(?P<lineage>[^\n]+)"
)

_FALLBACK_ROOT_TEAM_RE = re.compile(r"root_team_key\s*[:=]\s*[\"']?(?P<value>[^\"'\n<][^\"'\n]*)", re.IGNORECASE)
_FALLBACK_AGENT_NAME_RE = re.compile(
    r"agent_name(?:\s*\([^)]*\))?\s*[:=]\s*[\"']?(?P<value>[^\"'\n<][^\"'\n]*)",
    re.IGNORECASE,
)
_FALLBACK_LINEAGE_LENGTH_RE = re.compile(r"lineage_length\s*[:=]\s*(?P<value>\d+)", re.IGNORECASE)
_FALLBACK_ORIGIN_RE = re.compile(r"origin\s*[:=]\s*[\"']?(?P<value>[^\"'\n<][^\"'\n]*)", re.IGNORECASE)
_FALLBACK_SESSION_ID_RE = re.compile(r"session_id\s*[:=]\s*[\"']?(?P<value>[^\"'\n<][^\"'\n]*)", re.IGNORECASE)
_FALLBACK_LINEAGE_RE = re.compile(r"lineage\s*[:=]\s*[\"']?(?P<value>[^\"'\n<][^\"'\n]*)", re.IGNORECASE)


def _first_named_value(pattern: re.Pattern[str], text: str) -> str | None:
    for match in pattern.finditer(text):
        value = match.group("value").strip().strip(",")
        if value and "<value>" not in value.lower():
            return value
    return None


def _extract_lineage_fact_line(text: str) -> dict[str, str]:
    match = _LINEAGE_FACT_RE.search(text)
    if match:
        return {
            "root_team_key": match.group("root_team_key").strip(),
            "agent_name": match.group("agent_name").strip(),
            "lineage_length": match.group("lineage_length").strip(),
        }
    root_team_key = _first_named_value(_FALLBACK_ROOT_TEAM_RE, text)
    agent_name = _first_named_value(_FALLBACK_AGENT_NAME_RE, text)
    lineage_length = _first_named_value(_FALLBACK_LINEAGE_LENGTH_RE, text)
    assert root_team_key and agent_name and lineage_length, f"missing lineage fact line in:\n{text}"
    return {
        "root_team_key": root_team_key,
        "agent_name": agent_name,
        "lineage_length": lineage_length,
    }


def _message_is_exact_token(message_text: str, token: str) -> bool:
    normalized = message_text.strip().replace("_", "").replace("*", "").strip()
    return normalized == token


def _extract_lineage_payload_fact_line(text: str) -> dict[str, object]:
    match = _LINEAGE_PAYLOAD_FACT_RE.search(text)
    if match:
        lineage = [segment.strip() for segment in match.group("lineage").split(" >>> ") if segment.strip()]
        return {
            "root_team_key": match.group("root_team_key").strip(),
            "agent_name": match.group("agent_name").strip(),
            "lineage_length": int(match.group("lineage_length")),
            "origin": match.group("origin").strip(),
            "session_id": match.group("session_id").strip(),
            "lineage": lineage,
        }
    root_team_key = _first_named_value(_FALLBACK_ROOT_TEAM_RE, text)
    agent_name = _first_named_value(_FALLBACK_AGENT_NAME_RE, text)
    lineage_length = _first_named_value(_FALLBACK_LINEAGE_LENGTH_RE, text)
    origin = _first_named_value(_FALLBACK_ORIGIN_RE, text)
    session_id = _first_named_value(_FALLBACK_SESSION_ID_RE, text) or ""
    raw_lineage = _first_named_value(_FALLBACK_LINEAGE_RE, text) or ""
    lineage = [segment.strip() for segment in raw_lineage.split(" >>> ") if segment.strip()]
    assert (
        root_team_key and agent_name and lineage_length and origin
    ), f"missing lineage payload fact line in:\n{text}"
    return {
        "root_team_key": root_team_key,
        "agent_name": agent_name,
        "lineage_length": int(lineage_length),
        "origin": origin,
        "session_id": session_id,
        "lineage": lineage,
    }


async def _query_session_lineage_payload(
    harness: _LiveForumHarness,
    *,
    thread_id: int,
    timeout: float = 240.0,
) -> dict[str, object]:
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    token = f"LINEAGE-PAYLOAD-{uuid.uuid4().hex[:8]}"
    await harness.platform.send(
        (
            "This is a deterministic lineage smoke test. "
            "Call session_lineage exactly once. "
            f"Then reply with exactly {token}|root_team_key=<value>|agent_name=<value>|"
            "lineage_length=<value>|origin=<value>|session_id=<value>|lineage=<value>. "
            "Use the literal values returned by the tool. "
            "For lineage, join the lineage items with exactly ' >>> ' and nothing else. "
            "Do not explain, do not repeat placeholders like <value>, and do not add any surrounding text."
        ),
        thread_id=thread_id,
        require_done=False,
        timeout=timeout,
    )
    deadline = asyncio.get_running_loop().time() + timeout + 120.0
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=40)
        for message in recent:
            if message.message_id <= baseline or f"{token}|" not in message.text:
                continue
            try:
                return _extract_lineage_payload_fact_line(message.text)
            except AssertionError:
                continue
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for parseable session_lineage payload after message {baseline}\n"
                + harness.failure_context()
            )
        await asyncio.sleep(1.0)


async def _query_search_team_payload(
    harness: _LiveForumHarness,
    *,
    thread_id: int,
    mode: str,
    timeout: float = 240.0,
) -> dict[str, object]:
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    token = f"SEARCHTEAM-{uuid.uuid4().hex[:8]}"
    await harness.platform.send(
        (
            "This is a deterministic team-discovery smoke test. "
            f"Call search_team exactly once with mode={mode!r}. "
            f"Then reply with exactly {token}| followed by only a JSON object. "
            "For mode='family', use the exact shape "
            "{\"mode\":\"family\",\"parent\":<string-or-null>,\"children\":[<sorted names>],\"siblings\":[<sorted names>]}. "
            "For mode='tree', use the exact shape "
            "{\"mode\":\"tree\",\"tree\":[<sorted names>]}. "
            "Copy the literal teammate names returned by the tool and sort arrays lexicographically."
        ),
        thread_id=thread_id,
        require_done=False,
        timeout=timeout,
    )
    deadline = asyncio.get_running_loop().time() + timeout + 120.0
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=40)
        for message in recent:
            if message.message_id <= baseline or f"{token}|" not in message.text:
                continue
            try:
                payload = _extract_json_object(message.text)
            except Exception:
                continue
            if payload.get("mode") == mode:
                return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for parsable search_team payload mode={mode} after message {baseline}\n"
                + harness.failure_context()
            )
        await asyncio.sleep(1.0)


async def _query_session_lineage(
    harness: _LiveForumHarness,
    *,
    thread_id: int,
    token: str,
    timeout: float = 240.0,
) -> dict[str, str]:
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    await harness.platform.send(
        (
            "This is a deterministic lineage smoke test. "
            "Call session_lineage exactly once. "
            f"Then reply with exactly {token}|root_team_key=<value>|agent_name=<value>|lineage_length=<value> "
            "using the literal values returned by the tool. "
            "Replace the placeholders with the real values. Do not explain or add any other text."
        ),
        thread_id=thread_id,
        require_done=False,
        timeout=timeout,
    )
    deadline = asyncio.get_running_loop().time() + timeout + 120.0
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=40)
        for message in recent:
            if message.message_id <= baseline or f"{token}|" not in message.text:
                continue
            try:
                return _extract_lineage_fact_line(message.text)
            except AssertionError:
                continue
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for parseable session_lineage reply token={token} after message {baseline}\n"
                + harness.failure_context()
            )
        await asyncio.sleep(1.0)


async def _launch_lineage_worker(
    harness: _LiveForumHarness,
    *,
    launcher_thread_id: int,
    fork: bool,
    alias: str,
    launch_token: str,
    lineage_token: str,
    timeout: float = 240.0,
) -> tuple[int, dict[str, str]]:
    baseline = await harness.platform.latest_bot_message_id(thread_id=launcher_thread_id)
    await harness.platform.send(
        (
            "This is a deterministic lineage smoke test. "
            f"Use AgentTask exactly once with fork={'true' if fork else 'false'}, "
            f"display_name={alias!r}, and prompt "
            f"Important: the display_name must be exactly {alias!r}. "
            f"Do not derive the display_name from {launch_token!r} or {lineage_token!r}, "
            "and do not invent a different display_name, alias, name, or description. "
            "Use the display_name string verbatim. "
            "Then pass this exact prompt to the launched worker: "
            f"'Call session_lineage exactly once. "
            f"Then reply with exactly {lineage_token}|root_team_key=<value>|agent_name=<value>|lineage_length=<value> "
            "using the literal values returned by the tool. "
            "Replace the placeholders with the real values. Do not explain or add any other text.' "
            f"After launching, reply with only {launch_token}."
        ),
        thread_id=launcher_thread_id,
        require_done=False,
        timeout=timeout,
    )
    await _wait_for_message_after_containing(
        harness,
        thread_id=launcher_thread_id,
        after_message_id=baseline,
        token=launch_token,
        timeout=timeout + 40.0,
    )
    launch_message = await _wait_for_message_after_containing(
        harness,
        thread_id=launcher_thread_id,
        after_message_id=baseline,
        token="fork task launched" if fork else "agent task launched",
        timeout=timeout + 40.0,
    )
    child_thread_id, _ = _extract_topic_link(launch_message.text)
    child_launch_message = await _wait_for_message_containing(
        harness,
        thread_id=child_thread_id,
        token="agentId:",
        timeout=240.0,
    )
    deadline = asyncio.get_running_loop().time() + 420.0
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=child_thread_id, limit=40)
        for message in recent:
            if message.message_id <= child_launch_message.message_id or f"{lineage_token}|" not in message.text:
                continue
            try:
                return child_thread_id, _extract_lineage_fact_line(message.text)
            except AssertionError:
                continue
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for parseable launched-worker lineage token={lineage_token} "
                f"after message {child_launch_message.message_id}\n"
                + harness.failure_context()
            )
        await asyncio.sleep(1.0)


async def _send_inbox_message_and_wait_ack(
    harness: _LiveForumHarness,
    *,
    sender_thread_id: int,
    recipient: str,
    content: str,
    ack_token: str,
    summary: str = "fan-in",
    timeout: float = 240.0,
) -> None:
    baseline = await harness.platform.latest_bot_message_id(thread_id=sender_thread_id)
    await harness.platform.send(
        (
            "This is a deterministic inbox-routing smoke test. "
            f"Use SendInboxMessage exactly once with recipient={recipient}, "
            f"content={content!r}, summary={summary!r}, and omit team_name and sender. "
            f"Reply with only {ack_token}."
        ),
        thread_id=sender_thread_id,
        require_done=False,
        timeout=timeout,
    )
    await _wait_for_exact_message_after_any_token(
        harness,
        thread_id=sender_thread_id,
        after_message_id=baseline,
        tokens=[ack_token],
        timeout=timeout + 120.0,
    )


async def _send_inbox_message_and_expect_outcome(
    harness: _LiveForumHarness,
    *,
    sender_thread_id: int,
    recipient: str,
    content: str,
    delivered_token: str,
    undelivered_token: str,
    summary: str = "fan-in",
    timeout: float = 240.0,
) -> bool:
    baseline = await harness.platform.latest_bot_message_id(thread_id=sender_thread_id)
    await harness.platform.send(
        (
            "This is a deterministic inbox-routing smoke test. "
            f"Use SendInboxMessage exactly once with recipient={recipient}, "
            f"content={content!r}, summary={summary!r}, and omit team_name and sender. "
            f"If the tool reports delivered=false or returns an error, reply with only {undelivered_token}. "
            f"Otherwise reply with only {delivered_token}."
        ),
        thread_id=sender_thread_id,
        require_done=False,
        timeout=timeout,
    )
    result_message = await _wait_for_exact_message_after_any_token(
        harness,
        thread_id=sender_thread_id,
        after_message_id=baseline,
        tokens=[delivered_token, undelivered_token],
        timeout=timeout + 120.0,
    )
    return _message_is_exact_token(result_message.text, delivered_token)


async def _wait_for_message_after_any_token(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
    after_message_id: int,
    tokens: list[str],
    timeout: float = 120.0,
    limit: int = 40,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=limit)
        exact_match: TelegramForumObservedMessage | None = None
        substring_match: TelegramForumObservedMessage | None = None
        for message in recent:
            if message.message_id <= after_message_id:
                continue
            if exact_match is None and any(
                _message_is_exact_token(message.text, token) for token in tokens
            ):
                exact_match = message
                continue
            if substring_match is None and any(token in message.text for token in tokens):
                substring_match = message
        if exact_match is not None:
            return exact_match
        if substring_match is not None:
            return substring_match
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for any of {tokens!r} after message {after_message_id} in thread {thread_id}\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


async def _wait_for_exact_message_after_any_token(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
    after_message_id: int,
    tokens: list[str],
    timeout: float = 120.0,
    limit: int = 40,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=limit)
        for message in recent:
            if message.message_id <= after_message_id:
                continue
            if any(_message_is_exact_token(message.text, token) for token in tokens):
                return message
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for exact reply among {tokens!r} after message {after_message_id} in thread {thread_id}\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestTelegramLiveSmoke:
    async def test_live_smoke_core_supervisor_lifecycle(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Core {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only CORE-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"CORE-PRIME-{tag}",
            timeout=180.0,
        )
        await live_tg_forum.platform.rename_topic(parent_thread_id, f"Smoke Renamed {tag}")
        await asyncio.sleep(3.0)
        parent_session_before = await _session_id_for_route(
            live_tg_forum,
            thread_id=parent_thread_id,
        )
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use the AgentTask tool exactly once with fork=true, description CORE-CHILD-{tag}, and prompt "
                "'This is a deterministic smoke test inside the child. "
                "You must use Bash to execute exactly sleep 20 before final answer. "
                f"After sleep, reply with only CORE-CHILD-DONE-{tag}.' "
                f"After launch, reply with only CORE-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=120.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="fork task launched",
            timeout=180.0,
        )
        assert f"Smoke Renamed {tag}" in launch_message.text, live_tg_forum.failure_context()
        child_thread_id, _ = _extract_topic_link(launch_message.text)

        child_launch = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="agentId:",
            timeout=180.0,
        )
        handle = _extract_agent_id(child_launch.text)

        output_probe = await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Call AgentTaskOutput exactly once with task_id={handle}, block=false, timeout=1. "
                f"Reply with only CORE-RUNNING-{tag} if it reports not_ready/running, else CORE-OTHER-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=True,
            timeout=180.0,
        )
        assert (
            f"CORE-RUNNING-{tag}" in output_probe.output
            or f"CORE-OTHER-{tag}" in output_probe.output
        ), live_tg_forum.failure_context()

        stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Call AgentTaskStop exactly once with task_id={handle}. "
                f"Reply with only CORE-STOP-SENT-{tag} if it succeeds, otherwise CORE-STOP-NOP-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=120.0,
        )
        stop_result = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=stop_baseline,
            tokens=[f"CORE-STOP-SENT-{tag}", f"CORE-STOP-NOP-{tag}"],
            timeout=240.0,
        )
        if f"CORE-STOP-SENT-{tag}" in stop_result.text:
            child_terminal = await _wait_for_message_after_any_token(
                live_tg_forum,
                thread_id=child_thread_id,
                after_message_id=child_launch.message_id,
                tokens=["fork task stopped", f"CORE-CHILD-DONE-{tag}", "fork completed"],
                timeout=180.0,
            )
            assert (
                "fork task stopped" in child_terminal.text.lower()
                or f"CORE-CHILD-DONE-{tag}" in child_terminal.text
                or "fork completed" in child_terminal.text.lower()
            ), live_tg_forum.failure_context()

        resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Resume the same AgentTask handle {handle} using AgentTask with fork=true, resume={handle}, and prompt "
                f"'Reply with only CORE-RESUME-DONE-{tag}.' "
                f"After launching resumed work, reply with only CORE-RESUMED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=150.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=resume_baseline,
            token=f"CORE-RESUMED-{tag}",
            timeout=240.0,
        )
        resumed_child = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"CORE-RESUME-DONE-{tag}",
            timeout=240.0,
        )
        assert f"CORE-RESUME-DONE-{tag}" in resumed_child.text, live_tg_forum.failure_context()

        fresh_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use AgentTask exactly once with fork=false, description CORE-FRESH-{tag}, and prompt "
                f"'Run Bash sleep 35, then reply with only CORE-FRESH-DONE-{tag}.' "
                f"After launch, reply with only CORE-FRESH-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=150.0,
        )
        fresh_launch = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=fresh_baseline,
            token="agent task launched",
            timeout=240.0,
        )
        fresh_thread_id, _ = _extract_topic_link(fresh_launch.text)
        fresh_done = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=fresh_thread_id,
            token=f"CORE-FRESH-DONE-{tag}",
            timeout=240.0,
        )
        assert f"CORE-FRESH-DONE-{tag}" in fresh_done.text, live_tg_forum.failure_context()

        parent_session_after = await _session_id_for_route(
            live_tg_forum,
            thread_id=parent_thread_id,
        )
        assert parent_session_after == parent_session_before, live_tg_forum.failure_context()

    async def test_live_smoke_stop_all_interrupts_parent_and_delegated_tasks(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke StopAll {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only STOP-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"STOP-PRIME-{tag}",
            timeout=180.0,
        )

        setup_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke stop-all test. "
                "Do not call tools in parallel; execute exactly one tool call at a time. "
                "Use AgentTask exactly twice with fork=true and run_in_background=true. "
                f"First AgentTask description STOP-A-{tag}, prompt "
                f"'Use Bash to run sleep 180 and then reply with only CHILD-A-LATE-{tag}.' "
                f"Second AgentTask description STOP-B-{tag}, prompt "
                f"'Use Bash to run sleep 180 and then reply with only CHILD-B-LATE-{tag}.' "
                "Capture the returned handles and reply with exactly one line in this schema: "
                f'STOP-SETUP-{tag}: {{"child_task_ids":["<id1>","<id2>"]}}'
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=240.0,
        )
        setup_line = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=setup_baseline,
            token=f"STOP-SETUP-{tag}:",
            timeout=360.0,
        )
        setup_payload = _extract_json_object(setup_line.text)
        child_ids_raw = setup_payload.get("child_task_ids")
        assert isinstance(child_ids_raw, list), live_tg_forum.failure_context()
        child_task_ids = [
            str(value).strip()
            for value in child_ids_raw
            if str(value).strip()
        ]
        assert len(child_task_ids) >= 2, live_tg_forum.failure_context()

        deadline = asyncio.get_running_loop().time() + 180.0
        child_threads: set[int] = set()
        while asyncio.get_running_loop().time() < deadline and len(child_threads) < 2:
            recent = await live_tg_forum.platform.get_recent_messages(
                thread_id=parent_thread_id,
                limit=200,
            )
            for msg in recent:
                if msg.message_id <= setup_baseline:
                    continue
                if "fork task launched" not in msg.text.lower():
                    continue
                try:
                    thread_id, _ = _extract_topic_link(msg.text)
                except AssertionError:
                    continue
                child_threads.add(thread_id)
            if len(child_threads) < 2:
                await asyncio.sleep(1.0)
        assert len(child_threads) >= 2, live_tg_forum.failure_context()

        forbidden_tokens = [
            f"PARENT-ALL-LATE-{tag}",
            f"CHILD-A-LATE-{tag}",
            f"CHILD-B-LATE-{tag}",
        ]
        child_late_tokens = [
            f"CHILD-A-LATE-{tag}",
            f"CHILD-B-LATE-{tag}",
        ]
        inspected_threads = {parent_thread_id, *child_threads}

        parent_busy_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic smoke stop-all test. "
                f"Use Bash to run sleep 180, then reply with only PARENT-ALL-LATE-{tag}."
            ),
            thread_id=parent_thread_id,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=parent_busy_baseline,
            token="working",
            timeout=120.0,
        )

        stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        stop_trace = await live_tg_forum.platform.send_control(
            f"/stop@{live_tg_forum.bot_username} all",
            thread_id=parent_thread_id,
            timeout=120.0,
        )
        normalized_stop = stop_trace.output.lower()
        assert (
            "interrupt sent to all topics" in normalized_stop
            or "interrupt sent" in normalized_stop
        ), live_tg_forum.failure_context()

        for child_thread_id in sorted(child_threads):
            child_recent = await live_tg_forum.platform.get_recent_messages(
                thread_id=child_thread_id,
                limit=200,
            )
            completed_before_stop = any(
                message.message_id <= stop_baseline
                and any(token in message.text for token in child_late_tokens)
                for message in child_recent
            )
            if completed_before_stop:
                continue
            stopped = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token="fork task stopped",
                timeout=300.0,
            )
            assert "fork task stopped" in stopped.text.lower(), live_tg_forum.failure_context()

        for thread_id in inspected_threads:
            recent = await live_tg_forum.platform.get_recent_messages(thread_id=thread_id, limit=200)
            for token in forbidden_tokens:
                assert not any(
                    message.message_id > stop_baseline
                    and _message_is_exact_token(message.text, token)
                    for message in recent
                ), live_tg_forum.failure_context()

    async def test_live_smoke_stop_interrupts_parent_but_not_delegated_children(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke StopSingle {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only STOP-SINGLE-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"STOP-SINGLE-PRIME-{tag}",
            timeout=180.0,
        )

        setup_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke stop test. "
                "Do not call tools in parallel; execute exactly one tool call at a time. "
                "Use AgentTask exactly twice with fork=true and run_in_background=true. "
                f"First AgentTask description SINGLE-A-{tag}, prompt "
                f"'Use Bash to run sleep 45 and then reply with only CHILD-SINGLE-A-DONE-{tag}.' "
                f"Second AgentTask description SINGLE-B-{tag}, prompt "
                f"'Use Bash to run sleep 45 and then reply with only CHILD-SINGLE-B-DONE-{tag}.' "
                "Capture the returned handles and reply with exactly one line in this schema: "
                f'STOP-SINGLE-SETUP-{tag}: {{"child_task_ids":["<id1>","<id2>"]}}'
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=240.0,
        )
        setup_line = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=setup_baseline,
            token=f"STOP-SINGLE-SETUP-{tag}:",
            timeout=360.0,
        )
        setup_payload = _extract_json_object(setup_line.text)
        child_ids_raw = setup_payload.get("child_task_ids")
        assert isinstance(child_ids_raw, list), live_tg_forum.failure_context()
        child_task_ids = [
            str(value).strip()
            for value in child_ids_raw
            if str(value).strip()
        ]
        assert len(child_task_ids) >= 2, live_tg_forum.failure_context()

        child_threads: set[int] = set()
        deadline = asyncio.get_running_loop().time() + 180.0
        while asyncio.get_running_loop().time() < deadline and len(child_threads) < 2:
            recent = await live_tg_forum.platform.get_recent_messages(
                thread_id=parent_thread_id,
                limit=200,
            )
            for msg in recent:
                if msg.message_id <= setup_baseline:
                    continue
                if "fork task launched" not in msg.text.lower():
                    continue
                try:
                    thread_id, _ = _extract_topic_link(msg.text)
                except AssertionError:
                    continue
                child_threads.add(thread_id)
            if len(child_threads) < 2:
                await asyncio.sleep(1.0)
        assert len(child_threads) >= 2, live_tg_forum.failure_context()

        parent_busy_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic smoke stop test. "
                f"Use Bash to run sleep 35, then reply with only PARENT-SINGLE-LATE-{tag}."
            ),
            thread_id=parent_thread_id,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=parent_busy_baseline,
            token="working",
            timeout=120.0,
        )

        stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        stop_trace = await live_tg_forum.platform.send_control(
            f"/stop@{live_tg_forum.bot_username}",
            thread_id=parent_thread_id,
            timeout=120.0,
        )
        normalized_stop = stop_trace.output.lower()
        assert "interrupt sent" in normalized_stop, live_tg_forum.failure_context()
        assert "interrupt sent to all topics" not in normalized_stop, live_tg_forum.failure_context()

        completion_trace = await live_tg_forum.platform.wait_for_prompt_after(
            after_message_id=stop_baseline,
            thread_id=parent_thread_id,
            timeout=240.0,
            require_done=True,
        )
        assert "context:" in completion_trace.output.lower(), live_tg_forum.failure_context()

        child_tokens = [
            f"CHILD-SINGLE-A-DONE-{tag}",
            f"CHILD-SINGLE-B-DONE-{tag}",
        ]
        for token in child_tokens:
            found = False
            wait_deadline = asyncio.get_running_loop().time() + 420.0
            while asyncio.get_running_loop().time() < wait_deadline and not found:
                for child_thread_id in sorted(child_threads):
                    recent = await live_tg_forum.platform.get_recent_messages(
                        thread_id=child_thread_id,
                        limit=200,
                    )
                    if any(token in message.text for message in recent):
                        found = True
                        break
                if not found:
                    await asyncio.sleep(1.0)
            assert found, live_tg_forum.failure_context()

        for child_thread_id in sorted(child_threads):
            recent = await live_tg_forum.platform.get_recent_messages(thread_id=child_thread_id, limit=200)
            combined = "\n".join(message.text.lower() for message in recent)
            assert "fork task stopped" not in combined, live_tg_forum.failure_context()

        forbidden_parent_tokens = [
            f"PARENT-SINGLE-LATE-{tag}",
        ]
        parent_recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=parent_thread_id,
            limit=240,
        )
        parent_combined = "\n".join(message.text for message in parent_recent)
        for token in forbidden_parent_tokens:
            assert token not in parent_combined, live_tg_forum.failure_context()

    async def test_live_smoke_queued_message_gets_working_marker_on_delivery(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Smoke QueueWorking {tag}")

        start_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic queue marker smoke test. "
                f"Use Bash to run sleep 25, then reply with only QUEUE-FIRST-DONE-{tag}."
            ),
            thread_id=thread_id,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=start_baseline,
            token="working",
            timeout=120.0,
        )

        queue_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic queue marker smoke test. "
                f"Reply with only QUEUE-SECOND-DONE-{tag}."
            ),
            thread_id=thread_id,
        )
        received = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=queue_baseline,
            token="received",
            timeout=120.0,
        )
        delivered_working = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=received.message_id,
            token="working",
            timeout=300.0,
        )
        assert delivered_working.message_id > received.message_id, live_tg_forum.failure_context()

        cleanup_stop = await live_tg_forum.platform.send_control(
            f"/stop@{live_tg_forum.bot_username}",
            thread_id=thread_id,
            timeout=120.0,
        )
        assert "interrupt sent" in cleanup_stop.output.lower(), live_tg_forum.failure_context()
        completion = await live_tg_forum.platform.wait_for_prompt_after(
            after_message_id=received.message_id,
            thread_id=thread_id,
            timeout=240.0,
            require_done=True,
        )
        assert "context:" in completion.output.lower(), live_tg_forum.failure_context()

    async def test_live_smoke_stop_completion_is_terminal_marker_under_race(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Smoke StopOrder {tag}")

        attempts = 5
        for attempt in range(attempts):
            run_tag = f"{tag}-{attempt}"
            busy_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
            await live_tg_forum.platform.send_nowait(
                (
                    "This is a deterministic completion-order race smoke test. "
                    "Do several tool calls and stay busy: "
                    "run Bash sleep 35, then reply with only "
                    f"ORDER-LATE-{run_tag}."
                ),
                thread_id=thread_id,
            )
            await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=thread_id,
                after_message_id=busy_baseline,
                token="working",
                timeout=120.0,
            )

            stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
            stop_trace = await live_tg_forum.platform.send_control(
                f"/stop@{live_tg_forum.bot_username}",
                thread_id=thread_id,
                timeout=120.0,
            )
            assert "interrupt sent" in stop_trace.output.lower(), live_tg_forum.failure_context()
            completion_trace = await live_tg_forum.platform.wait_for_prompt_after(
                after_message_id=stop_baseline,
                thread_id=thread_id,
                timeout=240.0,
                require_done=True,
            )
            assert "context:" in completion_trace.output.lower(), live_tg_forum.failure_context()

            await asyncio.sleep(2.0)
            recent = await live_tg_forum.platform.get_recent_messages(
                thread_id=thread_id,
                limit=240,
            )
            after_stop = [m for m in recent if m.message_id > stop_baseline]
            completion_messages = [
                m for m in after_stop if "context:" in m.text.lower()
            ]
            assert completion_messages, live_tg_forum.failure_context()
            last_completion_id = max(message.message_id for message in completion_messages)
            trailing = [
                message for message in after_stop
                if message.message_id > last_completion_id
            ]
            assert not trailing, (
                f"Found messages after completion marker on attempt {attempt}: {trailing}\n"
                + live_tg_forum.failure_context()
            )

    async def test_live_smoke_stress_multi_chat_multi_bot_and_collisions(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        second_chat_id = await live_tg_forum.platform.provision_forum_chat()
        second = TelegramForumPlatform(chat_id=second_chat_id, idle_quiescence_timeout=90.0)
        await second.connect()
        try:
            tag = uuid.uuid4().hex[:8]
            chat1_token = f"SMOKE-CHAT1-{tag}"
            chat2_token = f"SMOKE-CHAT2-{tag}"
            first_task = live_tg_forum.platform.send(
                f"This is a deterministic smoke test. Reply with only {chat1_token}.",
                timeout=180.0,
            )
            second_task = second.send(
                f"This is a deterministic smoke test. Reply with only {chat2_token}.",
                timeout=180.0,
            )
            first_trace, second_trace = await asyncio.gather(first_task, second_task)
            assert chat1_token in first_trace.output, live_tg_forum.failure_context()
            assert chat2_token in second_trace.output, second.format_recent_messages()

            baseline = await second.latest_bot_message_id(thread_id=None)
            await second.send(
                (
                    "This is a deterministic stress smoke test. "
                    "Launch exactly six AgentTask children with fork=true in this chat. "
                    f"Use the same description COLLIDE-{tag}-F1 for each launch, "
                    f"with prompts replying only SMOKE-CHILD-{tag}-<index>. "
                    f"After all launches, reply with only SMOKE-LAUNCHED-{tag}."
                ),
                require_done=False,
                timeout=240.0,
            )
            launched = await _wait_for_message_after_containing(
                _LiveForumHarness(
                    platform=second,
                    proc=live_tg_forum.proc,
                    log_file=live_tg_forum.log_file,
                    vault_path=live_tg_forum.vault_path,
                    bot_username=live_tg_forum.bot_username,
                ),
                thread_id=None,
                after_message_id=baseline,
                token=f"SMOKE-LAUNCHED-{tag}",
                timeout=300.0,
            )
            assert f"SMOKE-LAUNCHED-{tag}" in launched.text, second.format_recent_messages()

            deadline = asyncio.get_running_loop().time() + 420.0
            terminal_markers = []
            distinct_threads: set[int] = set()
            while asyncio.get_running_loop().time() < deadline:
                recent = await second.get_recent_messages(thread_id=None, limit=240)
                launches = [
                    msg
                    for msg in recent
                    if msg.message_id > baseline
                    and "fork task launched" in msg.text.lower()
                ]
                for launch in launches:
                    try:
                        thread_id, _ = _extract_topic_link(launch.text)
                        distinct_threads.add(thread_id)
                    except AssertionError:
                        continue
                terminal_markers = [
                    msg
                    for msg in recent
                    if msg.message_id > baseline
                    and (
                        "fork task completed" in msg.text.lower()
                        or "fork task failed" in msg.text.lower()
                        or "fork task timed out" in msg.text.lower()
                        or "fork task stopped" in msg.text.lower()
                    )
                ]
                if len(terminal_markers) >= 6 and len(distinct_threads) >= 6:
                    break
                await asyncio.sleep(2.0)
            assert len(terminal_markers) >= 6, second.format_recent_messages()
            assert len(distinct_threads) >= 6, second.format_recent_messages()

            if len(second._bot_sender_ids) >= 2:
                recent = await second.get_recent_messages(thread_id=None, limit=200)
                senders = {
                    msg.sender_id
                    for msg in recent
                    if msg.message_id > baseline and isinstance(msg.sender_id, int)
                }
                assert len(senders) >= 2, second.format_recent_messages()
            # This scenario deliberately bursts forum-topic creation and background sends.
            # Give Telegram flood-control buckets time to settle so the next live smoke
            # measures runtime behavior instead of residual chat-level rate limits.
            await asyncio.sleep(35.0)
            # Also rotate away from the stressed shared forum chat for the next fixture.
            _clear_cached_forum_chat_id()
        finally:
            await second.close()

    async def test_live_smoke_team_workers_share_task_list_and_inbox(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        team_name = f"smoke-team-{tag}"
        worker_a = f"worker-a-{tag[:4]}"
        worker_b = f"worker-b-{tag[:4]}"
        worker_c = f"worker-c-{tag[:4]}"
        task_subject = f"TEAM-TASK-{tag}"
        inbox_token = f"TEAM-INBOX-{tag}"
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Team {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only TEAM-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"TEAM-PRIME-{tag}",
            timeout=180.0,
        )

        baseline_a = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for team workers. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_a}, description TEAM-A-{tag}, and prompt "
                "'This is a deterministic team-worker task. "
                f"Call TeamCreate with team_name={team_name}. "
                f"Then call TaskCreate with subject={task_subject} and description=\"Shared smoke task\". "
                f"After both tool calls succeed, reply with only WORKER-A-READY-{tag}.' "
                f"After launching, reply with only PARENT-LAUNCHED-A-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_a,
            token=f"PARENT-LAUNCHED-A-{tag}",
            timeout=240.0,
        )
        launch_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_a,
            token="agent task launched",
            timeout=240.0,
        )
        worker_a_thread, _ = _extract_topic_link(launch_a.text)
        worker_a_done = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=worker_a_thread,
            after_message_id=launch_a.message_id,
            tokens=[f"WORKER-A-READY-{tag}", f"WORKER-A-FAIL-{tag}"],
            timeout=420.0,
        )
        assert f"WORKER-A-READY-{tag}" in worker_a_done.text, live_tg_forum.failure_context()

        baseline_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for team workers. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_b}, description TEAM-B-{tag}, and prompt "
                f"'Reply with only WORKER-B-READY-{tag}.' "
                f"After launching, reply with only PARENT-LAUNCHED-B-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_b,
            token=f"PARENT-LAUNCHED-B-{tag}",
            timeout=240.0,
        )
        launch_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_b,
            token="agent task launched",
            timeout=240.0,
        )
        worker_b_thread, _ = _extract_topic_link(launch_b.text)
        worker_b_done = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=worker_b_thread,
            after_message_id=launch_b.message_id,
            tokens=[f"WORKER-B-READY-{tag}", f"WORKER-B-FAIL-{tag}"],
            timeout=420.0,
        )
        assert f"WORKER-B-READY-{tag}" in worker_b_done.text, live_tg_forum.failure_context()

        baseline_a_send = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_a_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for team workers. "
                f"Use SendInboxMessage exactly once with team_name={team_name}, recipient={worker_b}, "
                f"content={inbox_token!r}, summary='handoff', sender={worker_a}. "
                f"Then reply with only WORKER-A-SENT-{tag}."
            ),
            thread_id=worker_a_thread,
            require_done=False,
            timeout=180.0,
        )
        worker_a_sent = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=worker_a_thread,
            after_message_id=baseline_a_send,
            tokens=[f"WORKER-A-SENT-{tag}", f"WORKER-A-SEND-FAIL-{tag}"],
            timeout=420.0,
        )
        assert f"WORKER-A-SENT-{tag}" in worker_a_sent.text, live_tg_forum.failure_context()

        baseline_b_verify = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_b_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for team workers. "
                f"Call TaskList and verify {task_subject} exists. "
                f"Then call ReadInbox with team_name={team_name}, agent={worker_b}, "
                f"include_read=false, mark_read=true, limit=20 and verify {inbox_token} exists. "
                f"If both checks pass, reply with only WORKER-B-OK-{tag}. "
                f"Otherwise reply with only WORKER-B-FAIL-{tag}."
            ),
            thread_id=worker_b_thread,
            require_done=False,
            timeout=180.0,
        )
        worker_b_verified = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=worker_b_thread,
            after_message_id=baseline_b_verify,
            tokens=[f"WORKER-B-OK-{tag}", f"WORKER-B-FAIL-{tag}"],
            timeout=420.0,
        )
        assert f"WORKER-B-OK-{tag}" in worker_b_verified.text, live_tg_forum.failure_context()

        baseline_c = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_b_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for recursive team launch. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_c}, description TEAM-C-{tag}, and prompt "
                f"'Call TaskList and reply with only WORKER-C-OK-{tag} if {task_subject} exists; "
                f"otherwise reply with only WORKER-C-FAIL-{tag}.' "
                f"After launching, reply with only WORKER-B-LAUNCHED-C-{tag}."
            ),
            thread_id=worker_b_thread,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            after_message_id=baseline_c,
            token=f"WORKER-B-LAUNCHED-C-{tag}",
            timeout=240.0,
        )
        launch_c = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            after_message_id=baseline_c,
            token="agent task launched",
            timeout=240.0,
        )
        worker_c_thread, _ = _extract_topic_link(launch_c.text)
        worker_c_done = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=worker_c_thread,
            after_message_id=launch_c.message_id,
            tokens=[f"WORKER-C-OK-{tag}", f"WORKER-C-FAIL-{tag}"],
            timeout=420.0,
        )
        assert f"WORKER-C-OK-{tag}" in worker_c_done.text, live_tg_forum.failure_context()

        session_b = await _session_id_for_route(live_tg_forum, thread_id=worker_b_thread)
        session_c = await _session_id_for_route(live_tg_forum, thread_id=worker_c_thread)
        assert session_b != session_c, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_smoke_lineage_projection_and_cross_branch_inbox_routing(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Lineage {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only LINEAGE-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"LINEAGE-PRIME-{tag}",
            timeout=180.0,
        )

        baseline_root = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                "Call session_lineage exactly once. "
                f"Then reply with exactly LINEAGE-ROOT-{tag}|root_team_key=<value>|agent_name=<value>|lineage_length=<value> "
                "using the literal values returned by the tool."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        lineage_root_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_root,
            token=f"LINEAGE-ROOT-{tag}|",
            timeout=300.0,
        )
        lineage_root = _extract_lineage_fact_line(lineage_root_msg.text)

        worker_a_thread, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=parent_thread_id,
            fork=False,
            alias=f"AUTO-A-{tag}",
            launch_token=f"PARENT-LAUNCHED-A-{tag}",
            lineage_token=f"LINEAGE-A-{tag}",
            timeout=220.0,
        )

        worker_b_thread, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=parent_thread_id,
            fork=False,
            alias=f"AUTO-B-{tag}",
            launch_token=f"PARENT-LAUNCHED-B-{tag}",
            lineage_token=f"LINEAGE-B-{tag}",
            timeout=220.0,
        )

        worker_c_thread, lineage_c = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=worker_b_thread,
            fork=True,
            alias=f"AUTO-C-{tag}",
            launch_token=f"WORKER-B-LAUNCHED-C-{tag}",
            lineage_token=f"LINEAGE-C-{tag}",
            timeout=240.0,
        )

        token_c_to_b = f"LINEAGE-C-TO-B-{tag}"
        baseline_send_c = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_c_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                f"Use SendInboxMessage exactly once with recipient={lineage_b['agent_name']}, "
                f"content={token_c_to_b}, summary=\"cross-branch\", and omit team_name and sender. "
                f"Reply with only WORKER-C-SENT-B-{tag}."
            ),
            thread_id=worker_c_thread,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_c_thread,
            after_message_id=baseline_send_c,
            token=f"WORKER-C-SENT-B-{tag}",
            timeout=300.0,
        )

        token_b_to_a = f"LINEAGE-B-TO-A-{tag}"
        baseline_read_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_b_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_c_to_b} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_a['agent_name']}, "
                f"content={token_b_to_a}, summary=\"bounce\", and omit team_name and sender. "
                f"Reply with only WORKER-B-READ-C-SENT-A-{tag}."
            ),
            thread_id=worker_b_thread,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            after_message_id=baseline_read_b,
            token=f"WORKER-B-READ-C-SENT-A-{tag}",
            timeout=360.0,
        )

        baseline_read_a = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_a_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_b_to_a} exists in unread messages. "
                f"Reply with only WORKER-A-READ-B-{tag}."
            ),
            thread_id=worker_a_thread,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            after_message_id=baseline_read_a,
            token=f"WORKER-A-READ-B-{tag}",
            timeout=300.0,
        )

        token_root_to_c = f"LINEAGE-ROOT-TO-C-{tag}"
        baseline_send_root = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                f"Use SendInboxMessage exactly once with recipient={lineage_c['agent_name']}, "
                f"content={token_root_to_c}, summary=\"root-down\", and omit team_name and sender. "
                f"Reply with only ROOT-SENT-C-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_send_root,
            token=f"ROOT-SENT-C-{tag}",
            timeout=300.0,
        )

        token_c_to_root = f"LINEAGE-C-TO-ROOT-{tag}"
        baseline_read_c = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_c_thread)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_root_to_c} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_root['agent_name']}, "
                f"content={token_c_to_root}, summary=\"root-up\", and omit team_name and sender. "
                f"Reply with only WORKER-C-READ-ROOT-SENT-ROOT-{tag}."
            ),
            thread_id=worker_c_thread,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_c_thread,
            after_message_id=baseline_read_c,
            token=f"WORKER-C-READ-ROOT-SENT-ROOT-{tag}",
            timeout=360.0,
        )

        baseline_root_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_c_to_root} exists in unread messages. "
                f"Reply with only ROOT-READ-C-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_root_read,
            token=f"ROOT-READ-C-{tag}",
            timeout=300.0,
        )

        assert lineage_root["root_team_key"] == lineage_a["root_team_key"], live_tg_forum.failure_context()
        assert lineage_a["root_team_key"] == lineage_b["root_team_key"], live_tg_forum.failure_context()
        assert lineage_b["root_team_key"] == lineage_c["root_team_key"], live_tg_forum.failure_context()
        assert lineage_root["agent_name"] != lineage_a["agent_name"], live_tg_forum.failure_context()
        assert lineage_a["agent_name"] != lineage_b["agent_name"], live_tg_forum.failure_context()
        assert lineage_b["agent_name"] != lineage_c["agent_name"], live_tg_forum.failure_context()
        assert int(lineage_root["lineage_length"]) == 1, live_tg_forum.failure_context()
        assert int(lineage_a["lineage_length"]) == 2, live_tg_forum.failure_context()
        assert int(lineage_b["lineage_length"]) == 2, live_tg_forum.failure_context()
        assert int(lineage_c["lineage_length"]) == 3, live_tg_forum.failure_context()

    async def test_live_smoke_search_team_child_alias_and_parent_reply(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Search Team {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only SEARCHTEAM-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"SEARCHTEAM-PRIME-{tag}",
            timeout=180.0,
        )

        thread_a, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"Alpha-{tag}",
            launch_token=f"ROOT-LAUNCHED-ALPHA-{tag}",
            lineage_token=f"LINEAGE-ALPHA-{tag}",
            timeout=240.0,
        )
        thread_b, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=True,
            alias=f"Bravo-{tag}",
            launch_token=f"ALPHA-LAUNCHED-BRAVO-{tag}",
            lineage_token=f"LINEAGE-BRAVO-{tag}",
            timeout=240.0,
        )
        await asyncio.sleep(2.0)

        root_tree = await _query_search_team_payload(
            live_tg_forum,
            thread_id=root_thread_id,
            mode="tree",
            timeout=240.0,
        )
        assert root_tree["mode"] == "tree", live_tg_forum.failure_context()
        assert lineage_a["agent_name"] in root_tree["tree"], live_tg_forum.failure_context()
        assert lineage_b["agent_name"] in root_tree["tree"], live_tg_forum.failure_context()

        bravo_family = await _query_search_team_payload(
            live_tg_forum,
            thread_id=thread_b,
            mode="family",
            timeout=240.0,
        )
        assert bravo_family["mode"] == "family", live_tg_forum.failure_context()
        assert bravo_family["parent"] == lineage_a["agent_name"], live_tg_forum.failure_context()
        assert bravo_family["children"] == [], live_tg_forum.failure_context()
        await asyncio.sleep(2.0)

        token_root_to_alpha = f"SEARCHTEAM-ROOT-ALPHA-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread_id,
            recipient=f"Alpha-{tag}",
            content=token_root_to_alpha,
            ack_token=f"ROOT-ALIAS-SENT-ALPHA-{tag}",
            summary="search-team-root-alias",
            timeout=240.0,
        )
        alpha_read = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic search-team smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {token_root_to_alpha}, reply with only ALPHA-GOT-ROOT-{tag}. "
                f"Otherwise reply with only ALPHA-MISSED-ROOT-{tag}."
            ),
            thread_id=thread_a,
            token=f"ALPHA-GOT-ROOT-{tag}",
            timeout=300.0,
        )
        assert f"ALPHA-GOT-ROOT-{tag}" in alpha_read.text, live_tg_forum.failure_context()
        await asyncio.sleep(2.0)

        token_alpha_to_bravo = f"SEARCHTEAM-ALPHA-BRAVO-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=thread_a,
            recipient=f"Bravo-{tag}",
            content=token_alpha_to_bravo,
            ack_token=f"ALPHA-ALIAS-SENT-BRAVO-{tag}",
            summary="search-team-child-alias",
            timeout=240.0,
        )
        bravo_read = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic search-team smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {token_alpha_to_bravo}, reply with only BRAVO-GOT-ALPHA-{tag}. "
                f"Otherwise reply with only BRAVO-MISSED-ALPHA-{tag}."
            ),
            thread_id=thread_b,
            token=f"BRAVO-GOT-ALPHA-{tag}",
            timeout=300.0,
        )
        assert f"BRAVO-GOT-ALPHA-{tag}" in bravo_read.text, live_tg_forum.failure_context()
        await asyncio.sleep(2.0)

        baseline_bravo = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_b)
        token_bravo_to_parent = f"SEARCHTEAM-BRAVO-PARENT-{tag}"
        await live_tg_forum.platform.send(
            (
                "This is a deterministic search-team smoke test. "
                "Call search_team exactly once with mode='parent'. "
                f"Use SendInboxMessage exactly once with recipient equal to that parent and content={token_bravo_to_parent!r}, "
                "summary='search-team-parent', and omit team_name and sender. "
                f"Then reply with only BRAVO-SENT-PARENT-{tag}."
            ),
            thread_id=thread_b,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_b,
            after_message_id=baseline_bravo,
            token=f"BRAVO-SENT-PARENT-{tag}",
            timeout=360.0,
        )
        parent_read = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic search-team smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {token_bravo_to_parent}, reply with only ALPHA-GOT-PARENT-REPLY-{tag}. "
                f"Otherwise reply with only ALPHA-MISSED-PARENT-REPLY-{tag}."
            ),
            thread_id=thread_a,
            token=f"ALPHA-GOT-PARENT-REPLY-{tag}",
            timeout=300.0,
        )
        assert f"ALPHA-GOT-PARENT-REPLY-{tag}" in parent_read.text, live_tg_forum.failure_context()

    async def test_live_smoke_restart_preserves_exact_team_key_and_parent_routing(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Restart Parent {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only RESTART-PARENT-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"RESTART-PARENT-PRIME-{tag}",
            timeout=180.0,
        )

        child_thread_id, lineage_child = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"RestartChild-{tag}",
            launch_token=f"RESTART-LAUNCHED-CHILD-{tag}",
            lineage_token=f"RESTART-LINEAGE-CHILD-{tag}",
            timeout=240.0,
        )
        grandchild_thread_id, lineage_grandchild = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=child_thread_id,
            fork=False,
            alias=f"RestartGrand-{tag}",
            launch_token=f"RESTART-LAUNCHED-GRAND-{tag}",
            lineage_token=f"RESTART-LINEAGE-GRAND-{tag}",
            timeout=240.0,
        )
        await asyncio.sleep(2.0)

        lineage_before = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            timeout=240.0,
        )
        family_before = await _query_search_team_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            mode="family",
            timeout=240.0,
        )
        assert family_before["parent"] == lineage_child["agent_name"], live_tg_forum.failure_context()
        await asyncio.sleep(2.0)

        _stop_bot(live_tg_forum.proc)
        assert live_tg_forum.temp_root is not None
        live_tg_forum.proc, live_tg_forum.log_file = _start_bot(
            live_tg_forum.vault_path,
            live_tg_forum.temp_root,
            state_db_path=live_tg_forum.state_db_path,
        )
        await asyncio.sleep(2.0)

        root_post_restart_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only RESTART-ROOT-READY-{tag}.",
            thread_id=root_thread_id,
            token=f"RESTART-ROOT-READY-{tag}",
            timeout=240.0,
        )
        assert root_post_restart_baseline is not None

        lineage_after = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            timeout=240.0,
        )
        family_after = await _query_search_team_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            mode="family",
            timeout=240.0,
        )
        assert lineage_after["root_team_key"] == lineage_before["root_team_key"], live_tg_forum.failure_context()
        assert lineage_after["agent_name"] == lineage_before["agent_name"], live_tg_forum.failure_context()
        assert family_after["parent"] == lineage_child["agent_name"], live_tg_forum.failure_context()

        baseline_grand = await live_tg_forum.platform.latest_bot_message_id(thread_id=grandchild_thread_id)
        token_post_restart_parent = f"RESTART-PARENT-TOKEN-{tag}"
        await live_tg_forum.platform.send(
            (
                "This is a deterministic restart-parent smoke test. "
                "Call search_team exactly once with mode='parent'. "
                f"Use SendInboxMessage exactly once with recipient equal to that parent and content={token_post_restart_parent!r}, "
                "summary='restart-parent', and omit team_name and sender. "
                f"Then reply with only GRAND-SENT-PARENT-AFTER-RESTART-{tag}."
            ),
            thread_id=grandchild_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            after_message_id=baseline_grand,
            token=f"GRAND-SENT-PARENT-AFTER-RESTART-{tag}",
            timeout=360.0,
        )
        parent_check = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic restart-parent smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {token_post_restart_parent}, reply with only PARENT-GOT-RESTART-{tag}. "
                f"Otherwise reply with only PARENT-MISSED-RESTART-{tag}."
            ),
            thread_id=child_thread_id,
            token=f"PARENT-GOT-RESTART-{tag}",
            timeout=300.0,
        )
        assert f"PARENT-GOT-RESTART-{tag}" in parent_check.text, live_tg_forum.failure_context()

    async def test_live_smoke_deep_lineage_any_direction_inbox_routing(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Deep Lineage {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only DEEP-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"DEEP-PRIME-{tag}",
            timeout=180.0,
        )

        baseline_root = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call session_lineage exactly once. "
                f"Then reply with exactly DEEP-ROOT-{tag}|root_team_key=<value>|agent_name=<value>|lineage_length=<value> "
                "using the literal values returned by the tool."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=180.0,
        )
        root_lineage_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_root,
            token=f"DEEP-ROOT-{tag}|",
            timeout=300.0,
        )
        lineage_root = _extract_lineage_fact_line(root_lineage_msg.text)

        thread_a, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"DEEP-A-{tag}",
            launch_token=f"ROOT-LAUNCHED-A-{tag}",
            lineage_token=f"DEEP-A-{tag}",
            timeout=220.0,
        )
        thread_b, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=True,
            alias=f"DEEP-B-{tag}",
            launch_token=f"A-LAUNCHED-B-{tag}",
            lineage_token=f"DEEP-B-{tag}",
            timeout=240.0,
        )
        thread_c, lineage_c = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_b,
            fork=True,
            alias=f"DEEP-C-{tag}",
            launch_token=f"B-LAUNCHED-C-{tag}",
            lineage_token=f"DEEP-C-{tag}",
            timeout=240.0,
        )
        thread_d, lineage_d = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_c,
            fork=True,
            alias=f"DEEP-D-{tag}",
            launch_token=f"C-LAUNCHED-D-{tag}",
            lineage_token=f"DEEP-D-{tag}",
            timeout=240.0,
        )
        thread_e, lineage_e = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=False,
            alias=f"DEEP-E-{tag}",
            launch_token=f"A-LAUNCHED-E-{tag}",
            lineage_token=f"DEEP-E-{tag}",
            timeout=240.0,
        )

        token_d_to_root = f"DEEP-D-TO-ROOT-{tag}"
        baseline_send_d = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_d)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                f"Use SendInboxMessage exactly once with recipient={lineage_root['agent_name']}, "
                f"content={token_d_to_root}, summary=\"deep-up\", and omit team_name and sender. "
                f"Reply with only D-SENT-ROOT-{tag}."
            ),
            thread_id=thread_d,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_d,
            after_message_id=baseline_send_d,
            token=f"D-SENT-ROOT-{tag}",
            timeout=300.0,
        )

        token_root_to_e = f"DEEP-ROOT-TO-E-{tag}"
        baseline_root_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_d_to_root} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_e['agent_name']}, "
                f"content={token_root_to_e}, summary=\"deep-down\", and omit team_name and sender. "
                f"Reply with only ROOT-READ-D-SENT-E-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_root_read,
            token=f"ROOT-READ-D-SENT-E-{tag}",
            timeout=360.0,
        )

        token_e_to_b = f"DEEP-E-TO-B-{tag}"
        baseline_e_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_e)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_root_to_e} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_b['agent_name']}, "
                f"content={token_e_to_b}, summary=\"sibling-hop\", and omit team_name and sender. "
                f"Reply with only E-READ-ROOT-SENT-B-{tag}."
            ),
            thread_id=thread_e,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_e,
            after_message_id=baseline_e_read,
            token=f"E-READ-ROOT-SENT-B-{tag}",
            timeout=360.0,
        )

        token_b_to_d = f"DEEP-B-TO-D-{tag}"
        baseline_b_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_b)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_e_to_b} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_d['agent_name']}, "
                f"content={token_b_to_d}, summary=\"deep-down-2\", and omit team_name and sender. "
                f"Reply with only B-READ-E-SENT-D-{tag}."
            ),
            thread_id=thread_b,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_b,
            after_message_id=baseline_b_read,
            token=f"B-READ-E-SENT-D-{tag}",
            timeout=360.0,
        )

        token_d_to_c = f"DEEP-D-TO-C-{tag}"
        baseline_d_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_d)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_b_to_d} exists in unread messages. "
                f"Then use SendInboxMessage exactly once with recipient={lineage_c['agent_name']}, "
                f"content={token_d_to_c}, summary=\"parent-hop\", and omit team_name and sender. "
                f"Reply with only D-READ-B-SENT-C-{tag}."
            ),
            thread_id=thread_d,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_d,
            after_message_id=baseline_d_read,
            token=f"D-READ-B-SENT-C-{tag}",
            timeout=360.0,
        )

        baseline_c_read = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_c)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic deep-lineage smoke test. "
                "Call ReadInbox exactly once with no arguments and verify "
                f"{token_d_to_c} exists in unread messages. "
                f"Reply with only C-READ-D-{tag}."
            ),
            thread_id=thread_c,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_c,
            after_message_id=baseline_c_read,
            token=f"C-READ-D-{tag}",
            timeout=300.0,
        )

        all_lineages = [lineage_root, lineage_a, lineage_b, lineage_c, lineage_d, lineage_e]
        root_keys = {entry["root_team_key"] for entry in all_lineages}
        agent_names = {entry["agent_name"] for entry in all_lineages}
        assert len(root_keys) == 1, live_tg_forum.failure_context()
        assert len(agent_names) == len(all_lineages), live_tg_forum.failure_context()
        assert int(lineage_root["lineage_length"]) == 1, live_tg_forum.failure_context()
        assert int(lineage_a["lineage_length"]) == 2, live_tg_forum.failure_context()
        assert int(lineage_b["lineage_length"]) == 3, live_tg_forum.failure_context()
        assert int(lineage_c["lineage_length"]) == 4, live_tg_forum.failure_context()
        assert int(lineage_d["lineage_length"]) == 5, live_tg_forum.failure_context()
        assert int(lineage_e["lineage_length"]) == 3, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_smoke_lineage_concurrent_cross_branch_wake_roundtrip(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Lineage Wake {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only LINEAGE-WAKE-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"LINEAGE-WAKE-PRIME-{tag}",
            timeout=180.0,
        )

        thread_a, lineage_a = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"WAKE-A-{tag}",
            launch_token=f"ROOT-LAUNCHED-WAKE-A-{tag}",
            lineage_token=f"LINEAGE-WAKE-A-{tag}",
            timeout=220.0,
        )
        thread_b, lineage_b = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=True,
            alias=f"WAKE-B-{tag}",
            launch_token=f"A-LAUNCHED-WAKE-B-{tag}",
            lineage_token=f"LINEAGE-WAKE-B-{tag}",
            timeout=240.0,
        )
        thread_d, lineage_d = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"WAKE-D-{tag}",
            launch_token=f"ROOT-LAUNCHED-WAKE-D-{tag}",
            lineage_token=f"LINEAGE-WAKE-D-{tag}",
            timeout=220.0,
        )
        thread_c, lineage_c = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_d,
            fork=True,
            alias=f"WAKE-C-{tag}",
            launch_token=f"D-LAUNCHED-WAKE-C-{tag}",
            lineage_token=f"LINEAGE-WAKE-C-{tag}",
            timeout=240.0,
        )

        ping_b_to_d = f"LINEAGE-PING-B-TO-D-{tag}"
        ping_c_to_a = f"LINEAGE-PING-C-TO-A-{tag}"
        pong_d_to_b = f"LINEAGE-PONG-D-TO-B-{tag}"
        pong_a_to_c = f"LINEAGE-PONG-A-TO-C-{tag}"

        baseline_a_ready = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_a)
        baseline_d_ready = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_d)
        await asyncio.gather(
            live_tg_forum.platform.send(
                (
                    "This is a deterministic concurrent lineage wake smoke test. "
                    f"Reply with only A-WAKE-READY-{tag} now. "
                    f"Important for later wake turns: if you read an inbox message containing {ping_c_to_a}, "
                    f"send SendInboxMessage to recipient={lineage_c['agent_name']} with content={pong_a_to_c}, "
                    "summary=\"lineage-pong-a\", and omit team_name and sender. "
                    f"Then reply with only A-WAKE-PONG-SENT-{tag}."
                ),
                thread_id=thread_a,
                require_done=False,
                timeout=180.0,
            ),
            live_tg_forum.platform.send(
                (
                    "This is a deterministic concurrent lineage wake smoke test. "
                    f"Reply with only D-WAKE-READY-{tag} now. "
                    f"Important for later wake turns: if you read an inbox message containing {ping_b_to_d}, "
                    f"send SendInboxMessage to recipient={lineage_b['agent_name']} with content={pong_d_to_b}, "
                    "summary=\"lineage-pong-d\", and omit team_name and sender. "
                    f"Then reply with only D-WAKE-PONG-SENT-{tag}."
                ),
                thread_id=thread_d,
                require_done=False,
                timeout=180.0,
            ),
        )
        ready_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_a,
            after_message_id=baseline_a_ready,
            token=f"A-WAKE-READY-{tag}",
            timeout=300.0,
        )
        ready_d = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_d,
            after_message_id=baseline_d_ready,
            token=f"D-WAKE-READY-{tag}",
            timeout=300.0,
        )

        baseline_b_send = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_b)
        baseline_c_send = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_c)
        await asyncio.gather(
            live_tg_forum.platform.send(
                (
                    "This is a deterministic concurrent lineage wake smoke test. "
                    f"Use SendInboxMessage exactly once with recipient={lineage_d['agent_name']}, "
                    f"content={ping_b_to_d}, summary=\"cross-branch-ping-b\", and omit team_name and sender. "
                    f"Then reply with only B-SENT-D-{tag}. "
                    f"Important for later wake turns: if you read an inbox message containing {pong_d_to_b}, "
                    f"reply with only B-GOT-PONG-{tag}."
                ),
                thread_id=thread_b,
                require_done=False,
                timeout=220.0,
            ),
            live_tg_forum.platform.send(
                (
                    "This is a deterministic concurrent lineage wake smoke test. "
                    f"Use SendInboxMessage exactly once with recipient={lineage_a['agent_name']}, "
                    f"content={ping_c_to_a}, summary=\"cross-branch-ping-c\", and omit team_name and sender. "
                    f"Then reply with only C-SENT-A-{tag}. "
                    f"Important for later wake turns: if you read an inbox message containing {pong_a_to_c}, "
                    f"reply with only C-GOT-PONG-{tag}."
                ),
                thread_id=thread_c,
                require_done=False,
                timeout=220.0,
            ),
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_b,
            after_message_id=baseline_b_send,
            token=f"B-SENT-D-{tag}",
            timeout=320.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_c,
            after_message_id=baseline_c_send,
            token=f"C-SENT-A-{tag}",
            timeout=320.0,
        )

        wake_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_a,
            after_message_id=ready_a.message_id,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        wake_d = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_d,
            after_message_id=ready_d.message_id,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        assert lineage_c["agent_name"] in wake_a.text, live_tg_forum.failure_context()
        assert lineage_b["agent_name"] in wake_d.text, live_tg_forum.failure_context()

        pong_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_a,
            after_message_id=ready_a.message_id,
            token=f"A-WAKE-PONG-SENT-{tag}",
            timeout=480.0,
        )
        pong_d = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_d,
            after_message_id=ready_d.message_id,
            token=f"D-WAKE-PONG-SENT-{tag}",
            timeout=480.0,
        )
        assert f"A-WAKE-PONG-SENT-{tag}" in pong_a.text, live_tg_forum.failure_context()
        assert f"D-WAKE-PONG-SENT-{tag}" in pong_d.text, live_tg_forum.failure_context()

        wake_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_b,
            after_message_id=baseline_b_send,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        wake_c = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_c,
            after_message_id=baseline_c_send,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        assert lineage_d["agent_name"] in wake_b.text, live_tg_forum.failure_context()
        assert lineage_a["agent_name"] in wake_c.text, live_tg_forum.failure_context()

        got_pong_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_b,
            after_message_id=baseline_b_send,
            token=f"B-GOT-PONG-{tag}",
            timeout=480.0,
        )
        got_pong_c = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_c,
            after_message_id=baseline_c_send,
            token=f"C-GOT-PONG-{tag}",
            timeout=480.0,
        )
        assert f"B-GOT-PONG-{tag}" in got_pong_b.text, live_tg_forum.failure_context()
        assert f"C-GOT-PONG-{tag}" in got_pong_c.text, live_tg_forum.failure_context()

        root_keys = {
            lineage_a["root_team_key"],
            lineage_b["root_team_key"],
            lineage_c["root_team_key"],
            lineage_d["root_team_key"],
        }
        agent_names = {
            lineage_a["agent_name"],
            lineage_b["agent_name"],
            lineage_c["agent_name"],
            lineage_d["agent_name"],
        }
        assert len(root_keys) == 1, live_tg_forum.failure_context()
        assert len(agent_names) == 4, live_tg_forum.failure_context()
        assert int(lineage_a["lineage_length"]) == 2, live_tg_forum.failure_context()
        assert int(lineage_b["lineage_length"]) == 3, live_tg_forum.failure_context()
        assert int(lineage_d["lineage_length"]) == 2, live_tg_forum.failure_context()
        assert int(lineage_c["lineage_length"]) == 3, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_smoke_team_peer_discovery_and_wake_roundtrip(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        team_name = f"peer-team-{tag}"
        worker_a = f"worker-a-{tag[:4]}"
        worker_b = f"worker-b-{tag[:4]}"
        ping_token = f"PEER-PING-{tag}"
        pong_token = f"PEER-PONG-{tag}"
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Peer {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only PEER-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"PEER-PRIME-{tag}",
            timeout=180.0,
        )

        baseline_a = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic team peer-discovery smoke test. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_a}, description PEER-A-{tag}, and prompt "
                "'Call TeamCreate with team_name="
                f"{team_name}. "
                f"Reply with only WORKER-A-READY-{tag}. "
                f"Important for later wake turns: if you read an inbox message containing {ping_token}, "
                f"send SendInboxMessage back to the sender with content={pong_token}, summary=\"pong\", sender={worker_a}, "
                f"then reply with only WORKER-A-PONG-SENT-{tag}.' "
                f"After launching, reply with only PARENT-LAUNCHED-A-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=220.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_a,
            token=f"PARENT-LAUNCHED-A-{tag}",
            timeout=260.0,
        )
        launch_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_a,
            token="agent task launched",
            timeout=260.0,
        )
        worker_a_thread, _ = _extract_topic_link(launch_a.text)
        ready_a = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            token=f"WORKER-A-READY-{tag}",
            timeout=420.0,
        )
        assert f"WORKER-A-READY-{tag}" in ready_a.text, live_tg_forum.failure_context()

        baseline_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic team peer-discovery smoke test. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_b}, description PEER-B-{tag}, and prompt "
                "'Call TeamCreate with team_name="
                f"{team_name}. "
                f"Use Bash to read ~/.claude/teams/{team_name}/config.json and find a teammate name that is neither team-lead nor {worker_b}. "
                f"Call SendInboxMessage to that discovered teammate with content={ping_token}, summary=\"ping\", sender={worker_b}. "
                f"Then reply with only WORKER-B-SENT-{tag}. "
                f"Important for later wake turns: if you read an inbox message containing {pong_token}, "
                f"reply with only WORKER-B-GOT-PONG-{tag}.' "
                f"After launching, reply with only PARENT-LAUNCHED-B-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_b,
            token=f"PARENT-LAUNCHED-B-{tag}",
            timeout=280.0,
        )
        launch_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_b,
            token="agent task launched",
            timeout=280.0,
        )
        worker_b_thread, _ = _extract_topic_link(launch_b.text)
        sent_b = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            token=f"WORKER-B-SENT-{tag}",
            timeout=480.0,
        )
        assert f"WORKER-B-SENT-{tag}" in sent_b.text, live_tg_forum.failure_context()

        wake_a = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        assert worker_b in wake_a.text, live_tg_forum.failure_context()
        pong_a = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            token=f"WORKER-A-PONG-SENT-{tag}",
            timeout=480.0,
        )
        assert f"WORKER-A-PONG-SENT-{tag}" in pong_a.text, live_tg_forum.failure_context()

        wake_b = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            token="agent task wake: teammate message received",
            timeout=480.0,
        )
        assert worker_a in wake_b.text, live_tg_forum.failure_context()
        got_pong_b = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            token=f"WORKER-B-GOT-PONG-{tag}",
            timeout=480.0,
        )
        assert f"WORKER-B-GOT-PONG-{tag}" in got_pong_b.text, live_tg_forum.failure_context()

    async def test_live_smoke_many_to_one_fan_in_delivery_across_lineage_tree(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Fan In {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only FANIN-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"FANIN-PRIME-{tag}",
            timeout=180.0,
        )

        thread_a, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"FAN-A-{tag}",
            launch_token=f"ROOT-LAUNCHED-A-{tag}",
            lineage_token=f"FAN-LINEAGE-A-{tag}",
            timeout=220.0,
        )
        thread_b, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"FAN-B-{tag}",
            launch_token=f"ROOT-LAUNCHED-B-{tag}",
            lineage_token=f"FAN-LINEAGE-B-{tag}",
            timeout=220.0,
        )
        thread_c, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"FAN-C-{tag}",
            launch_token=f"ROOT-LAUNCHED-C-{tag}",
            lineage_token=f"FAN-LINEAGE-C-{tag}",
            timeout=220.0,
        )
        thread_target, lineage_target = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"FAN-TARGET-{tag}",
            launch_token=f"ROOT-LAUNCHED-TARGET-{tag}",
            lineage_token=f"FAN-LINEAGE-TARGET-{tag}",
            timeout=220.0,
        )
        protocol_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_target)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic fan-in smoke test protocol. "
                f"You will receive teammate inbox messages whose exact text starts with FANIN-TOKEN-{tag}-. "
                "Whenever you are woken because teammate messages may have arrived, call ReadInbox as needed, "
                "track the unique FANIN tokens you have seen across turns, and once you have seen exactly 10 unique "
                "FANIN tokens reply with only a JSON object of the exact form "
                "{\"count\": 10, \"tokens\": [<sorted tokens>]}. "
                "Sort the tokens lexicographically and do not add any extra text. "
                f"For now, just confirm you understand by replying with only FANIN-PROTOCOL-ACK-{tag}."
            ),
            thread_id=thread_target,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_target,
            after_message_id=protocol_baseline,
            token=f"FANIN-PROTOCOL-ACK-{tag}",
            timeout=180.0,
        )
        thread_a1, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=True,
            alias=f"FAN-A1-{tag}",
            launch_token=f"A-LAUNCHED-A1-{tag}",
            lineage_token=f"FAN-LINEAGE-A1-{tag}",
            timeout=240.0,
        )
        thread_a2, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_a,
            fork=False,
            alias=f"FAN-A2-{tag}",
            launch_token=f"A-LAUNCHED-A2-{tag}",
            lineage_token=f"FAN-LINEAGE-A2-{tag}",
            timeout=240.0,
        )
        thread_b1, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_b,
            fork=True,
            alias=f"FAN-B1-{tag}",
            launch_token=f"B-LAUNCHED-B1-{tag}",
            lineage_token=f"FAN-LINEAGE-B1-{tag}",
            timeout=240.0,
        )
        thread_b2, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_b,
            fork=False,
            alias=f"FAN-B2-{tag}",
            launch_token=f"B-LAUNCHED-B2-{tag}",
            lineage_token=f"FAN-LINEAGE-B2-{tag}",
            timeout=240.0,
        )
        thread_c1, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_c,
            fork=True,
            alias=f"FAN-C1-{tag}",
            launch_token=f"C-LAUNCHED-C1-{tag}",
            lineage_token=f"FAN-LINEAGE-C1-{tag}",
            timeout=240.0,
        )
        thread_c2, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_c,
            fork=False,
            alias=f"FAN-C2-{tag}",
            launch_token=f"C-LAUNCHED-C2-{tag}",
            lineage_token=f"FAN-LINEAGE-C2-{tag}",
            timeout=240.0,
        )

        sender_threads = [
            root_thread_id,
            thread_a,
            thread_b,
            thread_c,
            thread_a1,
            thread_a2,
            thread_b1,
            thread_b2,
            thread_c1,
            thread_c2,
        ]
        fan_in_tokens = [f"FANIN-TOKEN-{tag}-{index:02d}" for index in range(1, len(sender_threads) + 1)]
        await asyncio.gather(
            *[
                _send_inbox_message_and_wait_ack(
                    live_tg_forum,
                    sender_thread_id=sender_thread_id,
                    recipient=lineage_target["agent_name"],
                    content=token,
                    ack_token=f"FANIN-SENT-{tag}-{index:02d}",
                    summary="fan-in",
                    timeout=240.0,
                )
                for index, (sender_thread_id, token) in enumerate(zip(sender_threads, fan_in_tokens, strict=True), start=1)
            ]
        )

        wake_target = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=thread_target,
            token="teammate message received",
            timeout=480.0,
        )
        assert "teammate message received" in wake_target.text.lower(), live_tg_forum.failure_context()
        summary_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=thread_target,
            token="\"count\":",
            timeout=420.0,
        )
        payload = _extract_json_object(summary_message.text)
        assert payload["count"] == len(fan_in_tokens), live_tg_forum.failure_context()
        assert payload["tokens"] == sorted(fan_in_tokens), live_tg_forum.failure_context()

    async def test_live_smoke_unrun_fork_child_wakes_and_follows_inbox_instruction(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_title = f"Smoke Unrun Fork {tag}"
        root_thread_id = await live_tg_forum.platform.create_topic(root_title)

        prime_message = await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only UNRUN-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"UNRUN-PRIME-{tag}",
            timeout=180.0,
        )

        fork_alias = f"UNRUN-CHILD-{tag}"
        baseline_fork = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send_control(
            f"/fork@{live_tg_forum.bot_username} {fork_alias}",
            thread_id=root_thread_id,
            reply_to_message_id=prime_message.message_id,
            timeout=30.0,
        )
        fork_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=baseline_fork,
            token="fork topic created",
            timeout=180.0,
        )
        child_thread_id, _ = _extract_topic_link(fork_message.text)
        child_agent_name = agent_name_for_lineage((root_title, fork_alias))

        sender_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"UNRUN-SENDER-{tag}",
            launch_token=f"ROOT-LAUNCHED-SENDER-{tag}",
            lineage_token=f"UNRUN-LINEAGE-SENDER-{tag}",
            timeout=220.0,
        )

        instruction_token = f"UNRUN-CHILD-OK-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=sender_thread_id,
            recipient=child_agent_name,
            content=f"Reply in your topic with only {instruction_token}.",
            ack_token=f"SENDER-SENT-UNRUN-{tag}",
            summary="wake-unrun",
            timeout=240.0,
        )

        wake_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="topic wake: teammate message received",
            timeout=420.0,
        )
        assert "teammate message received" in wake_message.text.lower(), live_tg_forum.failure_context()
        child_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=instruction_token,
            timeout=420.0,
        )
        assert instruction_token in child_reply.text, live_tg_forum.failure_context()

    async def test_live_smoke_clear_preserves_identity_and_inbox_reachability(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Clear Identity {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only CLEAR-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"CLEAR-PRIME-{tag}",
            timeout=180.0,
        )

        lineage_before = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread_id,
            token=f"CLEAR-LINEAGE-BEFORE-{tag}",
            timeout=240.0,
        )
        sender_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"CLEAR-SENDER-{tag}",
            launch_token=f"CLEAR-LAUNCHED-SENDER-{tag}",
            lineage_token=f"CLEAR-LINEAGE-SENDER-{tag}",
            timeout=220.0,
        )

        clear_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send_control(
            f"/clear@{live_tg_forum.bot_username}",
            thread_id=root_thread_id,
            timeout=40.0,
        )
        clear_confirm = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=clear_baseline,
            token="session cleared; agent identity was kept",
            timeout=180.0,
        )
        assert "agent identity was kept" in clear_confirm.text.lower(), live_tg_forum.failure_context()

        forgot_message = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic smoke test. "
                f"Reply with only NO if you do not remember the exact token CLEAR-PRIME-{tag}, "
                "otherwise reply with only YES."
            ),
            thread_id=root_thread_id,
            token="NO",
            timeout=240.0,
        )
        assert "NO" in forgot_message.text, live_tg_forum.failure_context()

        lineage_after = await _query_session_lineage(
            live_tg_forum,
            thread_id=root_thread_id,
            token=f"CLEAR-LINEAGE-AFTER-{tag}",
            timeout=240.0,
        )
        # After /clear, lineage identity is preserved. Team key and agent name
        # should stay the same. Lineage length is always preserved.
        assert lineage_after["agent_name"] == lineage_before["agent_name"], (
            f"agent_name changed after /clear: {lineage_before['agent_name']} -> {lineage_after['agent_name']}\n"
            + live_tg_forum.failure_context()
        )
        assert lineage_after["lineage_length"] == lineage_before["lineage_length"], (
            f"lineage_length changed after /clear\n" + live_tg_forum.failure_context()
        )
        assert lineage_after["root_team_key"] == lineage_before["root_team_key"], (
            f"team key changed after /clear: {lineage_before['root_team_key']} -> {lineage_after['root_team_key']}\n"
            + live_tg_forum.failure_context()
        )

        inbox_token = f"CLEAR-INBOX-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=sender_thread_id,
            recipient=lineage_before["agent_name"],
            content=inbox_token,
            ack_token=f"CLEAR-SENDER-SENT-{tag}",
            summary="clear-preserve",
            timeout=240.0,
        )
        wake_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            token="topic wake: teammate message received",
            timeout=420.0,
        )
        assert "teammate message received" in wake_message.text.lower(), live_tg_forum.failure_context()

        seen_message = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {inbox_token}, reply with only CLEAR-INBOX-SEEN-{tag}. "
                f"Otherwise reply with only CLEAR-INBOX-MISS-{tag}."
            ),
            thread_id=root_thread_id,
            token=f"CLEAR-INBOX-SEEN-{tag}",
            timeout=300.0,
        )
        assert f"CLEAR-INBOX-SEEN-{tag}" in seen_message.text, live_tg_forum.failure_context()

    async def test_live_smoke_new_replaces_identity_and_old_recipient_becomes_undeliverable(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke New Root {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only NEW-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"NEW-PRIME-{tag}",
            timeout=180.0,
        )

        sender_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"NEW-SENDER-{tag}",
            launch_token=f"NEW-LAUNCHED-SENDER-{tag}",
            lineage_token=f"NEW-LINEAGE-SENDER-{tag}",
            timeout=220.0,
        )
        target_thread_id, target_lineage_before = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"NEW-TARGET-{tag}",
            launch_token=f"NEW-LAUNCHED-TARGET-{tag}",
            lineage_token=f"NEW-LINEAGE-TARGET-{tag}",
            timeout=220.0,
        )

        new_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=target_thread_id)
        await live_tg_forum.platform.send_control(
            f"/new@{live_tg_forum.bot_username} ⚡ Reborn {tag}",
            thread_id=target_thread_id,
            timeout=40.0,
        )
        new_confirm = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=target_thread_id,
            after_message_id=new_baseline,
            token="new trunk session created:",
            timeout=180.0,
        )
        assert "new trunk session created" in new_confirm.text.lower(), live_tg_forum.failure_context()
        assert f"Reborn {tag}" in new_confirm.text, live_tg_forum.failure_context()

        forgot_target = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic smoke test. "
                f"Reply with only NO if you do not remember the exact token NEW-LINEAGE-TARGET-{tag}, "
                "otherwise reply with only YES."
            ),
            thread_id=target_thread_id,
            token="NO",
            timeout=240.0,
        )
        assert "NO" in forgot_target.text, live_tg_forum.failure_context()

        target_lineage_after = await _query_session_lineage(
            live_tg_forum,
            thread_id=target_thread_id,
            token=f"NEW-LINEAGE-AFTER-{tag}",
            timeout=240.0,
        )
        assert int(target_lineage_after["lineage_length"]) == 1, live_tg_forum.failure_context()
        assert (
            target_lineage_after["root_team_key"] != target_lineage_before["root_team_key"]
        ), live_tg_forum.failure_context()
        assert (
            target_lineage_after["agent_name"] != target_lineage_before["agent_name"]
        ), live_tg_forum.failure_context()

        dead_period_token = f"NEW-DEAD-PERIOD-{tag}"
        delivered = await _send_inbox_message_and_expect_outcome(
            live_tg_forum,
            sender_thread_id=sender_thread_id,
            recipient=target_lineage_before["agent_name"],
            content=dead_period_token,
            delivered_token=f"NEW-DEAD-DELIVERED-{tag}",
            undelivered_token=f"NEW-DEAD-UNDELIVERED-{tag}",
            summary="new-undelivered",
            timeout=240.0,
        )
        assert delivered is False, live_tg_forum.failure_context()

        dead_check = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {dead_period_token}, reply with only NEW-DEAD-SEEN-{tag}. "
                f"Otherwise reply with only NEW-DEAD-MISS-{tag}."
            ),
            thread_id=target_thread_id,
            token=f"NEW-DEAD-MISS-{tag}",
            timeout=300.0,
        )
        assert f"NEW-DEAD-MISS-{tag}" in dead_check.text, live_tg_forum.failure_context()

    async def test_live_smoke_delete_then_respawn_same_alias_recovers_backlog_but_not_dead_period_sends(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Respawn {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only RESPAWN-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"RESPAWN-PRIME-{tag}",
            timeout=180.0,
        )

        sender_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"RESPAWN-SENDER-{tag}",
            launch_token=f"RESPAWN-LAUNCHED-SENDER-{tag}",
            lineage_token=f"RESPAWN-LINEAGE-SENDER-{tag}",
            timeout=220.0,
        )
        target_alias = f"RESPAWN-TARGET-{tag}"
        target_thread_id, target_lineage_before = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=target_alias,
            launch_token=f"RESPAWN-LAUNCHED-TARGET-{tag}",
            lineage_token=f"RESPAWN-LINEAGE-TARGET-{tag}",
            timeout=220.0,
        )

        backlog_token = f"RESPAWN-BACKLOG-{tag}"
        _append_unread_inbox_message(
            team_name=target_lineage_before["root_team_key"],
            recipient=target_lineage_before["agent_name"],
            content=backlog_token,
            summary="pre-delete backlog",
            sender="external-live-test",
        )

        await live_tg_forum.platform.send_nowait(
            f"/delete@{live_tg_forum.bot_username}",
            thread_id=target_thread_id,
        )
        await asyncio.sleep(4.0)

        dead_period_token = f"RESPAWN-DEAD-{tag}"
        delivered = await _send_inbox_message_and_expect_outcome(
            live_tg_forum,
            sender_thread_id=sender_thread_id,
            recipient=target_lineage_before["agent_name"],
            content=dead_period_token,
            delivered_token=f"RESPAWN-DEAD-DELIVERED-{tag}",
            undelivered_token=f"RESPAWN-DEAD-UNDELIVERED-{tag}",
            summary="respawn-undelivered",
            timeout=240.0,
        )
        assert delivered is False, live_tg_forum.failure_context()

        reborn_thread_id, target_lineage_after = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=target_alias,
            launch_token=f"RESPAWN-LAUNCHED-R2-{tag}",
            lineage_token=f"RESPAWN-LINEAGE-R2-{tag}",
            timeout=240.0,
        )
        assert target_lineage_after == target_lineage_before, live_tg_forum.failure_context()

        backlog_only_token = f"RESPAWN-BACKLOG-ONLY-{tag}"
        backlog_both_token = f"RESPAWN-BACKLOG-AND-DEAD-{tag}"
        backlog_dead_only_token = f"RESPAWN-DEAD-ONLY-{tag}"
        backlog_none_token = f"RESPAWN-BACKLOG-NONE-{tag}"
        backlog_check_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=reborn_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find {backlog_token!r} and do not find {dead_period_token!r}, reply with only {backlog_only_token}. "
                f"If you find both {backlog_token!r} and {dead_period_token!r}, reply with only {backlog_both_token}. "
                f"If you find only {dead_period_token!r}, reply with only {backlog_dead_only_token}. "
                f"If you find neither, reply with only {backlog_none_token}."
            ),
            thread_id=reborn_thread_id,
            require_done=False,
            timeout=240.0,
        )
        backlog_check_message = await _wait_for_exact_message_after_any_token(
            live_tg_forum,
            thread_id=reborn_thread_id,
            after_message_id=backlog_check_baseline,
            tokens=[
                backlog_only_token,
                backlog_both_token,
                backlog_dead_only_token,
                backlog_none_token,
            ],
            timeout=360.0,
        )
        assert _message_is_exact_token(backlog_check_message.text, backlog_only_token), (
            live_tg_forum.failure_context()
        )

        post_respawn_token = f"RESPAWN-POST-{tag}"
        delivered_post = await _send_inbox_message_and_expect_outcome(
            live_tg_forum,
            sender_thread_id=sender_thread_id,
            recipient=target_lineage_before["agent_name"],
            content=post_respawn_token,
            delivered_token=f"RESPAWN-POST-DELIVERED-{tag}",
            undelivered_token=f"RESPAWN-POST-UNDELIVERED-{tag}",
            summary="respawn-post",
            timeout=240.0,
        )
        assert delivered_post is True, live_tg_forum.failure_context()

        wake_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=reborn_thread_id,
            token="teammate message received",
            timeout=420.0,
        )
        assert "teammate message received" in wake_message.text.lower(), live_tg_forum.failure_context()

        final_ok_token = f"RESPAWN-FINAL-OK-{tag}"
        final_bad_token = f"RESPAWN-FINAL-BAD-{tag}"
        final_check_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=reborn_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=30. "
                f"If you find both {backlog_token!r} and {post_respawn_token!r} and do not find {dead_period_token!r}, "
                f"reply with only {final_ok_token}. "
                f"Otherwise reply with only {final_bad_token}."
            ),
            thread_id=reborn_thread_id,
            require_done=False,
            timeout=240.0,
        )
        final_check_message = await _wait_for_exact_message_after_any_token(
            live_tg_forum,
            thread_id=reborn_thread_id,
            after_message_id=final_check_baseline,
            tokens=[final_ok_token, final_bad_token],
            timeout=360.0,
        )
        assert _message_is_exact_token(final_check_message.text, final_ok_token), (
            live_tg_forum.failure_context()
        )

    async def test_live_smoke_inline_reply_fork_preserves_lineage_during_head_switch(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_title = f"Smoke Inline Lineage {tag}"
        thread_id = await live_tg_forum.platform.create_topic(root_title)

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only INLINE-PRIME-{tag}.",
            thread_id=thread_id,
            token=f"INLINE-PRIME-{tag}",
            timeout=180.0,
        )
        sender_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=thread_id,
            fork=False,
            alias=f"INLINE-SENDER-{tag}",
            launch_token=f"INLINE-LAUNCHED-SENDER-{tag}",
            lineage_token=f"INLINE-LINEAGE-SENDER-{tag}",
            timeout=220.0,
        )

        lineage_before = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=thread_id,
            timeout=240.0,
        )

        base_a = await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only INLINE-BASE-A-{tag}.",
            thread_id=thread_id,
            token=f"INLINE-BASE-A-{tag}",
            timeout=180.0,
        )
        base_b = await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only INLINE-BASE-B-{tag}.",
            thread_id=thread_id,
            token=f"INLINE-BASE-B-{tag}",
            timeout=180.0,
        )

        race_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=thread_id)
        inbox_token = f"INLINE-INBOX-{tag}"
        send_inbox = asyncio.create_task(
            _send_inbox_message_and_wait_ack(
                live_tg_forum,
                sender_thread_id=sender_thread_id,
                recipient=str(lineage_before["agent_name"]),
                content=inbox_token,
                ack_token=f"INLINE-SENDER-SENT-{tag}",
                summary="inline-race",
                timeout=240.0,
            )
        )
        await asyncio.gather(
            live_tg_forum.platform.send_nowait(
                (
                    "This is a deterministic inline-fork smoke test. "
                    f"Reply with only INLINE-RACE-A-{tag}."
                ),
                thread_id=thread_id,
                reply_to_message_id=base_a.message_id,
            ),
            live_tg_forum.platform.send_nowait(
                (
                    "This is a deterministic inline-fork smoke test. "
                    f"Reply with only INLINE-RACE-B-{tag}."
                ),
                thread_id=thread_id,
                reply_to_message_id=base_b.message_id,
            ),
            send_inbox,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=race_baseline,
            token=f"INLINE-RACE-A-{tag}",
            timeout=420.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=thread_id,
            after_message_id=race_baseline,
            token=f"INLINE-RACE-B-{tag}",
            timeout=420.0,
        )

        lineage_after = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=thread_id,
            timeout=240.0,
        )
        assert lineage_after["lineage"] == lineage_before["lineage"], live_tg_forum.failure_context()
        assert (
            lineage_after["agent_name"] == lineage_before["agent_name"]
        ), live_tg_forum.failure_context()
        assert lineage_after["root_team_key"] == lineage_before["root_team_key"], live_tg_forum.failure_context()
        assert lineage_after["origin"] == "inline_fork", live_tg_forum.failure_context()
        assert lineage_after["session_id"] != lineage_before["session_id"], live_tg_forum.failure_context()

        inbox_seen = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic inline-fork smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {inbox_token}, reply with only INLINE-INBOX-SEEN-{tag}. "
                f"Otherwise reply with only INLINE-INBOX-MISS-{tag}."
            ),
            thread_id=thread_id,
            token=f"INLINE-INBOX-SEEN-{tag}",
            timeout=300.0,
        )
        assert f"INLINE-INBOX-SEEN-{tag}" in inbox_seen.text, live_tg_forum.failure_context()

    async def test_live_smoke_hierarchy_schedules_and_restart_preserve_lineage(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Restart Sched {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only RESTART-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"RESTART-PRIME-{tag}",
            timeout=180.0,
        )

        child_thread_id, _ = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"RESTART-CHILD-{tag}",
            launch_token=f"RESTART-LAUNCHED-CHILD-{tag}",
            lineage_token=f"RESTART-LINEAGE-CHILD-{tag}",
            timeout=220.0,
        )
        grandchild_thread_id, grandchild_lineage = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=child_thread_id,
            fork=False,
            alias=f"RESTART-GRAND-{tag}",
            launch_token=f"RESTART-LAUNCHED-GRAND-{tag}",
            lineage_token=f"RESTART-LINEAGE-GRAND-{tag}",
            timeout=240.0,
        )

        root_schedule_token = f"ROOT-SCHED-{tag}"
        root_schedule_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic lineage schedule smoke test. "
                "Call CronCreate exactly once with "
                "schedule_mode='interval', cron='* * * * *', interval_seconds=55, reset_session=false, max_runs=2, "
                f"description='ROOT-SCHED-{tag}', "
                f"prompt='This is a deterministic lineage schedule smoke test. Reply with only {root_schedule_token}.' "
                f"After the tool call, reply with only ROOT-SCHED-CREATED-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=240.0,
        )
        root_created = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=root_schedule_baseline,
            token=f"ROOT-SCHED-CREATED-{tag}",
            timeout=300.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=root_created.message_id,
            token=root_schedule_token,
            timeout=180.0,
        )

        pre_restart_inbox_token = f"PRE-RESTART-INBOX-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread_id,
            recipient=grandchild_lineage["agent_name"],
            content=pre_restart_inbox_token,
            ack_token=f"PRE-RESTART-SENT-{tag}",
            summary="pre-restart-direct",
            timeout=240.0,
        )
        grandchild_pre_restart = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic lineage schedule smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=20. "
                f"If you find a message whose exact text is {pre_restart_inbox_token}, reply with only PRE-RESTART-INBOX-SEEN-{tag}. "
                f"Otherwise reply with only PRE-RESTART-INBOX-MISS-{tag}."
            ),
            thread_id=grandchild_thread_id,
            token=f"PRE-RESTART-INBOX-SEEN-{tag}",
            timeout=300.0,
        )
        assert f"PRE-RESTART-INBOX-SEEN-{tag}" in grandchild_pre_restart.text, live_tg_forum.failure_context()

        lineage_before = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            timeout=240.0,
        )

        _stop_bot(live_tg_forum.proc)
        assert live_tg_forum.temp_root is not None
        live_tg_forum.proc, live_tg_forum.log_file = _start_bot(
            live_tg_forum.vault_path,
            live_tg_forum.temp_root,
            state_db_path=live_tg_forum.state_db_path,
        )
        root_post_restart_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=root_post_restart_baseline,
            token=root_schedule_token,
            timeout=180.0,
        )

        lineage_after = await _query_session_lineage_payload(
            live_tg_forum,
            thread_id=grandchild_thread_id,
            timeout=240.0,
        )
        assert lineage_after["lineage"] == lineage_before["lineage"], live_tg_forum.failure_context()
        assert (
            lineage_after["agent_name"] == lineage_before["agent_name"]
        ), live_tg_forum.failure_context()
        assert lineage_after["root_team_key"] == lineage_before["root_team_key"], live_tg_forum.failure_context()

        restart_inbox_token = f"RESTART-INBOX-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread_id,
            recipient=grandchild_lineage["agent_name"],
            content=restart_inbox_token,
            ack_token=f"RESTART-SENT-INBOX-{tag}",
            summary="restart-post",
            timeout=240.0,
        )
        grandchild_post_restart = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic lineage schedule smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=30. "
                f"If you find a message whose exact text is {restart_inbox_token}, reply with only RESTART-INBOX-SEEN-{tag}. "
                f"Otherwise reply with only RESTART-INBOX-MISS-{tag}."
            ),
            thread_id=grandchild_thread_id,
            token=f"RESTART-INBOX-SEEN-{tag}",
            timeout=300.0,
        )
        assert f"RESTART-INBOX-SEEN-{tag}" in grandchild_post_restart.text, live_tg_forum.failure_context()

    async def test_live_smoke_rename_stop_resume_preserves_lineage_and_inbox(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        original_root_title = f"Smoke Rename Lifecycle {tag}"
        root_thread_id = await live_tg_forum.platform.create_topic(original_root_title)

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only RENAME-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"RENAME-PRIME-{tag}",
            timeout=180.0,
        )

        await live_tg_forum.platform.rename_topic(root_thread_id, f"Visible Root Renamed {tag}")
        await asyncio.sleep(3.0)

        child_alias = f"RENAME-TARGET-{tag}"
        launch_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic rename-lifecycle smoke test. "
                f"Use AgentTask exactly once with fork=false, run_in_background=true, alias={child_alias}, and prompt "
                f"'Use Bash to run sleep 180 and then reply with only RENAME-LATE-{tag}.' "
                f"After launching, reply with only RENAME-LAUNCHED-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=launch_baseline,
            token=f"RENAME-LAUNCHED-{tag}",
            timeout=300.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=launch_baseline,
            token="agent task launched",
            timeout=300.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)
        child_launch = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="agentId:",
            timeout=240.0,
        )
        handle = _extract_agent_id(child_launch.text)

        await live_tg_forum.platform.rename_topic(child_thread_id, f"Visible Child Renamed {tag}")
        await asyncio.sleep(3.0)

        stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic rename-lifecycle smoke test. "
                f"Call AgentTaskStop exactly once with task_id={handle}. "
                f"Reply with only RENAME-STOP-SENT-{tag} if it succeeds, otherwise RENAME-STOP-NOP-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=120.0,
        )
        stop_result = await _wait_for_message_after_any_token(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=stop_baseline,
            tokens=[f"RENAME-STOP-SENT-{tag}", f"RENAME-STOP-NOP-{tag}"],
            timeout=240.0,
        )
        assert (
            f"RENAME-STOP-SENT-{tag}" in stop_result.text
            or f"RENAME-STOP-NOP-{tag}" in stop_result.text
        ), live_tg_forum.failure_context()

        resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=root_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic rename-lifecycle smoke test. "
                f"Resume the existing AgentTask handle {handle} by calling AgentTask once with resume={handle}, "
                f"fork=false, run_in_background=true, alias={child_alias}, and prompt "
                f"'Reply with only RENAME-RESUME-DONE-{tag}.' "
                f"After launching resumed work, reply with only RENAME-RESUMED-{tag}."
            ),
            thread_id=root_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=root_thread_id,
            after_message_id=resume_baseline,
            token=f"RENAME-RESUMED-{tag}",
            timeout=300.0,
        )
        await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"RENAME-RESUME-DONE-{tag}",
            timeout=300.0,
        )

        expected_agent_name = agent_name_for_lineage((original_root_title, child_alias))
        renamed_agent_name = agent_name_for_lineage((f"Visible Root Renamed {tag}", child_alias))
        if renamed_agent_name != expected_agent_name:
            delivered_wrong = await _send_inbox_message_and_expect_outcome(
                live_tg_forum,
                sender_thread_id=root_thread_id,
                recipient=renamed_agent_name,
                content=f"RENAME-WRONG-INBOX-{tag}",
                delivered_token=f"RENAME-WRONG-DELIVERED-{tag}",
                undelivered_token=f"RENAME-WRONG-UNDELIVERED-{tag}",
                summary="rename-wrong-name",
                timeout=240.0,
            )
            # Always-deliver: message goes to inbox file regardless.
            # The agent may not read it (wrong inbox key) but delivery succeeds.
            assert delivered_wrong is True, live_tg_forum.failure_context()

        inbox_token = f"RENAME-INBOX-WOKE-{tag}"
        await _send_inbox_message_and_wait_ack(
            live_tg_forum,
            sender_thread_id=root_thread_id,
            recipient=expected_agent_name,
            content=(
                "This is a deterministic teammate instruction after rename and resume. "
                f"Reply with only {inbox_token}."
            ),
            ack_token=f"RENAME-SENDER-SENT-{tag}",
            summary="rename-resume",
            timeout=240.0,
        )
        inbox_check = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=inbox_token,
            timeout=420.0,
        )
        assert inbox_token in inbox_check.text, live_tg_forum.failure_context()

    async def test_live_smoke_fan_in_then_new_rejects_late_sends(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        root_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Terminal FanIn {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only TERMINAL-PRIME-{tag}.",
            thread_id=root_thread_id,
            token=f"TERMINAL-PRIME-{tag}",
            timeout=180.0,
        )

        target_thread_id, target_lineage_before = await _launch_lineage_worker(
            live_tg_forum,
            launcher_thread_id=root_thread_id,
            fork=False,
            alias=f"TERMINAL-TARGET-{tag}",
            launch_token=f"TERMINAL-LAUNCHED-TARGET-{tag}",
            lineage_token=f"TERMINAL-LINEAGE-TARGET-{tag}",
            timeout=220.0,
        )
        sender_threads = [root_thread_id]
        for index in range(1, 10):
            sender_thread_id, _ = await _launch_lineage_worker(
                live_tg_forum,
                launcher_thread_id=root_thread_id,
                fork=False,
                alias=f"TERMINAL-S{index}-{tag}",
                launch_token=f"TERMINAL-LAUNCHED-S{index}-{tag}",
                lineage_token=f"TERMINAL-LINEAGE-S{index}-{tag}",
                timeout=220.0,
            )
            sender_threads.append(sender_thread_id)

        protocol_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=target_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic terminal fan-in smoke test protocol. "
                f"You will receive teammate inbox messages whose exact text starts with TERMINAL-PRE-{tag}-. "
                "Whenever you are woken because teammate messages may have arrived, call ReadInbox as needed, "
                "track the unique TERMINAL-PRE tokens you have seen across turns, and once you have seen exactly 5 "
                "unique TERMINAL-PRE tokens reply with only a JSON object of the exact form "
                "{\"pre\": [<sorted tokens>]}. "
                f"For now, just confirm with only TERMINAL-PROTOCOL-ACK-{tag}."
            ),
            thread_id=target_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=target_thread_id,
            after_message_id=protocol_baseline,
            token=f"TERMINAL-PROTOCOL-ACK-{tag}",
            timeout=180.0,
        )

        pre_tokens = [f"TERMINAL-PRE-{tag}-{index:02d}" for index in range(1, 6)]
        post_tokens = [f"TERMINAL-POST-{tag}-{index:02d}" for index in range(6, 11)]
        await asyncio.gather(
            *[
                _send_inbox_message_and_wait_ack(
                    live_tg_forum,
                    sender_thread_id=sender_thread_id,
                    recipient=target_lineage_before["agent_name"],
                    content=token,
                    ack_token=f"TERMINAL-PRE-SENT-{tag}-{index:02d}",
                    summary="terminal-pre",
                    timeout=240.0,
                )
                for index, (sender_thread_id, token) in enumerate(
                    zip(sender_threads[:5], pre_tokens, strict=True),
                    start=1,
                )
            ]
        )
        pre_summary = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=target_thread_id,
            token="\"pre\":",
            timeout=480.0,
        )
        pre_payload = _extract_json_object(pre_summary.text)
        assert pre_payload["pre"] == sorted(pre_tokens), live_tg_forum.failure_context()

        new_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=target_thread_id)
        await live_tg_forum.platform.send_control(
            f"/new@{live_tg_forum.bot_username} ⚡ Terminal Reborn {tag}",
            thread_id=target_thread_id,
            timeout=40.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=target_thread_id,
            after_message_id=new_baseline,
            token="new trunk session created:",
            timeout=180.0,
        )

        delivered_results = await asyncio.gather(
            *[
                _send_inbox_message_and_expect_outcome(
                    live_tg_forum,
                    sender_thread_id=sender_thread_id,
                    recipient=target_lineage_before["agent_name"],
                    content=token,
                    delivered_token=f"TERMINAL-POST-DELIVERED-{tag}-{index:02d}",
                    undelivered_token=f"TERMINAL-POST-UNDELIVERED-{tag}-{index:02d}",
                    summary="terminal-post",
                    timeout=240.0,
                )
                for index, (sender_thread_id, token) in enumerate(
                    zip(sender_threads[5:], post_tokens, strict=True),
                    start=6,
                )
            ]
        )
        assert delivered_results == [False] * len(post_tokens), live_tg_forum.failure_context()

        final_check_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=target_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic terminal fan-in smoke test. "
                "Call ReadInbox exactly once with include_read=true, mark_read=false, limit=40. "
                f"Collect every inbox text starting with TERMINAL-PRE-{tag}- or TERMINAL-POST-{tag}-. "
                "Reply with only a JSON object of the exact form {\"texts\": [<sorted texts>]}. "
                "Sort the texts lexicographically."
            ),
            thread_id=target_thread_id,
            require_done=False,
            timeout=240.0,
        )
        final_check_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=target_thread_id,
            after_message_id=final_check_baseline,
            token="\"texts\":",
            timeout=360.0,
        )
        final_payload = _extract_json_object(final_check_message.text)
        assert final_payload["texts"] == [], live_tg_forum.failure_context()
