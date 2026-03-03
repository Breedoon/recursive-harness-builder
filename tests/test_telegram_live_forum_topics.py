"""Live Telegram forum-topic integration tests against a real forum group."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from tests.evals.platform_telegram_forum import (
    TelegramForumObservedMessage,
    TelegramForumPlatform,
    TelegramForumResponseTrace,
)


_REQUIRED_ENV = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "TELEGRAM_TEST_BOT_USERNAME",
    "OBS_TELEGRAM_TEST_BOT_TOKEN",
    "OBS_TELEGRAM_LIVE_FORUM_CHAT_ID",
]

_SESSION_ID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_TOPIC_LINK_RE = re.compile(r"https://t\.me/c/\d+/(\d+)/(\d+)")


def _has_telegram_credentials() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


def _read_log_tail(log_file: Path) -> str:
    if not log_file.exists():
        return ""
    text = log_file.read_text(errors="replace")
    return text[-12000:]


def _copy_vault(source: Path, destination: Path) -> Path:
    vault = destination / "vault"
    shutil.copytree(source, vault, symlinks=True)
    return vault


def _start_bot(vault_path: Path, temp_root: Path) -> tuple[subprocess.Popen, Path]:
    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    env["OBS_TELEGRAM_BOT_TOKEN"] = os.environ["OBS_TELEGRAM_TEST_BOT_TOKEN"]
    env["OBS_TELEGRAM_ALLOWED_USERS"] = os.environ.get("TELEGRAM_TEST_USER_ID", "5129431382")
    env["OBS_TELEGRAM_TEMP_ROOT"] = str(temp_root)

    log_file = Path(tempfile.mktemp(prefix="obs_tg_forum_", suffix=".log"))
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main"],
        env=env,
        stdout=log_fh,
        stderr=log_fh,
    )
    time.sleep(5)
    if proc.poll() is not None:
        log_fh.close()
        raise RuntimeError(
            f"Telegram bot exited during startup (rc={proc.returncode}).\n{_read_log_tail(log_file)}"
        )
    return proc, log_file


def _stop_bot(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _extract_session_id(text: str) -> str:
    match = _SESSION_ID_RE.search(text)
    assert match, f"session_id missing in:\n{text}"
    return match.group(1)


def _extract_topic_link(text: str) -> tuple[int, int]:
    match = _TOPIC_LINK_RE.search(text)
    assert match, f"topic link missing in:\n{text}"
    return int(match.group(1)), int(match.group(2))


def _message_containing(
    trace: TelegramForumResponseTrace,
    token: str,
) -> TelegramForumObservedMessage:
    for message in trace.messages:
        if token in message.text:
            return message
    raise AssertionError(
        f"Could not find message containing {token!r}\n"
        f"output={trace.output}\n"
        f"messages={trace.messages}"
    )


def _build_busy_files(vault_path: Path, count: int = 48) -> None:
    busy_dir = vault_path / "busy-topic-test"
    busy_dir.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        path = busy_dir / f"busy-{index:02d}.md"
        path.write_text(
            (
                f"# Busy file {index}\n\n"
                f"Deterministic topic test content {index}.\n"
                "Read this file individually.\n"
            ),
            encoding="utf-8",
        )


@dataclass
class _LiveForumHarness:
    platform: TelegramForumPlatform
    proc: subprocess.Popen
    log_file: Path
    vault_path: Path
    bot_username: str

    def failure_context(self) -> str:
        return (
            "\nRecent forum messages:\n"
            f"{self.platform.format_recent_messages()}\n\n"
            "Bot log tail:\n"
            f"{_read_log_tail(self.log_file)}"
        )


async def _warm_platform(harness: _LiveForumHarness) -> None:
    clear_cmd = f"/clear@{harness.bot_username} all"
    for _ in range(6):
        trace = await harness.platform.send_control(clear_cmd, timeout=25.0)
        if "cleared" in trace.output.lower():
            await asyncio.sleep(1.0)
            return
        await asyncio.sleep(1.0)
    raise AssertionError("Forum bot did not respond to warmup /clear all")


async def _reset_general(harness: _LiveForumHarness) -> None:
    trace = await harness.platform.send_control(
        f"/clear@{harness.bot_username} all",
        timeout=25.0,
    )
    assert "cleared" in trace.output.lower(), trace.output + harness.failure_context()
    await asyncio.sleep(1.0)


async def _session_id_for_route(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
) -> str:
    trace = await harness.platform.send(
        (
            "This is a deterministic integration test. "
            "Use the session_info tool and reply with only the session_id UUID."
        ),
        thread_id=thread_id,
    )
    return _extract_session_id(trace.output)


@pytest_asyncio.fixture
async def live_tg_forum(eval_vault: Path, tmp_path: Path) -> _LiveForumHarness:
    if not _has_telegram_credentials():
        pytest.skip("Telegram forum credentials not configured in environment")

    vault_path = _copy_vault(eval_vault, tmp_path)
    temp_root = tmp_path / "obs-agent-temp"
    proc, log_file = _start_bot(vault_path, temp_root)
    platform = TelegramForumPlatform()
    harness = _LiveForumHarness(
        platform=platform,
        proc=proc,
        log_file=log_file,
        vault_path=vault_path,
        bot_username=os.environ["TELEGRAM_TEST_BOT_USERNAME"],
    )
    await platform.connect()
    try:
        await _warm_platform(harness)
        yield harness
    finally:
        await platform.close()
        _stop_bot(proc)


@pytest.mark.integration
@pytest.mark.telegram
class TestTelegramLiveForumTopics:
    async def test_live_general_and_topic_routes_are_isolated(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Isolation {tag}")

        general_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only GENERAL-{tag}.",
        )
        topic_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only TOPIC-{tag}.",
            thread_id=thread_id,
        )

        assert f"GENERAL-{tag}" in general_trace.output, live_tg_forum.failure_context()
        assert f"TOPIC-{tag}" in topic_trace.output, live_tg_forum.failure_context()

        general_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token TOPIC-{tag} anywhere "
                "in our conversation, otherwise NO."
            ),
        )
        topic_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token GENERAL-{tag} anywhere "
                "in our conversation, otherwise NO."
            ),
            thread_id=thread_id,
        )

        assert "NO" in general_visibility.output, live_tg_forum.failure_context()
        assert "NO" in topic_visibility.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_command_creates_child_topic_and_keeps_parent_session(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        alpha_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only ALPHA-{tag}.",
        )
        assert f"ALPHA-{tag}" in alpha_trace.output, live_tg_forum.failure_context()
        general_session_before = await _session_id_for_route(live_tg_forum, thread_id=None)

        fork_trace = await live_tg_forum.platform.send_control(
            f"/fork@{live_tg_forum.bot_username} Fork-{tag}",
            timeout=30.0,
        )
        child_thread_id, service_message_id = _extract_topic_link(fork_trace.output)
        child_recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=child_thread_id,
            limit=6,
        )

        assert any("fork created" in message.text.lower() for message in child_recent), (
            fork_trace.output + live_tg_forum.failure_context()
        )
        assert any(str(service_message_id) == str(message.message_id) for message in child_recent), (
            fork_trace.output + live_tg_forum.failure_context()
        )

        child_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token ALPHA-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=child_thread_id,
        )
        child_session = await _session_id_for_route(live_tg_forum, thread_id=child_thread_id)
        general_session_after = await _session_id_for_route(live_tg_forum, thread_id=None)

        assert "YES" in child_visibility.output, live_tg_forum.failure_context()
        assert child_session != general_session_before, live_tg_forum.failure_context()
        assert general_session_after == general_session_before, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_inline_reply_fork_stays_in_same_topic_and_plain_followup_uses_it(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Inline Fork {tag}")

        alpha_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only ALPHA-{tag}.",
            thread_id=thread_id,
        )
        alpha_message_id = _message_containing(alpha_trace, f"ALPHA-{tag}").message_id
        session_before = await _session_id_for_route(live_tg_forum, thread_id=thread_id)

        beta_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only BETA-{tag}.",
            thread_id=thread_id,
        )
        assert f"BETA-{tag}" in beta_trace.output, live_tg_forum.failure_context()

        reply_fork_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token BETA-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=thread_id,
            reply_to_message_id=alpha_message_id,
        )
        session_after = await _session_id_for_route(live_tg_forum, thread_id=thread_id)
        plain_followup = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token BETA-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=thread_id,
        )

        assert "NO" in reply_fork_trace.output, live_tg_forum.failure_context()
        assert session_after != session_before, live_tg_forum.failure_context()
        assert "NO" in plain_followup.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_topic_clear_delete_and_other_topics_survive(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        topic_a = await live_tg_forum.platform.create_topic(f"Clear A {tag}")
        topic_b = await live_tg_forum.platform.create_topic(f"Clear B {tag}")

        await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only A-{tag}.",
            thread_id=topic_a,
        )
        await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only B-{tag}.",
            thread_id=topic_b,
        )

        clear_trace = await live_tg_forum.platform.send_control(
            f"/clear@{live_tg_forum.bot_username}",
            thread_id=topic_a,
            timeout=25.0,
        )
        assert "session cleared" in clear_trace.output.lower(), live_tg_forum.failure_context()

        topic_a_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token A-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=topic_a,
        )
        topic_b_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token B-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=topic_b,
        )

        await live_tg_forum.platform.send_nowait(
            f"/delete@{live_tg_forum.bot_username}",
            thread_id=topic_a,
        )
        await asyncio.sleep(4.0)
        topic_b_alive = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only ALIVE-{tag}.",
            thread_id=topic_b,
        )

        assert "NO" in topic_a_visibility.output, live_tg_forum.failure_context()
        assert "YES" in topic_b_visibility.output, live_tg_forum.failure_context()
        assert f"ALIVE-{tag}" in topic_b_alive.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_stop_pauses_auto_resume_until_new_message(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        _build_busy_files(live_tg_forum.vault_path)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Stop {tag}")

        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic integration test. "
                "Read every markdown file under busy-topic-test and then reply with only "
                f"FINISHED-{tag}."
            ),
            thread_id=thread_id,
        )
        await asyncio.sleep(1.0)

        queued_token = f"QUEUED-{tag}"
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic integration test. "
                f"Remember the exact token {queued_token}."
            ),
            thread_id=thread_id,
        )
        await asyncio.sleep(1.0)

        stop_trace = await live_tg_forum.platform.send_control(
            f"/stop@{live_tg_forum.bot_username}",
            thread_id=thread_id,
            timeout=25.0,
        )
        assert "interrupt sent" in stop_trace.output.lower(), live_tg_forum.failure_context()

        await live_tg_forum.platform.wait_for_prompt(thread_id=thread_id, timeout=180.0)
        await live_tg_forum.platform.wait_for_silence(thread_id=thread_id, seconds=8.0)

        resume_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token {queued_token} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=thread_id,
        )

        assert "YES" in resume_trace.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_external_topic_delete_while_busy_does_not_crash_daemon(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        _build_busy_files(live_tg_forum.vault_path, count=64)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Crash {tag}")

        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic integration test. "
                "Read every markdown file under busy-topic-test and then reply with only "
                f"CRASH-{tag}."
            ),
            thread_id=thread_id,
        )
        await asyncio.sleep(1.0)
        await live_tg_forum.platform.delete_topic(thread_id)
        await asyncio.sleep(8.0)

        general_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only SURVIVED-{tag}.",
        )

        assert f"SURVIVED-{tag}" in general_trace.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_round_robin_topics_keep_sessions_distinct(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_ids = [
            await live_tg_forum.platform.create_topic(f"Round {tag} {index}")
            for index in range(4)
        ]
        session_ids: list[str] = []

        for index, thread_id in enumerate(thread_ids):
            marker = f"ROUND-{tag}-{index}"
            trace = await live_tg_forum.platform.send(
                f"This is a deterministic integration test. Reply with only {marker}.",
                thread_id=thread_id,
            )
            assert marker in trace.output, live_tg_forum.failure_context()
            session_ids.append(
                await _session_id_for_route(live_tg_forum, thread_id=thread_id)
            )

        assert len(set(session_ids)) == len(thread_ids), live_tg_forum.failure_context()

        for index, thread_id in enumerate(thread_ids):
            foreign_index = (index + 1) % len(thread_ids)
            foreign_marker = f"ROUND-{tag}-{foreign_index}"
            visibility = await live_tg_forum.platform.send(
                (
                    "This is a deterministic integration test. "
                    f"Reply with only YES if you saw the exact token {foreign_marker} "
                    "anywhere in our conversation, otherwise NO."
                ),
                thread_id=thread_id,
            )
            assert "NO" in visibility.output, live_tg_forum.failure_context()

        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_delete_all_from_topic_confirms_in_general_and_removes_topics(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        topic_a = await live_tg_forum.platform.create_topic(f"DeleteAll A {tag}")
        topic_b = await live_tg_forum.platform.create_topic(f"DeleteAll B {tag}")
        general_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send_nowait(
            f"/delete@{live_tg_forum.bot_username} all",
            thread_id=topic_a,
        )
        general_trace = await live_tg_forum.platform.wait_for_prompt_after(
            after_message_id=general_baseline,
            thread_id=None,
            timeout=25.0,
            require_done=False,
        )

        assert "all non-general topics deleted" in general_trace.output.lower(), (
            live_tg_forum.failure_context()
        )
        assert await live_tg_forum.platform.get_recent_messages(thread_id=topic_a, limit=5) == [], (
            live_tg_forum.failure_context()
        )
        assert await live_tg_forum.platform.get_recent_messages(thread_id=topic_b, limit=5) == [], (
            live_tg_forum.failure_context()
        )
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()
