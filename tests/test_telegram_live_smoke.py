"""Dense live Telegram smoke scenarios for fast dev-loop confidence.

These tests intentionally pack multiple behaviors into 2 long scenarios so day-to-day
validation is substantially faster than running the full granular suite.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.evals.platform_telegram_forum import TelegramForumPlatform
from tests.test_telegram_live_forum_topics import (
    _extract_agent_id,
    _extract_json_object,
    _extract_topic_link,
    _reset_general,
    _send_and_wait_for_token,
    _session_id_for_route,
    _wait_for_message_after_containing,
    _wait_for_message_containing,
    _LiveForumHarness,
    live_tg_forum,  # fixture import
)


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

        stop_probe = await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Call AgentTaskStop exactly once with task_id={handle}. "
                f"Reply with only CORE-STOP-SENT-{tag} if it succeeds, otherwise CORE-STOP-NOP-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=120.0,
        )
        assert (
            f"CORE-STOP-SENT-{tag}" in stop_probe.output
            or f"CORE-STOP-NOP-{tag}" in stop_probe.output
        ), live_tg_forum.failure_context()
        if f"CORE-STOP-SENT-{tag}" in stop_probe.output:
            stopped_child = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token="fork task stopped",
                timeout=180.0,
            )
            assert "fork task stopped" in stopped_child.text.lower(), live_tg_forum.failure_context()

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
                    message.message_id > stop_baseline and token in message.text
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
                    and f"COLLIDE-{tag}-F1" in msg.text
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
                f"Then call SendInboxMessage with team_name={team_name}, recipient={worker_b}, "
                f"content={inbox_token}, summary=\"handoff\", sender={worker_a}. "
                f"After all tool calls succeed, reply with only WORKER-A-DONE-{tag}.' "
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
        worker_a_done = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            token=f"WORKER-A-DONE-{tag}",
            timeout=420.0,
        )
        assert f"WORKER-A-DONE-{tag}" in worker_a_done.text, live_tg_forum.failure_context()

        baseline_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test for team workers. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_b}, description TEAM-B-{tag}, and prompt "
                "'This is a deterministic team-worker verification task. "
                f"Call TaskList and verify {task_subject} exists. "
                f"Then call ReadInbox with team_name={team_name}, agent={worker_b}, "
                f"include_read=false, mark_read=true, limit=20 and verify {inbox_token} exists. "
                f"If both checks pass, reply with only WORKER-B-OK-{tag}. "
                f"Otherwise reply with only WORKER-B-FAIL-{tag}.' "
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
        worker_b_done = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_b_thread,
            token=f"WORKER-B-OK-{tag}",
            timeout=420.0,
        )
        assert f"WORKER-B-OK-{tag}" in worker_b_done.text, live_tg_forum.failure_context()

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
        worker_c_done = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_c_thread,
            token=f"WORKER-C-OK-{tag}",
            timeout=420.0,
        )
        assert f"WORKER-C-OK-{tag}" in worker_c_done.text, live_tg_forum.failure_context()

        session_b = await _session_id_for_route(live_tg_forum, thread_id=worker_b_thread)
        session_c = await _session_id_for_route(live_tg_forum, thread_id=worker_c_thread)
        assert session_b != session_c, live_tg_forum.failure_context()
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
