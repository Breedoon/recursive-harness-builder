"""Live long-session compaction regression tests.

These tests use real Claude Code / SDK calls. They are intentionally opt-in
because they spend live model tokens, but they exercise the failure mode where
OBS passed a too-large auto-compact window and sessions died with
``Prompt is too long`` before compaction.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any

import pytest
import httpx
from fastapi.testclient import TestClient

from obs_agent.cache_proxy_lifecycle import start_cache_proxy
from obs_agent.config import OBSConfig
from obs_agent.context_jsonl import find_session_jsonl, load_jsonl_usage_snapshot
from obs_agent.daemon import create_app
from obs_agent.hooks import HookState
from obs_agent.jsonl_health import analyze_jsonl_path
from obs_agent.jsonl_fork import fork_session_jsonl
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent
from obs_agent.session import SessionManager
from tests.live_test_vault import ensure_live_test_vault

pytestmark = [pytest.mark.live, pytest.mark.asyncio, pytest.mark.real_get_client]

_DEFAULT_BROKEN_OPUS_SESSION_ID = "5d85d993-6134-4b0c-8590-bfe305d16e3b"
_DEFAULT_BROKEN_GPT_SESSION_ID = "226b1adc-5a6d-44e6-8ae4-557572912fd8"
_DEFAULT_TRUST_SESSION_ID = "137406cb-965d-4488-97ba-7aca104a3d45"


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_LONG_CONTEXT_LIVE") != "1",
    reason="set OBS_RUN_LONG_CONTEXT_LIVE=1 to spend live tokens on compaction",
)
async def test_haiku_long_session_auto_compacts_and_remains_usable(tmp_path):
    await _run_haiku_compaction_probe(
        tmp_path=tmp_path,
        auto_compact_window_tokens=50_000,
        padding_repetitions=1200,
        max_turns=7,
        min_peak_tokens=25_000,
        drop_ratio=0.75,
        token_prefix="COMPACT",
    )


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_LONG_CONTEXT_200K_LIVE") != "1",
    reason="set OBS_RUN_LONG_CONTEXT_200K_LIVE=1 to spend live tokens on a 200k compaction probe",
)
async def test_haiku_200k_auto_compacts_and_remains_usable(tmp_path):
    await _run_haiku_compaction_probe(
        tmp_path=tmp_path,
        auto_compact_window_tokens=200_000,
        padding_repetitions=1450,
        max_turns=11,
        min_peak_tokens=145_000,
        drop_ratio=0.80,
        token_prefix="CLAUDE200K",
    )


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_TRUST_DAEMON_LIVE") != "1",
    reason="set OBS_RUN_TRUST_DAEMON_LIVE=1 to spend live Opus tokens on the TRUST daemon route",
)
async def test_trust_session_runs_via_daemon_with_default_1m_context() -> None:
    _load_dotenv()
    session_id = os.environ.get("OBS_TRUST_SESSION_ID", _DEFAULT_TRUST_SESSION_ID)
    config = OBSConfig(
        vault_path=Path(
            os.environ.get(
                "OBS_TRUST_CWD",
                str(OBSConfig().vault_path),
            )
        ),
        model="claude-opus-4-7",
        cache_proxy_enabled=False,
    )
    app = create_app(config)
    app.state.session_manager.set_session_id(session_id)
    options = app.state.session_manager.create_options()
    assert options.resume == session_id
    assert options.model == "claude-opus-4-7[1m]"
    assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
    assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"

    prompt = (
        "This is a live OBS daemon recovery check for the TRUST session. "
        "Do not continue prior delegated work. Reply with exactly "
        "TRUST-DAEMON-1M-RUNNABLE."
    )
    with TestClient(app) as client:
        response = await asyncio.to_thread(
            client.post,
            "/chat",
            json={"message": prompt},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] == session_id
    assert "TRUST-DAEMON-1M-RUNNABLE" in payload["response"]
    assert "Prompt is too long" not in payload["response"]


async def _run_haiku_compaction_probe(
    *,
    tmp_path: Path,
    auto_compact_window_tokens: int,
    padding_repetitions: int,
    max_turns: int,
    min_peak_tokens: int,
    drop_ratio: float,
    token_prefix: str,
) -> None:
    _load_dotenv()
    vault = ensure_live_test_vault(tmp_path / "vault")
    config = OBSConfig(vault_path=vault, model="haiku", cache_proxy_enabled=False)
    config.auto_compact_window_tokens = auto_compact_window_tokens

    hook_state = HookState()
    session_manager = SessionManager(config=config, hook_state=hook_state)
    usage_totals: list[int] = []
    outputs: list[str] = []
    padding = "Synthetic long-context compaction test padding. " * padding_repetitions

    try:
        options = session_manager.create_options()
        assert options.model == "claude-haiku-4-5[1m]"
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
        assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(auto_compact_window_tokens)
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in options.env

        for turn in range(1, max_turns + 1):
            runner = ConversationRunner(session_manager, hook_state, config)
            output_parts: list[str] = []
            prompt = (
                f"Turn {turn}. Store this padding, then reply with exactly "
                f"{token_prefix}-OK-{turn}.\n\n{padding}"
            )
            async for event in runner.run(prompt):
                if isinstance(event, TextEvent):
                    output_parts.append(event.text)
                elif isinstance(event, DoneEvent):
                    break

            output = "".join(output_parts)
            outputs.append(output)
            assert "Prompt is too long" not in output

            snapshot = load_jsonl_usage_snapshot(
                session_id=session_manager.session_id,
                cwd=vault,
            )
            if snapshot is None:
                continue
            usage_totals.append(snapshot.latest_context_triplet_tokens)

            if (
                len(usage_totals) >= 3
                and max(usage_totals[:-1]) > min_peak_tokens
                and usage_totals[-1] < max(usage_totals[:-1]) * drop_ratio
            ):
                followup_runner = ConversationRunner(session_manager, hook_state, config)
                followup_parts: list[str] = []
                async for event in followup_runner.run(
                    f"After compaction, reply with exactly {token_prefix}-STILL-USABLE."
                ):
                    if isinstance(event, TextEvent):
                        followup_parts.append(event.text)
                    elif isinstance(event, DoneEvent):
                        break
                followup = "".join(followup_parts)
                assert f"{token_prefix}-STILL-USABLE" in followup
                assert "Prompt is too long" not in followup
                return

        raise AssertionError(
            f"no compaction signal observed; usage_totals={usage_totals}, "
            f"last_outputs={outputs[-2:]}"
        )
    finally:
        await session_manager.disconnect()


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_BROKEN_OPUS_RECOVERY_LIVE") != "1",
    reason="set OBS_RUN_BROKEN_OPUS_RECOVERY_LIVE=1 to spend live Opus tokens on recovery",
)
async def test_existing_broken_opus_session_compacts_and_recovers() -> None:
    _load_dotenv()
    session_id = os.environ.get(
        "OBS_BROKEN_OPUS_SESSION_ID",
        _DEFAULT_BROKEN_OPUS_SESSION_ID,
    )
    cwd = Path(
        os.environ.get(
            "OBS_BROKEN_OPUS_CWD",
            str(OBSConfig().vault_path),
        )
    )
    source_path = find_session_jsonl(session_id=session_id, cwd=cwd)
    if source_path is None:
        pytest.skip(f"broken Opus JSONL not found for {session_id}")

    health = analyze_jsonl_path(path=source_path, session_id=session_id)
    target_uuid = health.safe_recovery_uuid
    assert target_uuid is not None
    assert _context_triplet_at_uuid(source_path, target_uuid) >= 160_000
    recovery_session_id = fork_session_jsonl(
        session_id=session_id,
        target_uuid=target_uuid,
        cwd=cwd,
        new_session_id=str(uuid.uuid4()),
    )

    first = await _run_claude_resume(
        session_id=recovery_session_id,
        cwd=cwd,
        prompt=(
            "Stop prior work. Compact if needed, then reply briefly that the "
            "session is usable."
        ),
        timeout_seconds=360,
    )
    assert _contains_prompt_too_long_error(session_id=recovery_session_id, cwd=cwd) is False

    snapshot = load_jsonl_usage_snapshot(session_id=recovery_session_id, cwd=cwd)
    assert snapshot is not None
    assert snapshot.latest_context_triplet_tokens < 120_000

    second = await _run_claude_resume(
        session_id=recovery_session_id,
        cwd=cwd,
        prompt="Reply with exactly BROKEN-OPUS-RECOVERED.",
        timeout_seconds=180,
    )
    assert "BROKEN-OPUS-RECOVERED" in second
    assert _contains_prompt_too_long_error(session_id=recovery_session_id, cwd=cwd) is False


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_BROKEN_GPT_RECOVERY_LIVE") != "1",
    reason="set OBS_RUN_BROKEN_GPT_RECOVERY_LIVE=1 to spend live GPT tokens on recovery",
)
async def test_existing_broken_gpt_session_compacts_and_recovers() -> None:
    _load_dotenv()
    session_id = os.environ.get(
        "OBS_BROKEN_GPT_SESSION_ID",
        _DEFAULT_BROKEN_GPT_SESSION_ID,
    )
    cwd = Path(
        os.environ.get(
            "OBS_BROKEN_GPT_CWD",
            str(OBSConfig().vault_path),
        )
    )
    source_path = find_session_jsonl(session_id=session_id, cwd=cwd)
    if source_path is None:
        pytest.skip(f"broken GPT JSONL not found for {session_id}")

    health = analyze_jsonl_path(path=source_path, session_id=session_id)
    target_uuid = health.safe_recovery_uuid
    assert target_uuid is not None
    assert _context_triplet_at_uuid(source_path, target_uuid) >= 160_000
    recovery_session_id = fork_session_jsonl(
        session_id=session_id,
        target_uuid=target_uuid,
        cwd=cwd,
        new_session_id=str(uuid.uuid4()),
    )

    output = await _run_gpt_resume(
        session_id=recovery_session_id,
        cwd=cwd,
        prompt=(
            "Ignore prior task state. Reply with exactly "
            "BROKEN-GPT-RECOVERED."
        ),
        timeout_seconds=360,
    )
    assert "compact_boundary" in output
    assert '"subtype":"success"' in output or '"subtype": "success"' in output
    assert _contains_prompt_too_long_error(session_id=recovery_session_id, cwd=cwd) is False


def _last_good_assistant_before_prompt_too_long(source_path: Path) -> str:
    entries: list[dict[str, Any]] = []
    by_uuid: dict[str, dict[str, Any]] = {}
    with source_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            entry_uuid = obj.get("uuid")
            if isinstance(entry_uuid, str) and entry_uuid:
                by_uuid[entry_uuid] = obj

    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        if "Prompt is too long" not in _entry_text(entry):
            continue
        failing_user_uuid = entry.get("parentUuid")
        failing_user = by_uuid.get(failing_user_uuid) if isinstance(failing_user_uuid, str) else None
        if failing_user is not None and failing_user.get("type") == "user":
            last_good_uuid = failing_user.get("parentUuid")
            if isinstance(last_good_uuid, str) and last_good_uuid:
                return last_good_uuid
        if isinstance(failing_user_uuid, str) and failing_user_uuid:
            return failing_user_uuid
        break

    raise AssertionError(f"no Prompt is too long assistant entry found in {source_path}")


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
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _contains_prompt_too_long_error(*, session_id: str, cwd: Path) -> bool:
    path = find_session_jsonl(session_id=session_id, cwd=cwd)
    if path is None:
        return False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("error") == "invalid_request" and _entry_text(obj).strip() == "Prompt is too long":
                return True
    return False


def _context_triplet_at_uuid(source_path: Path, target_uuid: str) -> int:
    with source_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict) or obj.get("uuid") != target_uuid:
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                break
            usage = message.get("usage")
            if not isinstance(usage, dict):
                break
            return (
                _as_int(usage.get("input_tokens"))
                + _as_int(usage.get("cache_creation_input_tokens"))
                + _as_int(usage.get("cache_read_input_tokens"))
            )
    raise AssertionError(f"no usage found at {target_uuid} in {source_path}")


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


async def _run_claude_resume(
    *,
    session_id: str,
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
) -> str:
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        pytest.skip("claude CLI not found")

    env = os.environ.copy()
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "1000000"
    env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] = "1000000"
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"

    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "-r",
        session_id,
        "-p",
        prompt,
        "--model",
        "claude-opus-4-7[1m]",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    output = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"claude exited {proc.returncode}\nSTDOUT:\n{output}\nSTDERR:\n{err}"
    return output + "\n" + err


def _ensure_cache_proxy(port: int):
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
        if response.status_code == 200:
            return None
    except Exception:
        pass
    return start_cache_proxy(port)


async def _run_gpt_resume(
    *,
    session_id: str,
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
) -> str:
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        pytest.skip("claude CLI not found")

    _load_dotenv()
    config = OBSConfig.from_env()
    proxy_proc = _ensure_cache_proxy(config.cache_proxy_port)
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{config.cache_proxy_port}"
    env["ANTHROPIC_API_KEY"] = env.get("OBS_CLI_PROXY_API_KEY", config.cli_proxy_api_key)
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "200000"
    env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] = "400000"
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"

    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "--bare",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "-r",
        session_id,
        "-p",
        prompt,
        "--model",
        os.environ.get("OBS_BROKEN_GPT_MODEL", "gpt-5.5[400k]"),
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    finally:
        if proxy_proc is not None:
            proxy_proc.terminate()
            try:
                proxy_proc.wait(timeout=3)
            except Exception:
                proxy_proc.kill()

    output = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"claude exited {proc.returncode}\nSTDOUT:\n{output}\nSTDERR:\n{err}"
    return output + "\n" + err
