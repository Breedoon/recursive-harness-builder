"""Live Telegram integration tests for reply-driven session forking."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from obs_agent.context_jsonl import find_session_jsonl
from tests.evals.platform_telegram import TelegramObservedMessage, TelegramPlatform, TelegramResponseTrace
from tests.live_test_vault import ensure_live_test_vault


_REQUIRED_ENV = [
    "OBS_TEST_TELEGRAM_API_ID",
    "OBS_TEST_TELEGRAM_API_HASH",
    "OBS_TEST_TELEGRAM_SESSION",
    "OBS_TEST_TELEGRAM_BOT_USERNAME",
    "OBS_TEST_TELEGRAM_BOT_TOKEN",
]

_SESSION_ID_RE = re.compile(
    r"session_id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_JSONL_PATH_RE = re.compile(r"jsonl_session_file:\s*(\S+)")


def _has_telegram_credentials() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


def _read_log_tail(log_file: Path) -> str:
    if not log_file.exists():
        return ""
    text = log_file.read_text(errors="replace")
    return text[-8000:]


def _resolve_allowed_users() -> str:
    candidates: list[str] = []
    for key in ("OBS_TEST_TELEGRAM_ALLOWED_USERS", "OBS_TELEGRAM_ALLOWED_USERS"):
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


def _start_bot(
    vault_path: Path,
    temp_root: Path,
    *,
    cache_window_seconds: int | None = None,
) -> tuple[subprocess.Popen, Path]:
    worktree_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["pkill", "-f", f"{worktree_root}.*obs_agent\\.telegram_main"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    env["OBS_TELEGRAM_BOT_TOKEN"] = os.environ["OBS_TEST_TELEGRAM_BOT_TOKEN"]
    # Keep single-sender mode in DM-based tests: TelegramPlatform only listens
    # to OBS_TEST_TELEGRAM_BOT_USERNAME responses.
    env["OBS_TELEGRAM_BOT_TOKENS"] = os.environ["OBS_TEST_TELEGRAM_BOT_TOKEN"]
    env["OBS_TELEGRAM_ALLOWED_USERS"] = _resolve_allowed_users()
    env["OBS_TELEGRAM_TEMP_ROOT"] = str(temp_root)
    env["OBS_TELEGRAM_DROP_PENDING_UPDATES"] = "1"
    if cache_window_seconds is not None:
        env["OBS_CACHE_WINDOW"] = str(cache_window_seconds)

    log_file = Path(tempfile.mktemp(prefix="obs_tg_fork_", suffix=".log"))
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main", "--test"],
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


def _stop_bot(proc: subprocess.Popen, log_file: Path) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _warm_platform(platform: TelegramPlatform) -> None:
    for _ in range(6):
        reply = await platform.send_control("/new", timeout=20.0)
        if "session cleared" in reply.lower():
            await platform.rebaseline()
            return
        await asyncio.sleep(1.0)
    raise AssertionError("Telegram bot did not respond to /new during warmup")


async def _reset(platform: TelegramPlatform) -> None:
    reply = await platform.send_control("/new", timeout=20.0)
    assert "session cleared" in reply.lower(), reply
    await platform.rebaseline()
    await asyncio.sleep(1.0)


def _extract_session_id(text: str) -> str:
    match = _SESSION_ID_RE.search(text)
    assert match, f"session_id missing in text:\n{text}"
    return match.group(1)


def _extract_jsonl_path(text: str) -> Path:
    match = _JSONL_PATH_RE.search(text)
    assert match, f"jsonl_session_file missing in text:\n{text}"
    return Path(match.group(1))


def _entry_text(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _uuid_chain(entries: list[dict[str, Any]], target_uuid: str) -> list[str]:
    by_uuid = {
        entry["uuid"]: entry
        for entry in entries
        if isinstance(entry.get("uuid"), str) and entry["uuid"]
    }
    chain: list[str] = []
    cursor = target_uuid
    seen: set[str] = set()
    while cursor:
        assert cursor not in seen, f"cycle in uuid chain at {cursor}"
        seen.add(cursor)
        entry = by_uuid[cursor]
        chain.append(cursor)
        parent_uuid = entry.get("parentUuid")
        if not isinstance(parent_uuid, str) or not parent_uuid:
            break
        cursor = parent_uuid
    chain.reverse()
    return chain


def _find_uuid_containing(entries: list[dict[str, Any]], snippet: str) -> str:
    for entry in entries:
        uuid = entry.get("uuid")
        if isinstance(uuid, str) and snippet in _entry_text(entry):
            return uuid
    raise AssertionError(f"Could not find JSONL entry containing {snippet!r}")


def _uuid_list(entries: list[dict[str, Any]]) -> list[str]:
    return [
        entry["uuid"]
        for entry in entries
        if isinstance(entry.get("uuid"), str) and entry["uuid"]
    ]


def _content_message(trace: TelegramResponseTrace, token: str) -> TelegramObservedMessage:
    for message in trace.messages:
        if token in message.text:
            return message
    raise AssertionError(
        f"Could not find content message containing {token!r}.\n"
        f"output={trace.output}\n"
        f"recent={trace.messages}"
    )


def _all_jsonl_files(vault_path: Path) -> set[Path]:
    project_root = Path.home() / ".claude" / "projects"
    encoded = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9]", "-", str(vault_path.resolve(strict=False)))) or "-"
    project_dir = project_root / encoded
    if not project_dir.exists():
        return set()
    return set(project_dir.glob("*.jsonl"))


def _build_busy_files(vault_path: Path, count: int = 16) -> list[Path]:
    busy_dir = vault_path / "busy-test"
    busy_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(count):
        path = busy_dir / f"busy-{index:02d}.md"
        path.write_text(
            (
                f"# Busy file {index}\n\n"
                f"This is deterministic test content number {index}.\n"
                "Read this file separately.\n"
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


@dataclass
class _LiveForkHarness:
    platform: TelegramPlatform
    proc: subprocess.Popen
    log_file: Path
    temp_root: Path
    vault_path: Path

    def failure_context(self) -> str:
        return (
            "\nRecent Telegram messages:\n"
            f"{self.platform.format_recent_messages()}\n\n"
            "Bot log tail:\n"
            f"{_read_log_tail(self.log_file)}"
        )


@pytest_asyncio.fixture
async def live_tg_fork(tmp_path: Path) -> _LiveForkHarness:
    if not _has_telegram_credentials():
        pytest.skip("Telegram credentials not configured in environment")

    vault_path = ensure_live_test_vault()
    temp_root = tmp_path / "obs-agent-temp"
    proc, log_file = _start_bot(vault_path, temp_root)
    platform = TelegramPlatform(
        timeout=180,
        done_timeout=180,
        idle_quiescence_timeout=45,
    )
    await platform.connect()
    try:
        await _warm_platform(platform)
        yield _LiveForkHarness(
            platform=platform,
            proc=proc,
            log_file=log_file,
            temp_root=temp_root,
            vault_path=vault_path,
        )
    finally:
        await platform.close()
        _stop_bot(proc, log_file)


async def _prime(platform: TelegramPlatform) -> None:
    reply = await platform.send(
        "This is a deterministic Telegram harness test. "
        "For the rest of this test, follow exact formatting instructions. "
        "Reply with exactly READY."
    )
    assert "READY" in reply, reply


async def _tool_session_id(platform: TelegramPlatform, tool_name: str) -> str:
    reply = await platform.send(
        "This is still the same deterministic Telegram harness test. "
        f"Call the {tool_name} tool. Then reply with exactly the line from the tool output "
        "that starts with session_id: and nothing else."
    )
    return _extract_session_id(reply)


async def _command_context(platform: TelegramPlatform) -> str:
    return await platform.send_control("/context", timeout=20.0)


@pytest.mark.integration
@pytest.mark.telegram
class TestTelegramLiveForking:
    async def test_live_context_command_and_tool_session_ids_match(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        tool_sid = await _tool_session_id(live_tg_fork.platform, "session_info")
        context_sid = await _tool_session_id(live_tg_fork.platform, "context_info")
        context_output = await _command_context(live_tg_fork.platform)
        command_sid = _extract_session_id(context_output)
        jsonl_path = _extract_jsonl_path(context_output)

        assert tool_sid == context_sid == command_sid, live_tg_fork.failure_context()
        assert jsonl_path.name == f"{command_sid}.jsonl", live_tg_fork.failure_context()
        assert jsonl_path.exists(), live_tg_fork.failure_context()

    async def test_live_reply_to_latest_message_continues_same_session(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        seed = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly LATEST_ANCHOR."
        )
        assert "LATEST_ANCHOR" in seed.output, live_tg_fork.failure_context()
        session_before = _extract_session_id(await _command_context(live_tg_fork.platform))
        files_before = _all_jsonl_files(live_tg_fork.vault_path)

        latest_message_id = seed.messages[-1].message_id
        reply = await live_tg_fork.platform.reply_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Call the session_info tool and then reply with exactly the line starting with session_id:.",
            reply_to_message_id=latest_message_id,
        )
        session_after = _extract_session_id(reply.output)
        files_after = _all_jsonl_files(live_tg_fork.vault_path)

        assert session_after == session_before, live_tg_fork.failure_context()
        assert files_after == files_before, live_tg_fork.failure_context()

    async def test_live_reply_to_older_message_forks_and_plain_followup_stays_on_fork(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        alpha_token = "TOKEN_ALPHA_31415"
        beta_token = "TOKEN_BETA_27182"
        alpha = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            f"Remember the token {alpha_token}. Reply with exactly ACK_ALPHA."
        )
        beta = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            f"Remember the token {beta_token}. Reply with exactly ACK_BETA."
        )
        assert "ACK_ALPHA" in alpha.output, live_tg_fork.failure_context()
        assert "ACK_BETA" in beta.output, live_tg_fork.failure_context()

        root_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        root_path = find_session_jsonl(session_id=root_session_id, cwd=live_tg_fork.vault_path)
        assert root_path is not None, live_tg_fork.failure_context()
        root_entries = _read_jsonl(root_path)
        alpha_uuid = _find_uuid_containing(root_entries, "ACK_ALPHA")
        expected_chain = _uuid_chain(root_entries, alpha_uuid)
        files_before = _all_jsonl_files(live_tg_fork.vault_path)

        alpha_message_id = _content_message(alpha, "ACK_ALPHA").message_id
        fork_check = await live_tg_fork.platform.reply_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation.",
            reply_to_message_id=alpha_message_id,
        )
        fork_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        files_after = _all_jsonl_files(live_tg_fork.vault_path)
        fork_path = find_session_jsonl(session_id=fork_session_id, cwd=live_tg_fork.vault_path)

        assert "ACK_ALPHA" in fork_check.output, live_tg_fork.failure_context()
        assert fork_session_id != root_session_id, live_tg_fork.failure_context()
        assert files_after - files_before, live_tg_fork.failure_context()
        assert fork_path is not None, live_tg_fork.failure_context()

        fork_entries = _read_jsonl(fork_path)
        fork_uuids = _uuid_list(fork_entries)
        assert fork_uuids[: len(expected_chain)] == expected_chain, live_tg_fork.failure_context()
        assert beta_token in "\n".join(_entry_text(entry) for entry in root_entries), live_tg_fork.failure_context()
        copied_prefix_entries = [
            entry
            for entry in fork_entries
            if isinstance(entry.get("uuid"), str) and entry["uuid"]
        ][: len(expected_chain)]
        assert beta_token not in "\n".join(
            _entry_text(entry) for entry in copied_prefix_entries
        ), live_tg_fork.failure_context()

        plain_followup = await live_tg_fork.platform.send(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation."
        )
        session_after_followup = _extract_session_id(await _command_context(live_tg_fork.platform))
        assert "ACK_ALPHA" in plain_followup, live_tg_fork.failure_context()
        assert session_after_followup == fork_session_id, live_tg_fork.failure_context()

    async def test_live_fork_from_fork_excludes_newer_branch_history(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        beta = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Remember the token TOKEN_BETA_DEEP. Reply with exactly ACK_BETA_DEEP."
        )
        gamma = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Remember the token TOKEN_GAMMA_DEEP. Reply with exactly ACK_GAMMA_DEEP."
        )
        assert "ACK_BETA_DEEP" in beta.output, live_tg_fork.failure_context()
        assert "ACK_GAMMA_DEEP" in gamma.output, live_tg_fork.failure_context()
        trunk_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))

        beta_message_id = _content_message(beta, "ACK_BETA_DEEP").message_id
        fork_one = await live_tg_fork.platform.reply_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation.",
            reply_to_message_id=beta_message_id,
        )
        fork_one_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        assert "ACK_BETA_DEEP" in fork_one.output, live_tg_fork.failure_context()
        assert fork_one_session_id != trunk_session_id, live_tg_fork.failure_context()

        delta = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Remember the token TOKEN_DELTA_DEEP. Reply with exactly ACK_DELTA_DEEP."
        )
        assert "ACK_DELTA_DEEP" in delta.output, live_tg_fork.failure_context()

        fork_one_message_id = _content_message(fork_one, "ACK_BETA_DEEP").message_id
        fork_two = await live_tg_fork.platform.reply_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation.",
            reply_to_message_id=fork_one_message_id,
        )
        fork_two_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        fork_two_followup = await live_tg_fork.platform.send(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation."
        )

        assert "ACK_BETA_DEEP" in fork_two.output, live_tg_fork.failure_context()
        assert "ACK_BETA_DEEP" in fork_two_followup, live_tg_fork.failure_context()
        assert fork_two_session_id not in {trunk_session_id, fork_one_session_id}, live_tg_fork.failure_context()

    async def test_live_repeated_branches_from_same_anchor_create_distinct_sessions(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        anchor = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly ANCHOR_ROOT."
        )
        tail = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly TRUNK_TAIL."
        )
        assert "ANCHOR_ROOT" in anchor.output, live_tg_fork.failure_context()
        assert "TRUNK_TAIL" in tail.output, live_tg_fork.failure_context()

        anchor_message_id = _content_message(anchor, "ANCHOR_ROOT").message_id
        seen_session_ids: set[str] = set()
        files_before = _all_jsonl_files(live_tg_fork.vault_path)

        for index in range(5):
            trace = await live_tg_fork.platform.reply_with_trace(
                "This is still the deterministic Telegram harness test. "
                "Call the session_info tool and then reply with exactly the line starting with session_id:.",
                reply_to_message_id=anchor_message_id,
            )
            session_id = _extract_session_id(trace.output)
            seen_session_ids.add(session_id)

        files_after = _all_jsonl_files(live_tg_fork.vault_path)
        assert len(seen_session_ids) == 5, live_tg_fork.failure_context()
        assert len(files_after - files_before) >= 5, live_tg_fork.failure_context()

    async def test_live_busy_reply_to_old_message_forks_when_queue_drains(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        alpha = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Remember the token TOKEN_ALPHA_BUSY. Reply with exactly ACK_ALPHA_BUSY."
        )
        tail = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Remember the token TOKEN_TAIL_BUSY. Reply with exactly ACK_TAIL_BUSY."
        )
        assert "ACK_ALPHA_BUSY" in alpha.output, live_tg_fork.failure_context()
        assert "ACK_TAIL_BUSY" in tail.output, live_tg_fork.failure_context()

        root_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        files_before = _all_jsonl_files(live_tg_fork.vault_path)
        alpha_message_id = _content_message(alpha, "ACK_ALPHA_BUSY").message_id

        busy_files = _build_busy_files(live_tg_fork.vault_path)
        busy_list = ", ".join(str(path.relative_to(live_tg_fork.vault_path)) for path in busy_files)
        await live_tg_fork.platform.send_nowait(
            "This is still the deterministic Telegram harness test. "
            "Read each of these files with separate Read tool calls before answering. "
            "After finishing, reply with exactly LONG_BUSY_DONE. Files: "
            f"{busy_list}"
        )
        # Keep this beyond FragmentBuffer quiet-gap so the follow-up reply is
        # queued as a separate turn (not merged into the busy prompt batch).
        await asyncio.sleep(2.5)

        queued_reply = await live_tg_fork.platform.reply_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation.",
            reply_to_message_id=alpha_message_id,
            timeout=240.0,
        )
        queued_fork = await live_tg_fork.platform.wait_for_prompt_with_trace(timeout=240)
        fork_session_id = _extract_session_id(await _command_context(live_tg_fork.platform))
        files_after = _all_jsonl_files(live_tg_fork.vault_path)

        assert "LONG_BUSY_DONE" in queued_reply.output, live_tg_fork.failure_context()
        assert "ACK_ALPHA_BUSY" in queued_fork.output, live_tg_fork.failure_context()
        assert fork_session_id != root_session_id, live_tg_fork.failure_context()
        assert files_after - files_before, live_tg_fork.failure_context()

        followup = await live_tg_fork.platform.send(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly the acknowledgement from the most recent remember-token "
            "instruction in our conversation."
        )
        session_after_followup = _extract_session_id(await _command_context(live_tg_fork.platform))
        assert "ACK_ALPHA_BUSY" in followup, live_tg_fork.failure_context()
        assert session_after_followup == fork_session_id, live_tg_fork.failure_context()

    async def test_live_reply_to_old_message_after_restart_fails_cleanly(
        self, live_tg_fork: _LiveForkHarness
    ) -> None:
        await _reset(live_tg_fork.platform)
        await _prime(live_tg_fork.platform)

        seed = await live_tg_fork.platform.send_with_trace(
            "This is still the deterministic Telegram harness test. "
            "Reply with exactly RESTART_ANCHOR."
        )
        anchor_message_id = _content_message(seed, "RESTART_ANCHOR").message_id
        files_before = _all_jsonl_files(live_tg_fork.vault_path)

        _stop_bot(live_tg_fork.proc, live_tg_fork.log_file)
        live_tg_fork.proc, live_tg_fork.log_file = _start_bot(
            live_tg_fork.vault_path,
            live_tg_fork.temp_root,
        )
        await asyncio.sleep(5)
        await live_tg_fork.platform.rebaseline()

        reply = await live_tg_fork.platform.reply_control_with_trace(
            "This is still the deterministic Telegram harness test. Reply exactly SHOULD_NOT_FORK.",
            reply_to_message_id=anchor_message_id,
            timeout=20.0,
        )
        files_after = _all_jsonl_files(live_tg_fork.vault_path)

        reply_lower = reply.output.lower()
        assert (
            "can't fork from this message" in reply_lower
            or "__working__" in reply_lower
        ), live_tg_fork.failure_context()
        assert files_after == files_before, live_tg_fork.failure_context()
