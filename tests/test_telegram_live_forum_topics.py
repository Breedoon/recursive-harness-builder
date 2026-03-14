"""Live Telegram forum-topic integration tests against a real forum group."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
from tests.live_test_vault import ensure_live_test_vault


_REQUIRED_ENV = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "TELEGRAM_TEST_BOT_USERNAME",
    "OBS_TELEGRAM_TEST_BOT_TOKEN",
]

_SESSION_ID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f-]+)", re.IGNORECASE)
_TOPIC_LINK_RE = re.compile(r"https://t\.me/c/\d+/(\d+)/(\d+)")
_MESSAGE_LINK_RE = re.compile(r"https://t\.me/c/\d+/(?:\d+/)?\d+")
_CACHED_FORUM_CHAT_ID: int | None = None


def _has_telegram_credentials() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


def _read_log_tail(log_file: Path) -> str:
    if not log_file.exists():
        return ""
    text = log_file.read_text(errors="replace")
    return text[-12000:]


def _resolve_allowed_users() -> str:
    candidates: list[str] = []
    for key in (
        "OBS_TELEGRAM_ALLOWED_USERS",
        "OBS_TELEGRAM_AUTHORIZED_USER_ID",
        "TELEGRAM_TEST_USER_ID",
        "TELEGRAM_USER_ID",
    ):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        for part in raw.split(","):
            token = part.strip()
            if token and token not in candidates:
                candidates.append(token)
    if not candidates:
        candidates.append("5129431382")
    return ",".join(candidates)


def _resolve_sender_tokens() -> list[str]:
    primary = os.environ["OBS_TELEGRAM_TEST_BOT_TOKEN"].strip()
    tokens = [primary]
    for key in ("OBS_TELEGRAM_TEST_BOT_TOKEN_2", "OBS_TELEGRAM_TEST_SECOND_BOT_TOKEN"):
        extra = (os.environ.get(key) or "").strip()
        if extra and extra not in tokens:
            tokens.append(extra)
    return tokens


def _kill_existing_daemons_enabled() -> bool:
    raw = (os.environ.get("OBS_TELEGRAM_TEST_KILL_EXISTING_DAEMONS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _kill_worktree_telegram_daemons(worktree_root: Path) -> None:
    pgrep = subprocess.run(
        ["pgrep", "-f", "obs_agent\\.telegram_main"],
        check=False,
        capture_output=True,
        text=True,
    )
    if pgrep.returncode not in (0, 1):
        return
    root = str(worktree_root.resolve())
    for token in pgrep.stdout.split():
        if not token.isdigit():
            continue
        pid = int(token)
        if pid == os.getpid():
            continue
        cwd_probe = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
        )
        cwd = ""
        for line in cwd_probe.stdout.splitlines():
            if line.startswith("n"):
                cwd = line[1:].strip()
                break
        if not cwd:
            continue
        try:
            resolved = str(Path(cwd).resolve())
        except Exception:
            continue
        if resolved.startswith(root):
            subprocess.run(["kill", str(pid)], check=False)


def _start_bot(
    vault_path: Path,
    temp_root: Path,
    *,
    state_db_path: Path | None = None,
) -> tuple[subprocess.Popen, Path]:
    if _kill_existing_daemons_enabled():
        worktree_root = Path(__file__).resolve().parents[1]
        _kill_worktree_telegram_daemons(worktree_root)
        time.sleep(0.5)

    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    sender_tokens = _resolve_sender_tokens()
    env["OBS_TELEGRAM_BOT_TOKEN"] = sender_tokens[0]
    env["OBS_TELEGRAM_BOT_TOKENS"] = ",".join(sender_tokens)
    env["OBS_TELEGRAM_ALLOWED_USERS"] = _resolve_allowed_users()
    env["OBS_TELEGRAM_TEMP_ROOT"] = str(temp_root)
    resolved_state_db = state_db_path or (temp_root.parent / "telegram-state.sqlite3")
    env["OBS_TELEGRAM_STATE_DB_PATH"] = str(resolved_state_db)
    env["OBS_TELEGRAM_DROP_PENDING_UPDATES"] = "1"
    # Live tests intentionally overlap parent/child traffic in one forum chat.
    # Use conservative transport pacing so flood control does not dominate the signal.
    env.setdefault("OBS_TELEGRAM_TRANSPORT_BASE_CHAT_INTERVAL_SECONDS", "1.2")
    env.setdefault("OBS_TELEGRAM_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS", "15.0")
    env.setdefault("OBS_TELEGRAM_TYPING_ACTIONS_ENABLED", "0")
    # Native Task* parity harness requires the built-in task tools enabled.
    env.setdefault("CLAUDE_CODE_ENABLE_TASKS", "1")

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


def _extract_agent_id(text: str) -> str:
    match = _AGENT_ID_RE.search(text)
    assert match, f"agentId missing in:\n{text}"
    return match.group(1)


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


def _extract_json_object(text: str) -> dict[str, object]:
    start = text.find("{")
    end = text.rfind("}")
    assert start != -1 and end != -1 and end > start, f"missing JSON object in:\n{text}"
    return json.loads(text[start : end + 1])


async def _wait_for_message_containing(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
    token: str,
    timeout: float = 120.0,
    limit: int = 30,
) -> TelegramForumObservedMessage:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=limit)
        for message in recent:
            if token in message.text:
                return message
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for token {token!r} in thread {thread_id}\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


async def _wait_for_message_after_containing(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
    after_message_id: int,
    token: str,
    timeout: float = 120.0,
    limit: int = 40,
) -> TelegramForumObservedMessage:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=limit)
        for message in recent:
            if message.message_id > after_message_id and token in message.text:
                return message
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for token {token!r} after message {after_message_id} in thread {thread_id}\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


async def _wait_for_report_with_sections(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
    after_message_id: int,
    prefix: str,
    sections: tuple[str, ...],
    timeout: float = 420.0,
    limit: int = 160,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    normalized_sections = tuple(section.lower() for section in sections)
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=limit)
        relevant = [message for message in recent if message.message_id > after_message_id]
        if relevant:
            combined = "\n".join(message.text for message in relevant)
            normalized = combined.lower()
            if prefix in combined and all(section in normalized for section in normalized_sections):
                return combined
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for report prefix {prefix!r} with required sections {sections}\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


async def _send_and_wait_for_token(
    harness: _LiveForumHarness,
    *,
    text: str,
    token: str,
    thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    timeout: float = 180.0,
    limit: int = 120,
) -> TelegramForumObservedMessage:
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    await harness.platform.send_nowait(
        text,
        thread_id=thread_id,
        reply_to_message_id=reply_to_message_id,
    )
    return await _wait_for_message_after_containing(
        harness,
        thread_id=thread_id,
        after_message_id=baseline,
        token=token,
        timeout=timeout,
        limit=limit,
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


def _append_unread_inbox_message(
    *,
    team_name: str,
    recipient: str,
    content: str,
    summary: str,
    sender: str,
) -> None:
    inbox_path = Path.home() / ".claude" / "teams" / team_name / "inboxes" / f"{recipient}.json"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    if inbox_path.exists():
        try:
            loaded = json.loads(inbox_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                entries = [item for item in loaded if isinstance(item, dict)]
        except Exception:
            entries = []
    entries.append(
        {
            "from": sender,
            "text": content,
            "summary": summary,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "read": False,
        }
    )
    inbox_path.write_text(json.dumps(entries, ensure_ascii=True), encoding="utf-8")


@dataclass
class _LiveForumHarness:
    platform: TelegramForumPlatform
    proc: subprocess.Popen
    log_file: Path
    vault_path: Path
    bot_username: str
    temp_root: Path | None = None
    state_db_path: Path | None = None

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
            # Allow pending callbacks from prior runs/topics to drain so tests start
            # from a stable baseline.
            await asyncio.sleep(2.0)
            try:
                await harness.platform.wait_for_global_silence(seconds=2.0)
            except AssertionError:
                await asyncio.sleep(2.0)
            return
        await asyncio.sleep(1.0)
    raise AssertionError("Forum bot did not respond to warmup /clear all")


async def _reset_general(harness: _LiveForumHarness) -> None:
    clear_cmd = f"/clear@{harness.bot_username} all"
    for _ in range(6):
        baseline = await harness.platform.latest_bot_message_id(thread_id=None)
        trace = await harness.platform.send_control(
            clear_cmd,
            timeout=25.0,
        )
        if "cleared" in trace.output.lower():
            await asyncio.sleep(2.0)
            try:
                await harness.platform.wait_for_global_silence(seconds=2.0)
            except AssertionError:
                await asyncio.sleep(2.0)
            return
        recent = await harness.platform.get_recent_messages(thread_id=None, limit=40)
        if any(
            message.message_id > baseline and "cleared" in message.text.lower()
            for message in recent
        ):
            await asyncio.sleep(2.0)
            try:
                await harness.platform.wait_for_global_silence(seconds=2.0)
            except AssertionError:
                await asyncio.sleep(2.0)
            return
        await asyncio.sleep(1.0)
    raise AssertionError("Forum bot did not confirm /clear all")


async def _session_id_for_route(
    harness: _LiveForumHarness,
    *,
    thread_id: int | None,
) -> str:
    baseline = await harness.platform.latest_bot_message_id(thread_id=thread_id)
    await harness.platform.send_nowait(
        (
            "This is a deterministic integration test. "
            "Use the session_info tool and reply with only the session_id UUID."
        ),
        thread_id=thread_id,
    )
    deadline = asyncio.get_running_loop().time() + 90.0
    while True:
        recent = await harness.platform.get_recent_messages(thread_id=thread_id, limit=40)
        for message in recent:
            if message.message_id <= baseline:
                continue
            match = _SESSION_ID_RE.search(message.text)
            if match:
                return match.group(1)
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "Timed out waiting for session_id UUID\n"
                f"{harness.failure_context()}"
            )
        await asyncio.sleep(1.0)


async def _ensure_cached_forum_chat_id() -> int:
    global _CACHED_FORUM_CHAT_ID

    bootstrap = TelegramForumPlatform()
    await bootstrap.connect()
    try:
        if _CACHED_FORUM_CHAT_ID is not None:
            if await bootstrap._probe_forum_chat(_CACHED_FORUM_CHAT_ID):
                return _CACHED_FORUM_CHAT_ID
            _CACHED_FORUM_CHAT_ID = None

        for key in ("OBS_TELEGRAM_TEST_FORUM_CHAT_ID", "TELEGRAM_TEST_FORUM_CHAT_ID"):
            raw = (os.environ.get(key) or "").strip()
            if not raw:
                continue
            try:
                configured_chat_id = int(raw)
            except ValueError:
                continue
            if await bootstrap._probe_forum_chat(configured_chat_id):
                _CACHED_FORUM_CHAT_ID = configured_chat_id
                return _CACHED_FORUM_CHAT_ID

        # Prefer reusing an existing valid forum chat to avoid Telegram
        # CreateChannel flood-wait throttling across long live suites.
        _CACHED_FORUM_CHAT_ID = await bootstrap.provision_forum_chat()
        return _CACHED_FORUM_CHAT_ID
    finally:
        await bootstrap.close()


def _clear_cached_forum_chat_id() -> None:
    global _CACHED_FORUM_CHAT_ID
    _CACHED_FORUM_CHAT_ID = None


@pytest_asyncio.fixture
async def live_tg_forum(tmp_path: Path) -> _LiveForumHarness:
    if not _has_telegram_credentials():
        pytest.skip("Telegram forum credentials not configured in environment")
    os.environ["OBS_TELEGRAM_BOT_TOKENS"] = ",".join(_resolve_sender_tokens())

    vault_path = ensure_live_test_vault()
    temp_root = tmp_path / "obs-agent-temp"
    state_db_path = tmp_path / "telegram-state.sqlite3"
    proc, log_file = _start_bot(vault_path, temp_root, state_db_path=state_db_path)
    shared_chat_id = await _ensure_cached_forum_chat_id()
    platform = TelegramForumPlatform(chat_id=shared_chat_id, idle_quiescence_timeout=90.0)
    harness = _LiveForumHarness(
        platform=platform,
        proc=proc,
        log_file=log_file,
        vault_path=vault_path,
        bot_username=os.environ["TELEGRAM_TEST_BOT_USERNAME"],
        temp_root=temp_root,
        state_db_path=state_db_path,
    )
    await platform.connect()
    try:
        await _warm_platform(harness)
        yield harness
    finally:
        await platform.close()
        _stop_bot(harness.proc)


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

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only GENERAL-{tag}.",
            token=f"GENERAL-{tag}",
            timeout=240.0,
        )
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only TOPIC-{tag}.",
            thread_id=thread_id,
            token=f"TOPIC-{tag}",
            timeout=240.0,
        )

        general_visibility = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token TOPIC-{tag} anywhere "
                "in our conversation, otherwise NO."
            ),
            token="NO",
            timeout=240.0,
        )
        topic_visibility = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token GENERAL-{tag} anywhere "
                "in our conversation, otherwise NO."
            ),
            thread_id=thread_id,
            token="NO",
            timeout=240.0,
        )

        assert "NO" in general_visibility.text, live_tg_forum.failure_context()
        assert "NO" in topic_visibility.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_command_creates_child_topic_and_keeps_parent_session(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        alpha_msg = await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only ALPHA-{tag}.",
            token=f"ALPHA-{tag}",
            timeout=240.0,
        )
        assert f"ALPHA-{tag}" in alpha_msg.text, live_tg_forum.failure_context()
        general_session_before = await _session_id_for_route(live_tg_forum, thread_id=None)
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send_control(
            f"/fork@{live_tg_forum.bot_username} Fork-{tag}",
            timeout=30.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork topic created",
            timeout=120.0,
        )
        child_thread_id, service_message_id = _extract_topic_link(launch_msg.text)
        child_recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=child_thread_id,
            limit=6,
        )

        assert any("fork created" in message.text.lower() for message in child_recent), (
            launch_msg.text + live_tg_forum.failure_context()
        )
        assert any(str(service_message_id) == str(message.message_id) for message in child_recent), (
            launch_msg.text + live_tg_forum.failure_context()
        )

        child_visibility = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token ALPHA-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=child_thread_id,
            token="YES",
            timeout=240.0,
        )
        child_session = await _session_id_for_route(live_tg_forum, thread_id=child_thread_id)
        general_session_after = await _session_id_for_route(live_tg_forum, thread_id=None)

        assert "YES" in child_visibility.text, live_tg_forum.failure_context()
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

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only A-{tag}.",
            thread_id=topic_a,
            token=f"A-{tag}",
            timeout=240.0,
        )
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only B-{tag}.",
            thread_id=topic_b,
            token=f"B-{tag}",
            timeout=240.0,
        )

        clear_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=topic_a)
        await live_tg_forum.platform.send_control(
            f"/clear@{live_tg_forum.bot_username}",
            thread_id=topic_a,
            timeout=60.0,
        )
        clear_confirm = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=topic_a,
            after_message_id=clear_baseline,
            token="session cleared",
            timeout=120.0,
        )
        assert "session cleared" in clear_confirm.text.lower(), live_tg_forum.failure_context()

        topic_a_visibility = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token A-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=topic_a,
            token="NO",
            timeout=240.0,
        )
        topic_b_visibility = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token B-{tag} earlier "
                "in our conversation, otherwise NO."
            ),
            thread_id=topic_b,
            token="YES",
            timeout=240.0,
        )

        await live_tg_forum.platform.send_nowait(
            f"/delete@{live_tg_forum.bot_username}",
            thread_id=topic_a,
        )
        await asyncio.sleep(2.0)
        topic_b_alive = await _send_and_wait_for_token(
            live_tg_forum,
            text=(
                "This is a deterministic integration test. "
                f"Reply with only ALIVE-{tag}."
            ),
            thread_id=topic_b,
            token=f"ALIVE-{tag}",
            timeout=240.0,
        )

        assert "NO" in topic_a_visibility.text, live_tg_forum.failure_context()
        assert "YES" in topic_b_visibility.text, live_tg_forum.failure_context()
        assert f"ALIVE-{tag}" in topic_b_alive.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_stop_interrupts_and_topic_stays_responsive(
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

        resume_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Reply with only RESUMED."
            ),
            thread_id=thread_id,
            require_done=False,
            timeout=90.0,
        )
        assert "working" in resume_trace.output.lower(), live_tg_forum.failure_context()
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

    async def test_live_renamed_topic_uses_new_visible_name_for_next_child(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Original {tag}")
        await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
            thread_id=parent_thread_id,
        )
        await live_tg_forum.platform.rename_topic(parent_thread_id, f"Renamed {tag}")
        await asyncio.sleep(3.0)
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description CHILD-{tag} and prompt "
                f"'This is a deterministic integration test inside a child topic. Reply with only CHILD-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=90.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        assert f"Renamed {tag} - CHILD-{tag}" in launch_message.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_launches_child_and_parent_receives_result(
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
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        launch_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description CHILD-{tag} and prompt "
                f"'This is a deterministic integration test inside a child topic. "
                f"Reply with only CHILD-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}. "
                "Do not solve the child task yourself."
            ),
            require_done=False,
            timeout=60.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)

        child_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"CHILD-{tag}",
            timeout=180.0,
        )
        assert f"CHILD-{tag}" in child_message.text, live_tg_forum.failure_context()

        parent_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw the exact token CHILD-{tag} from the most recent "
                "ForkTask result, otherwise NO."
            ),
            require_done=False,
        )
        parent_yes = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=parent_baseline,
            token="YES",
            timeout=180.0,
        )
        child_session = await _session_id_for_route(live_tg_forum, thread_id=child_thread_id)
        general_session_after = await _session_id_for_route(live_tg_forum, thread_id=None)

        assert "YES" in parent_yes.text, live_tg_forum.failure_context()
        assert child_session != general_session_before, live_tg_forum.failure_context()
        assert general_session_after == general_session_before, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_child_can_receive_user_secret_and_report_back(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        secret = f"SECRET-{tag}"
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        launch_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description SECRET-{tag} and prompt "
                "'This is a deterministic integration test inside a child topic. "
                "You must use the Bash tool to execute exactly sleep 15 before any final answer. "
                f"If the user later sends a token starting with SECRET-{tag}, remember it and "
                "when you finish reply with only that exact token.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=60.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)

        await asyncio.sleep(2.0)
        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic integration test. "
                f"The exact secret token is {secret}. Reply later with only that token."
            ),
            thread_id=child_thread_id,
        )

        child_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=secret,
            timeout=240.0,
        )
        assert secret in child_message.text, live_tg_forum.failure_context()

        parent_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if the most recent ForkTask result reported the exact token "
                f"{secret}, otherwise NO."
            ),
            require_done=False,
            timeout=60.0,
        )

        parent_yes = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=parent_baseline,
            token="YES",
            timeout=180.0,
        )
        assert "YES" in parent_yes.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_interrupt_reports_stopped_to_parent(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        launch_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description INTERRUPT-{tag} and prompt "
                "'This is a deterministic integration test inside a child topic. "
                "You must use the Bash tool to execute exactly sleep 30 before any final answer. "
                "Do not send any natural-language answer before that command finishes. "
                "After the sleep, and only after the sleep, "
                f"reply with NEVER-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=60.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)

        await asyncio.sleep(2.0)
        stop_trace = await live_tg_forum.platform.send_control(
            f"/stop@{live_tg_forum.bot_username}",
            thread_id=child_thread_id,
            timeout=25.0,
        )
        normalized_stop = stop_trace.output.lower()
        assert (
            "interrupt sent" in normalized_stop
            or "timeout: no response from bot" in normalized_stop
        ), live_tg_forum.failure_context()

        interrupted_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="fork task stopped",
            timeout=180.0,
        )
        assert "fork task stopped" in interrupted_message.text.lower(), (
            live_tg_forum.failure_context()
        )

        parent_visibility = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Reply with only STOPPED if the most recent ForkTask ended stopped, "
                "otherwise OTHER."
            ),
        )

        assert "stopped" in parent_visibility.output.lower(), live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_output_reports_running(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description OUTPUT-{tag} and prompt "
                "'This is a deterministic integration test inside a child topic. "
                "You must use the Bash tool to execute exactly sleep 20 before any final answer. "
                f"After the sleep, reply with only OUTPUT-DONE-{tag}.' "
                "Immediately after launch, call ForkTaskOutput on the returned handle with "
                "block=false and timeout=1. Reply with only RUNNING if the output reports running, "
                "otherwise OTHER."
            ),
            require_done=False,
            timeout=90.0,
        )
        running_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="RUNNING",
            timeout=180.0,
        )
        assert "RUNNING" in running_message.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_stop_tool_stops_running_child(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description STOPTOOL-{tag} and prompt "
                "'This is a deterministic integration test inside a child topic. "
                "You must use the Bash tool to execute exactly sleep 25 before any final answer. "
                f"After the sleep, reply with only NEVER-{tag}.' "
                "Immediately after launch, call ForkTaskStop on the returned handle. "
                "Reply with only STOP-SENT."
            ),
            require_done=False,
            timeout=90.0,
        )
        stop_sent = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="STOP-SENT",
            timeout=180.0,
        )
        assert "STOP-SENT" in stop_sent.text, live_tg_forum.failure_context()
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)
        stopped_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="fork task stopped",
            timeout=180.0,
        )
        assert "fork task stopped" in stopped_message.text.lower(), live_tg_forum.failure_context()

    async def test_live_fork_task_resume_reuses_same_topic(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description RESUME-{tag} and prompt "
                f"'This is a deterministic integration test inside a child topic. Reply with only FIRST-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=60.0,
        )
        launched_parent = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"LAUNCHED-{tag}",
            timeout=180.0,
        )
        assert f"LAUNCHED-{tag}" in launched_parent.text, live_tg_forum.failure_context()
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)
        first_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"FIRST-{tag}",
            timeout=180.0,
        )
        assert f"FIRST-{tag}" in first_message.text, live_tg_forum.failure_context()

        resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Resume the most recent ForkTask by calling ForkTask again with the same handle, "
                f"description RESUME2-{tag}, and prompt "
                f"'This is a deterministic integration test inside the resumed child topic. Reply with only SECOND-{tag}.' "
                f"Reply with only RESUMED-{tag} after launching the resumed work."
            ),
            require_done=False,
            timeout=90.0,
        )
        resumed_parent = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=resume_baseline,
            token=f"RESUMED-{tag}",
            timeout=180.0,
        )
        assert f"RESUMED-{tag}" in resumed_parent.text, live_tg_forum.failure_context()

        second_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"SECOND-{tag}",
            timeout=180.0,
        )
        assert f"SECOND-{tag}" in second_message.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_output_invalid_handle_reports_not_found(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()

        result_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Call ForkTaskOutput with task_id bogus-task-id, block=false, timeout=1. "
                "Reply with only NOT_FOUND if the tool reports no task found, otherwise OTHER."
            ),
        )

        assert "NOT_FOUND" in result_trace.output, live_tg_forum.failure_context()

    async def test_live_fork_task_stop_invalid_handle_reports_not_found(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()

        result_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Call ForkTaskStop with task_id bogus-task-id. "
                "Reply with only NOT_FOUND if the tool reports no task found, otherwise OTHER."
            ),
        )

        assert "NOT_FOUND" in result_trace.output, live_tg_forum.failure_context()

    async def test_live_fork_task_repeated_stop_and_output_on_stopped_handle_are_deterministic(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description STOPCHECK-{tag} and prompt "
                "'This is a deterministic integration test inside a child topic. "
                "You must use the Bash tool to execute exactly sleep 25 before any final answer.' "
                "Immediately call ForkTaskStop on the returned handle. "
                "Then call ForkTaskStop again on the same handle. "
                "Then call ForkTaskOutput on the same handle with block=false timeout=1. "
                f"Reply with only STOPPED-{tag} if the first stop succeeds, and both the second stop "
                "and the later ForkTaskOutput report that the task is already unavailable "
                "(either 'no task found' or 'not running with status killed'). "
                "Otherwise reply with only OTHER."
            ),
            require_done=False,
            timeout=180.0,
        )
        stopped = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"STOPPED-{tag}",
            timeout=180.0,
        )
        assert f"STOPPED-{tag}" in stopped.text, live_tg_forum.failure_context()

    async def test_live_fork_task_resume_after_user_intervention_preserves_state(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        secret = f"SECRET-{tag}"
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description RESUMESECRET-{tag} and prompt "
                f"'This is a deterministic integration test inside a child topic. Reply with only FIRST-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=60.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=120.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)
        await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"FIRST-{tag}",
            timeout=180.0,
        )

        await live_tg_forum.platform.send_nowait(
            (
                "This is a deterministic integration test. "
                f"The exact secret token is {secret}. Remember it for later and do not forget it."
            ),
            thread_id=child_thread_id,
        )
        await asyncio.sleep(2.0)

        resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Resume the most recent ForkTask by calling ForkTask again with the same handle and prompt "
                f"'This is a deterministic integration test inside the resumed child topic. "
                f"Reply with only {secret}.' "
                f"After launching resumed work, reply with only RESUMED-{tag}."
            ),
            require_done=False,
            timeout=90.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=resume_baseline,
            token=f"RESUMED-{tag}",
            timeout=180.0,
        )
        resumed_message = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=secret,
            timeout=180.0,
        )
        assert secret in resumed_message.text, live_tg_forum.failure_context()

    async def test_live_fork_task_output_after_completion_returns_completed(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()

        result_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description STABLE-{tag} and prompt "
                f"'This is a deterministic integration test inside a child topic. Reply with only STABLE-{tag}.' "
                "Capture the returned agentId, then call ForkTaskOutput with block=true timeout=120000. "
                "After it completes, call ForkTaskOutput again on the same task_id with block=false timeout=1. "
                f"Reply with only STABLE-{tag} if both ForkTaskOutput calls report completed status for that handle, otherwise reply with only OTHER."
            ),
            timeout=180.0,
        )

        assert f"STABLE-{tag}" in result_trace.output, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_agent_task_resume_survives_daemon_restart(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the AgentTask tool exactly once with description RESTART-{tag}, "
                "fork=false, run_in_background=true, and prompt "
                f"'This is a deterministic integration test inside a child topic. Reply with only FIRST-{tag}.' "
                f"After launching it, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=90.0,
        )
        launch_system = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="agent task launched:",
            timeout=180.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_system.text)
        launch_child = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token="agentId:",
            timeout=180.0,
        )
        task_id = _extract_agent_id(launch_child.text)
        first_child = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"FIRST-{tag}",
            timeout=180.0,
        )
        assert f"FIRST-{tag}" in first_child.text, live_tg_forum.failure_context()

        _stop_bot(live_tg_forum.proc)
        assert live_tg_forum.temp_root is not None
        live_tg_forum.proc, live_tg_forum.log_file = _start_bot(
            live_tg_forum.vault_path,
            live_tg_forum.temp_root,
            state_db_path=live_tg_forum.state_db_path,
        )
        await asyncio.sleep(5.0)

        resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Resume the existing AgentTask handle {task_id} by calling AgentTask once with "
                f"resume={task_id}, fork=false, run_in_background=true, description RESTART-RESUME-{tag}, "
                "and prompt "
                f"'This is a deterministic integration test inside the resumed child topic. Reply with only SECOND-{tag}.' "
                f"After launching resumed work, reply with only RESUMED-{tag}."
            ),
            require_done=False,
            timeout=120.0,
        )
        resumed_parent = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=resume_baseline,
            token=f"RESUMED-{tag}",
            timeout=240.0,
        )
        assert f"RESUMED-{tag}" in resumed_parent.text, live_tg_forum.failure_context()
        second_child = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"SECOND-{tag}",
            timeout=240.0,
        )
        assert f"SECOND-{tag}" in second_child.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_fork_task_multi_handle_stop_is_isolated(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Launch exactly two ForkTask children and then stop. "
                f"For the first, use description ISO-A-{tag} and prompt "
                f"'This is a deterministic integration test inside child A. You must use the Bash tool to execute exactly sleep 90 before any final answer. After the sleep, reply with only A-DONE-{tag}.' "
                f"For the second, use description ISO-B-{tag} and prompt "
                f"'This is a deterministic integration test inside child B. Reply with only B-DONE-{tag}.' "
                f"After both launches, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=180.0,
        )
        launched = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"LAUNCHED-{tag}",
            timeout=180.0,
        )
        assert f"LAUNCHED-{tag}" in launched.text, live_tg_forum.failure_context()

        launch_a = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"fork task launched: __[__General - ISO-A-{tag}",
            timeout=120.0,
        )
        launch_b = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"fork task launched: __[__General - ISO-B-{tag}",
            timeout=120.0,
        )
        child_a_thread_id, _ = _extract_topic_link(launch_a.text)
        child_b_thread_id, _ = _extract_topic_link(launch_b.text)
        child_a_launch = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_a_thread_id,
            token="agentId:",
            timeout=120.0,
        )
        handle_a = _extract_agent_id(child_a_launch.text)

        stop_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Call ForkTaskStop exactly once with task_id={handle_a}. "
                f"Reply with only STOP-SENT-{tag}."
            ),
            require_done=False,
            timeout=120.0,
        )
        stop_sent = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=stop_baseline,
            token=f"STOP-SENT-{tag}",
            timeout=180.0,
        )
        assert f"STOP-SENT-{tag}" in stop_sent.text, live_tg_forum.failure_context()

        stopped_a = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_a_thread_id,
            token="fork task stopped",
            timeout=180.0,
        )
        completed_b = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_b_thread_id,
            token=f"B-DONE-{tag}",
            timeout=180.0,
        )

        assert "fork task stopped" in stopped_a.text.lower(), live_tg_forum.failure_context()
        assert f"B-DONE-{tag}" in completed_b.text, live_tg_forum.failure_context()
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_multi_chat_concurrent_isolation(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        second_chat_id = await live_tg_forum.platform.provision_forum_chat()
        second = TelegramForumPlatform(chat_id=second_chat_id)
        await second.connect()
        try:
            tag = uuid.uuid4().hex[:8]
            first_token = f"CHAT1-{tag}"
            second_token = f"CHAT2-{tag}"
            first_task = live_tg_forum.platform.send(
                f"This is a deterministic integration test. Reply with only {first_token}.",
                timeout=180.0,
            )
            second_task = second.send(
                f"This is a deterministic integration test. Reply with only {second_token}.",
                timeout=180.0,
            )
            first_trace, second_trace = await asyncio.gather(first_task, second_task)
            assert first_token in first_trace.output, live_tg_forum.failure_context()
            assert second_token in second_trace.output, second.format_recent_messages()
            assert second_token not in first_trace.output, live_tg_forum.failure_context()
            assert first_token not in second_trace.output, second.format_recent_messages()
        finally:
            await second.close()

    async def test_live_multi_bot_sender_pool_uses_multiple_bot_senders(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        if len(live_tg_forum.platform._bot_sender_ids) < 2:
            pytest.skip("Need at least two bot sender IDs for multi-bot sender-pool validation")
        second_chat_id = await live_tg_forum.platform.provision_forum_chat()
        second = TelegramForumPlatform(chat_id=second_chat_id, idle_quiescence_timeout=90.0)
        await second.connect()
        try:
            tag = uuid.uuid4().hex[:8]
            _build_busy_files(live_tg_forum.vault_path, count=12)
            busy_list = ", ".join(
                str(path.relative_to(live_tg_forum.vault_path))
                for path in sorted((live_tg_forum.vault_path / "busy-topic-test").glob("*.md"))
            )
            await second.send_control(f"/clear@{live_tg_forum.bot_username} all", timeout=40.0)
            baseline = await second.latest_bot_message_id(thread_id=None)
            await second.send(
                (
                    "This is a deterministic integration test. "
                    "Read each listed file using separate Read tool calls before answering. "
                    f"After all reads, reply with only MULTIBOT-{tag}. Files: {busy_list}"
                ),
                require_done=False,
                timeout=120.0,
            )
            final = await _wait_for_message_after_containing(
                _LiveForumHarness(
                    platform=second,
                    proc=live_tg_forum.proc,
                    log_file=live_tg_forum.log_file,
                    vault_path=live_tg_forum.vault_path,
                    bot_username=live_tg_forum.bot_username,
                ),
                thread_id=None,
                after_message_id=baseline,
                token=f"MULTIBOT-{tag}",
                timeout=420.0,
            )
            assert f"MULTIBOT-{tag}" in final.text, second.format_recent_messages()
            recent = await second.get_recent_messages(thread_id=None, limit=120)
            sender_ids = {
                msg.sender_id
                for msg in recent
                if msg.message_id > baseline and isinstance(msg.sender_id, int)
            }
            assert len(sender_ids) >= 2, (
                f"Expected at least two sender bots in recent stream, got {sender_ids}\n"
                f"{second.format_recent_messages()}"
            )
        finally:
            await second.close()

    async def test_live_fork_task_ten_concurrent_children_complete(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-{tag}.",
            timeout=120.0,
        )
        assert f"PRIME-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        launch_instructions = " ".join(
            (
                f"Child {index}: description LOAD-{tag}-{index:02d}; "
                f"prompt 'This is a deterministic integration test inside child {index}. "
                f"Reply with only LOAD-DONE-{tag}-{index:02d}.'; "
                "timeout_ms 600000"
            )
            for index in range(1, 11)
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                "Launch exactly ten ForkTask children. "
                "Do not set resume for these launches. "
                f"{launch_instructions} "
                f"After launching all children, reply with only LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=240.0,
        )
        launched = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"LAUNCHED-{tag}",
            timeout=240.0,
        )
        assert f"LAUNCHED-{tag}" in launched.text, live_tg_forum.failure_context()

        deadline = asyncio.get_running_loop().time() + 420.0
        terminal_markers: list[TelegramForumObservedMessage] = []
        while asyncio.get_running_loop().time() < deadline:
            recent = await live_tg_forum.platform.get_recent_messages(thread_id=None, limit=200)
            terminal_markers = [
                msg for msg in recent
                if msg.message_id > baseline
                and (
                    "fork task completed" in msg.text.lower()
                    or "fork task failed" in msg.text.lower()
                    or "fork task timed out" in msg.text.lower()
                    or "fork task stopped" in msg.text.lower()
                )
            ]
            if len(terminal_markers) >= 10:
                break
            await asyncio.sleep(2.0)
        assert len(terminal_markers) >= 10, (
            f"Expected >=10 parent terminal markers, got {len(terminal_markers)}\n"
            f"{live_tg_forum.failure_context()}"
        )

    async def test_live_fork_task_same_title_collision_creates_distinct_topics(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        prime_trace = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only PRIME-COLLIDE-{tag}.",
            timeout=120.0,
        )
        assert f"PRIME-COLLIDE-{tag}" in prime_trace.output, live_tg_forum.failure_context()
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use ForkTask exactly twice with the same description COLLIDE-{tag}-F1. "
                "Do not set resume for these launches. "
                f"First prompt: 'Reply with only COLLIDE-A-{tag}.' "
                f"Second prompt: 'Reply with only COLLIDE-B-{tag}.' "
                f"After both launches, reply with only COLLIDE-LAUNCHED-{tag}."
            ),
            require_done=False,
            timeout=180.0,
        )
        launched = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"COLLIDE-LAUNCHED-{tag}",
            timeout=180.0,
        )
        assert f"COLLIDE-LAUNCHED-{tag}" in launched.text, live_tg_forum.failure_context()
        recent = await live_tg_forum.platform.get_recent_messages(thread_id=None, limit=120)
        launches = [
            msg for msg in recent
            if msg.message_id > baseline and f"COLLIDE-{tag}-F1" in msg.text and "fork task launched" in msg.text.lower()
        ]
        assert len(launches) >= 2, live_tg_forum.failure_context()
        thread_ids: set[int] = set()
        for msg in launches:
            thread_id, _ = _extract_topic_link(msg.text)
            thread_ids.add(thread_id)
        assert len(thread_ids) >= 2, (
            f"Expected duplicate title launches to map to distinct topics; got {thread_ids}\n"
            f"{live_tg_forum.failure_context()}"
        )

    async def test_live_native_task_is_denied_while_fork_task_remains_available(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]

        native_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic policy integration test. "
                "Call the native Task tool exactly once with run_in_background=true and description "
                f"NATIVE-{tag}. "
                "Do not call other native Task* tools. "
                "Classify the outcome into this schema: "
                '{"task_available":bool,"launch_ok":bool,"blocked":bool}. '
                f"Reply with exactly one line: NATIVE-PARITY-{tag}: <json>"
            ),
            require_done=False,
            timeout=180.0,
        )
        native_line = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=native_baseline,
            token=f"NATIVE-PARITY-{tag}:",
            timeout=360.0,
        )
        native_payload = _extract_json_object(native_line.text)
        assert native_payload.get("launch_ok") is False, live_tg_forum.failure_context()
        assert native_payload.get("blocked") is True, live_tg_forum.failure_context()
        assert isinstance(native_payload.get("task_available"), bool), live_tg_forum.failure_context()

        fork_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic parity integration test. "
                "Use the ForkTask tool exactly once with description "
                f"FORK-{tag}, run_in_background='true', timeout_ms='120000', and empty resume. "
                f"Fork prompt: 'Use Bash to run sleep 20 and then respond with only FORK-DONE-{tag}.' "
                "Do not call tools in parallel; execute exactly one tool call at a time and wait for each result. "
                "Capture the returned agentId. Then, in this same turn: "
                "call ForkTaskOutput with block=false timeout=1 on that agentId; "
                "call ForkTaskStop on that agentId; "
                "call ForkTaskStop again on that agentId; "
                "call ForkTaskOutput again with block=false timeout=1. "
                "Now classify results into this schema: "
                '{"launch_ok":bool,"first_output":"not_ready|no_task|other","second_stop":"killed|no_task|other","final_output":"no_task|sibling_error|other"}. '
                f"Reply with exactly one line: FORK-PARITY-{tag}: <json>"
            ),
            require_done=False,
            timeout=180.0,
        )
        fork_line = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=fork_baseline,
            token=f"FORK-PARITY-{tag}:",
            timeout=360.0,
        )
        fork_payload = _extract_json_object(fork_line.text)
        assert fork_payload.get("launch_ok") is True, live_tg_forum.failure_context()
        assert fork_payload.get("first_output") in {"not_ready", "no_task"}, live_tg_forum.failure_context()
        assert fork_payload.get("second_stop") in {"killed", "no_task"}, live_tg_forum.failure_context()
        assert fork_payload.get("final_output") in {"no_task", "sibling_error"}, live_tg_forum.failure_context()

    async def test_live_agent_authored_task_vs_forktask_comparison_report(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic parity integration test. "
                "Attempt one native Task launch and one short ForkTask run first. "
                f"Native Task description REPORT-NATIVE-{tag}, prompt 'Reply with only REPORT-NATIVE-DONE-{tag}'. "
                f"ForkTask description REPORT-FORK-{tag}, prompt 'Reply with only REPORT-FORK-DONE-{tag}', run_in_background='true'. "
                "After both are launched, compare usability and behavior from your own tool-caller perspective. "
                "Include exactly these section headers in plain text: "
                "API shape, Runtime semantics, Error semantics, Agent ergonomics, Recommendation. "
                f"Prefix the full report with REPORT-{tag}:"
            ),
            require_done=False,
            timeout=240.0,
        )
        text = await _wait_for_report_with_sections(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            prefix=f"REPORT-{tag}:",
            sections=(
                "API shape",
                "Runtime semantics",
                "Error semantics",
                "Agent ergonomics",
                "Recommendation",
            ),
            timeout=420.0,
            limit=160,
        )
        assert "API shape" in text, live_tg_forum.failure_context()
        assert "Runtime semantics" in text, live_tg_forum.failure_context()
        assert "Error semantics" in text, live_tg_forum.failure_context()
        assert "Agent ergonomics" in text, live_tg_forum.failure_context()
        assert "Recommendation" in text, live_tg_forum.failure_context()

    async def test_live_inline_reply_fork_from_user_message_succeeds(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"User Anchor {tag}")

        seed = await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only SEED-{tag}.",
            thread_id=thread_id,
            require_done=True,
        )
        _message_containing(seed, f"SEED-{tag}")
        user_anchor_id = seed.sent_message_id
        assert isinstance(user_anchor_id, int), live_tg_forum.failure_context()
        session_before = await _session_id_for_route(live_tg_forum, thread_id=thread_id)

        tail = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Remember token USER-TAIL-{tag}. Reply with only TAIL-{tag}."
            ),
            thread_id=thread_id,
            require_done=True,
        )
        _message_containing(tail, f"TAIL-{tag}")

        forked = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw token USER-TAIL-{tag}, otherwise NO."
            ),
            thread_id=thread_id,
            reply_to_message_id=user_anchor_id,
            require_done=True,
        )
        session_after = await _session_id_for_route(live_tg_forum, thread_id=thread_id)

        forked_text = "\n".join(message.text for message in forked.messages) or forked.output
        assert "can't fork from this message" not in forked_text.lower(), live_tg_forum.failure_context()
        assert "NO" in forked_text, live_tg_forum.failure_context()
        assert session_after != session_before, live_tg_forum.failure_context()

    async def test_live_inline_reply_fork_from_tool_use_message_succeeds(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        thread_id = await live_tg_forum.platform.create_topic(f"Tool Anchor {tag}")

        data_dir = live_tg_forum.vault_path / "__tg_live_test__"
        data_dir.mkdir(parents=True, exist_ok=True)
        tool_file = data_dir / f"tool-anchor-{tag}.md"
        tool_file.write_text(f"# Tool Anchor {tag}\n", encoding="utf-8")

        tool_trace = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the Read tool exactly once on __tg_live_test__/tool-anchor-{tag}.md, "
                f"then reply with only TOOL-READY-{tag}."
            ),
            thread_id=thread_id,
            require_done=True,
        )
        _message_containing(tool_trace, f"TOOL-READY-{tag}")

        tool_message = next(
            (
                msg
                for msg in tool_trace.messages
                if ("Read:" in msg.text or "Bash:" in msg.text)
            ),
            None,
        )
        assert tool_message is not None, live_tg_forum.failure_context()
        session_before = await _session_id_for_route(live_tg_forum, thread_id=thread_id)

        tail = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Remember token TOOL-TAIL-{tag}. Reply with only TOOL-TAIL-ACK-{tag}."
            ),
            thread_id=thread_id,
            require_done=True,
        )
        assert f"TOOL-TAIL-ACK-{tag}" in tail.output, live_tg_forum.failure_context()

        forked = await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Reply with only YES if you saw token TOOL-TAIL-{tag}, otherwise NO."
            ),
            thread_id=thread_id,
            reply_to_message_id=tool_message.message_id,
            require_done=True,
        )
        session_after = await _session_id_for_route(live_tg_forum, thread_id=thread_id)

        assert "can't fork from this message" not in forked.output.lower(), live_tg_forum.failure_context()
        assert "NO" in forked.output, live_tg_forum.failure_context()
        assert session_after != session_before, live_tg_forum.failure_context()

    async def test_live_fork_child_service_message_links_to_source_message(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send(
            f"This is a deterministic integration test. Reply with only SOURCE-{tag}.",
            require_done=False,
        )
        source_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"SOURCE-{tag}",
            timeout=180.0,
        )
        source_message_id = source_message.message_id

        fork_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        await live_tg_forum.platform.send_control(
            f"/fork@{live_tg_forum.bot_username} Link-{tag}",
            timeout=30.0,
        )
        fork_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=fork_baseline,
            token="fork topic created",
            timeout=120.0,
        )
        child_thread_id, service_message_id = _extract_topic_link(fork_message.text)
        child_recent = await live_tg_forum.platform.get_recent_messages(
            thread_id=child_thread_id,
            limit=8,
        )
        service_message = next(
            (msg for msg in child_recent if msg.message_id == service_message_id),
            None,
        )
        assert service_message is not None, live_tg_forum.failure_context()

        links = _MESSAGE_LINK_RE.findall(service_message.text)
        assert links, (
            "Expected child service message to include source link.\n"
            + live_tg_forum.failure_context()
        )
        assert any(
            link.rstrip("/").split("/")[-1] == str(source_message_id)
            for link in links
        ), (
            f"Expected source message id {source_message_id} in service links {links}\n"
            + live_tg_forum.failure_context()
        )

    async def test_live_multi_bot_single_route_keeps_single_session(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        if len(live_tg_forum.platform._bot_sender_ids) < 2:
            pytest.skip("Need at least two bot sender IDs for multi-bot route/session test")

        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
        for index in range(4):
            trace = await live_tg_forum.platform.send(
                (
                    "This is a deterministic multi-bot consistency integration test. "
                    f"Reply with only MULTI-SESSION-{tag}-{index}."
                ),
                timeout=120.0,
                require_done=True,
            )
            assert f"MULTI-SESSION-{tag}-{index}" in trace.output, live_tg_forum.failure_context()
            working_count = sum(1 for message in trace.messages if "working" in message.text.lower())
            assert working_count == 1, (
                f"Expected exactly one working marker per prompt, saw {working_count}.\n"
                + live_tg_forum.failure_context()
            )

        recent = await live_tg_forum.platform.get_recent_messages(thread_id=None, limit=120)
        sender_ids = {
            msg.sender_id
            for msg in recent
            if msg.message_id > baseline and isinstance(msg.sender_id, int)
        }
        assert len(sender_ids) >= 2, (
            f"Expected multi-bot sender activity, got sender_ids={sender_ids}\n"
            + live_tg_forum.failure_context()
        )

        sids = {
            await _session_id_for_route(live_tg_forum, thread_id=None),
            await _session_id_for_route(live_tg_forum, thread_id=None),
            await _session_id_for_route(live_tg_forum, thread_id=None),
        }
        assert len(sids) == 1, (
            f"Expected one stable session id in general route, got {sids}\n"
            + live_tg_forum.failure_context()
        )

    async def test_live_fork_task_resume_supports_multiple_consecutive_resumes(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)

        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test. "
                f"Use the ForkTask tool exactly once with description RESUME3-{tag} and prompt "
                f"'Reply with only FIRST3-{tag}.' "
                f"After launching it, reply with only LAUNCHED3-{tag}."
            ),
            require_done=False,
            timeout=90.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token=f"LAUNCHED3-{tag}",
            timeout=180.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=None,
            after_message_id=baseline,
            token="fork task launched",
            timeout=180.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_message.text)
        await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"FIRST3-{tag}",
            timeout=180.0,
        )

        for index, token in enumerate((f"SECOND3-{tag}", f"THIRD3-{tag}"), start=1):
            resume_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=None)
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic integration test. "
                    "Resume the most recent ForkTask by calling ForkTask with the same handle and prompt "
                    f"'Reply with only {token}.' "
                    f"After launching resumed work, reply with only RESUME-STEP-{index}-{tag}."
                ),
                require_done=False,
                timeout=120.0,
            )
            await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=None,
                after_message_id=resume_baseline,
                token=f"RESUME-STEP-{index}-{tag}",
                timeout=240.0,
            )
            resumed_token = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=token,
                timeout=240.0,
            )
            assert token in resumed_token.text, live_tg_forum.failure_context()

    async def test_live_super_task_team_workers_share_task_list_and_inbox(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        team_name = f"live-team-{tag}"
        worker_a = f"live-a-{tag[:4]}"
        worker_b = f"live-b-{tag[:4]}"
        task_subject = f"LIVE-TASK-{tag}"
        inbox_token = f"LIVE-INBOX-{tag}"
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Team Flow {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only TEAM-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"TEAM-PRIME-{tag}",
            timeout=240.0,
        )

        baseline_a = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test for team workers. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_a}, description TEAM-A-{tag}, and prompt "
                "'Call TeamCreate with team_name="
                f"{team_name}. "
                f"Then call TaskCreate with subject={task_subject} and description=\"Shared integration task\". "
                f"Then call SendInboxMessage with team_name={team_name}, recipient={worker_b}, "
                f"content={inbox_token}, summary=\"handoff\", sender={worker_a}. "
                f"When done, reply with only TEAM-A-DONE-{tag}.' "
                f"After launch, reply with only TEAM-A-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_a,
            token=f"TEAM-A-LAUNCHED-{tag}",
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
        await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_a_thread,
            token=f"TEAM-A-DONE-{tag}",
            timeout=420.0,
        )

        baseline_b = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test for team workers. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_b}, description TEAM-B-{tag}, and prompt "
                "'Call TaskList and verify "
                f"{task_subject} is present. "
                f"Then call ReadInbox with team_name={team_name}, agent={worker_b}, "
                f"include_read=false, mark_read=true, limit=20 and verify {inbox_token} exists. "
                f"If both checks pass, reply with only TEAM-B-OK-{tag}; otherwise TEAM-B-FAIL-{tag}.' "
                f"After launch, reply with only TEAM-B-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_b,
            token=f"TEAM-B-LAUNCHED-{tag}",
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
            token=f"TEAM-B-OK-{tag}",
            timeout=420.0,
        )
        assert f"TEAM-B-OK-{tag}" in worker_b_done.text, live_tg_forum.failure_context()

    async def test_live_idle_team_worker_wakes_from_polled_inbox(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        team_name = f"live-polled-team-{tag}"
        worker_name = f"live-polled-worker-{tag[:4]}"
        inbox_token = f"POLLED-WAKE-{tag}"
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Team Poll Wake {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only POLL-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"POLL-PRIME-{tag}",
            timeout=240.0,
        )

        launch_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test for team wake polling. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_name}, description POLL-WORKER-{tag}, and prompt "
                "'Call TeamCreate with team_name="
                f"{team_name}. "
                f"Then reply with only POLL-IDLE-{tag}.' "
                f"After launch, reply with only POLL-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=launch_baseline,
            token=f"POLL-LAUNCHED-{tag}",
            timeout=240.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=launch_baseline,
            token="agent task launched",
            timeout=240.0,
        )
        worker_thread_id, _ = _extract_topic_link(launch_message.text)
        await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_thread_id,
            token=f"POLL-IDLE-{tag}",
            timeout=420.0,
        )

        wake_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=worker_thread_id)
        _append_unread_inbox_message(
            team_name=team_name,
            recipient=worker_name,
            content=inbox_token,
            summary="polled wake",
            sender="external-live-test",
        )
        wake_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_thread_id,
            after_message_id=wake_baseline,
            token="agent task wake: teammate message received",
            timeout=240.0,
        )
        assert "agent task wake: teammate message received" in wake_message.text.lower(), (
            live_tg_forum.failure_context()
        )
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()

    async def test_live_running_team_worker_consumes_polled_pending_wake_after_completion(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        await _reset_general(live_tg_forum)
        tag = uuid.uuid4().hex[:8]
        team_name = f"live-pending-team-{tag}"
        worker_name = f"live-pending-worker-{tag[:4]}"
        first_token = f"POLL-FIRST-{tag}"
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Team Pending Wake {tag}")

        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic integration test. Reply with only PENDING-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"PENDING-PRIME-{tag}",
            timeout=240.0,
        )

        launch_baseline = await live_tg_forum.platform.latest_bot_message_id(thread_id=parent_thread_id)
        await live_tg_forum.platform.send(
            (
                "This is a deterministic integration test for pending team wake polling. "
                "Use AgentTask exactly once with fork=false, "
                f"team_name={team_name}, name={worker_name}, description PENDING-WORKER-{tag}, and prompt "
                "'Call TeamCreate with team_name="
                f"{team_name}. "
                "Then use Bash exactly once to run sleep 20. "
                f"After sleep finishes, reply with only {first_token}.' "
                f"After launch, reply with only PENDING-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=240.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=launch_baseline,
            token=f"PENDING-LAUNCHED-{tag}",
            timeout=240.0,
        )
        launch_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=launch_baseline,
            token="agent task launched",
            timeout=240.0,
        )
        worker_thread_id, _ = _extract_topic_link(launch_message.text)

        await asyncio.sleep(4.0)
        _append_unread_inbox_message(
            team_name=team_name,
            recipient=worker_name,
            content=f"pending polled wake message {tag}",
            summary="pending polled wake",
            sender="external-live-test",
        )

        first_done = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=worker_thread_id,
            token=first_token,
            timeout=300.0,
        )
        wake_message = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=worker_thread_id,
            after_message_id=first_done.message_id,
            token="agent task wake: teammate message received",
            timeout=240.0,
        )
        assert "agent task wake: teammate message received" in wake_message.text.lower(), (
            live_tg_forum.failure_context()
        )
        assert live_tg_forum.proc.poll() is None, live_tg_forum.failure_context()
