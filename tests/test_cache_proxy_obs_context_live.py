"""Live OBS cache-proxy test for persisted CLAUDE.md context.

Runs the real OBS SessionManager/ConversationRunner path through an isolated
cache proxy.  This covers the behavior that SDK-only cache tests cannot:
OBS prepends project context into the first persisted user turn, the proxy
strips every Claude Code <system-reminder>, and a manual JSONL fork resumes
without duplicating the OBS context.

Run explicitly:
    ~/Documents/obs/.venv/bin/python -m pytest -m live \
        tests/test_cache_proxy_obs_context_live.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

_tests_dir = str(Path(__file__).resolve().parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import pytest

from conftest_cache_proxy import (  # noqa: E402
    BULK_TEXT,
    TEST_LOG_DIR,
    assert_cache_hit,
    fmt_usage,
    get_proxy_usage_for_turns,
    proxy_log_length,
)
from obs_agent.config import OBSConfig  # noqa: E402
import obs_agent.cache_proxy_lifecycle as cache_proxy_lifecycle  # noqa: E402
from obs_agent.context_jsonl import find_session_jsonl  # noqa: E402
from obs_agent.events import StatusEvent  # noqa: E402
from obs_agent.hooks import HookState  # noqa: E402
from obs_agent.jsonl_fork import fork_session_jsonl  # noqa: E402
from obs_agent.prompt import ENTRY_FILE_SENTINEL  # noqa: E402
from obs_agent.runner import ConversationRunner  # noqa: E402
from obs_agent.session import SessionManager  # noqa: E402

pytestmark = [pytest.mark.live, pytest.mark.asyncio, pytest.mark.real_get_client]


def _make_config(project: Path, proxy_port: int) -> OBSConfig:
    return OBSConfig(
        vault_path=project,
        model="claude-opus-4-7",
        cache_proxy_enabled=True,
        cache_proxy_port=proxy_port,
        telegram_state_db_path=project / ".claude" / "telegram-state.sqlite3",
    )


async def _run_obs_turn(
    manager: SessionManager,
    config: OBSConfig,
    prompt: str,
) -> list[str]:
    runner = ConversationRunner(manager, manager.hook_state, config)
    text: list[str] = []
    async for event in runner.run(prompt):
        if hasattr(event, "text"):
            text.append(str(event.text))
        elif isinstance(event, StatusEvent):
            continue
    return text


def _body_files(kind: str) -> list[Path]:
    return sorted((Path(TEST_LOG_DIR) / "bodies").glob(f"req*_{kind}.json"))


def _read_body(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _user_text_blocks(body: dict):
    for msg in body.get("messages", []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield block.get("text", "")


def _assert_no_user_system_reminder_blocks(body: dict) -> None:
    for text in _user_text_blocks(body):
        assert "<system-reminder>" not in text


def _last_assistant_uuid(jsonl_path: Path) -> str:
    latest: str | None = None
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("type") == "assistant" and isinstance(obj.get("uuid"), str):
            latest = obj["uuid"]
    assert latest is not None, f"no assistant uuid found in {jsonl_path}"
    return latest


def _user_entry_context_count(jsonl_path: Path) -> int:
    count = 0
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if ENTRY_FILE_SENTINEL not in line:
            continue
        obj = json.loads(line)
        if obj.get("type") == "user":
            count += 1
    return count


def _wait_for_user_entry_context_count(
    jsonl_path: Path,
    *,
    expected: int = 1,
    timeout: float = 5.0,
) -> int:
    deadline = time.monotonic() + timeout
    latest = _user_entry_context_count(jsonl_path)
    while latest != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        latest = _user_entry_context_count(jsonl_path)
    return latest


async def test_obs_context_is_persisted_and_all_reminders_strip_on_opus47(
    proxy_with_bodies: int,
    test_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cache_proxy_lifecycle, "_proxy_healthy", True)

    marker = f"OBS_LIVE_CONTEXT_MARKER_{uuid.uuid4().hex[:12]}"
    (test_project / "CLAUDE.md").write_text(
        "# Live OBS Context\n\n"
        f"Project marker: {marker}\n"
        "Keep responses concise. If asked for the project marker, use the exact "
        "Project marker value above.\n",
        encoding="utf-8",
    )

    config = _make_config(test_project, proxy_with_bodies)
    parent = SessionManager(config=config, hook_state=HookState())

    parent_log_start = proxy_log_length()
    parent_text = await _run_obs_turn(
        parent,
        config,
        "Use the project marker from persistent context, then reply exactly "
        f"OBS_CONTEXT_OK {marker}.\n\n"
        f"Reference padding:\n{BULK_TEXT[:20000]}",
    )
    await parent.disconnect()

    assert parent.session_id, "OBS runner did not capture a parent session id"
    parent_jsonl = find_session_jsonl(session_id=parent.session_id, cwd=test_project)
    assert parent_jsonl is not None, f"JSONL not found for {parent.session_id}"
    parent_raw = parent_jsonl.read_text(encoding="utf-8")
    assert _wait_for_user_entry_context_count(parent_jsonl) == 1
    assert marker in parent_raw
    assert marker in "".join(parent_text)

    pre_files = _body_files("pre")
    post_files = _body_files("post")
    assert pre_files and post_files, "proxy did not save request bodies"
    parent_pre = _read_body(pre_files[-1])
    parent_post = _read_body(post_files[-1])
    assert "<system-reminder>" in _dump(parent_pre)
    _assert_no_user_system_reminder_blocks(parent_post)
    assert parent_post["model"] == "claude-opus-4-7"
    assert _dump(parent_post).count(ENTRY_FILE_SENTINEL) == 1
    assert marker in _dump(parent_post)

    parent_usage = get_proxy_usage_for_turns(start_offset=parent_log_start)
    assert parent_usage, "proxy did not record parent usage"
    parent_last = parent_usage[-1]
    print(f"\n  OBS parent {parent.session_id}: {fmt_usage(parent_last)}")

    target_uuid = _last_assistant_uuid(parent_jsonl)
    fork_session_id = fork_session_jsonl(
        session_id=parent.session_id,
        target_uuid=target_uuid,
        cwd=test_project,
        new_session_id=str(uuid.uuid4()),
    )

    await asyncio.sleep(1.0)

    fork_config = _make_config(test_project, proxy_with_bodies)
    fork = SessionManager(config=fork_config, hook_state=HookState())
    fork.set_session_id(fork_session_id)
    fork_log_start = proxy_log_length()
    await _run_obs_turn(
        fork,
        fork_config,
        "Reply exactly OBS_FORK_OK and do not modify files.",
    )
    await fork.disconnect()

    fork_jsonl = find_session_jsonl(session_id=fork_session_id, cwd=test_project)
    assert fork_jsonl is not None, f"fork JSONL not found for {fork_session_id}"
    fork_raw = fork_jsonl.read_text(encoding="utf-8")
    assert _wait_for_user_entry_context_count(fork_jsonl) == 1

    fork_post = _read_body(_body_files("post")[-1])
    fork_dump = _dump(fork_post)
    _assert_no_user_system_reminder_blocks(fork_post)
    assert fork_dump.count(ENTRY_FILE_SENTINEL) == 1
    assert marker in fork_dump

    fork_usage = get_proxy_usage_for_turns(start_offset=fork_log_start)
    assert fork_usage, "proxy did not record fork usage"
    fork_first = fork_usage[-1]
    print(f"  OBS manual JSONL fork {fork_session_id}: {fmt_usage(fork_first)}")
    assert_cache_hit(
        fork_first.get("cr", 0),
        parent_last.get("tot", 0),
        baseline=0,
        label="OBS manual JSONL fork first turn",
    )
