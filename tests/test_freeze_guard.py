"""Freeze guard on OBS's own restart and deploy paths (bead ops-47.6).

These tests run the REAL vault freeze tool (scripts/ops/freeze.py) against a scratch state file
(OPS_FREEZE_STATE, isolated by conftest), so OBS and the tool cannot drift apart silently.
No test sends a real signal or restarts anything: os.kill, plain_restart and the daemon restart
requests are patched.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import freeze_guard
from obs_agent import maintenance_restart as maint

TOOL = freeze_guard.DEFAULT_TOOL
needs_tool = pytest.mark.skipif(not TOOL.is_file(), reason="vault freeze tool not present on this host")


def _declare(fid: str, *scopes: str) -> None:
    argv = [sys.executable, str(TOOL), "declare", "--id", fid, "--by", "test", "--reason", "unit test"]
    for s in scopes:
        argv += ["--scope", s]
    subprocess.run(argv, check=True, capture_output=True, text=True, env=os.environ.copy())


@needs_tool
def test_no_freeze_allows():
    r = freeze_guard.check(maint.freeze_check_command(plain=False))
    assert not r.blocked and r.error is None


@needs_tool
@pytest.mark.parametrize("scope,plain", [("obs-restart", False), ("obs-restart", True),
                                         ("all-shared", False), ("supervisor", True)])
def test_covering_freeze_blocks(scope, plain):
    _declare("t-freeze", scope)
    r = freeze_guard.check(maint.freeze_check_command(plain=plain))
    assert r.blocked and "t-freeze" in r.message


@needs_tool
def test_non_covering_freeze_allows():
    _declare("t-cliproxy", "cliproxy")
    assert not freeze_guard.check(maint.freeze_check_command(plain=False)).blocked


@needs_tool
def test_supervisor_scope_does_not_cover_signal_restart():
    # A maintenance restart is a signal to the daemon, not a supervisorctl verb.
    _declare("t-sup", "supervisor")
    assert not freeze_guard.check(maint.freeze_check_command(plain=False)).blocked


@needs_tool
def test_ack_env_releases_only_the_named_freeze(monkeypatch):
    _declare("t-ack", "obs-restart")
    monkeypatch.setenv("OPS_FREEZE_ACK", "other-id")
    assert freeze_guard.check(maint.freeze_check_command(plain=False)).blocked
    monkeypatch.setenv("OPS_FREEZE_ACK", "t-ack")
    assert not freeze_guard.check(maint.freeze_check_command(plain=False)).blocked


def test_missing_tool_fails_open(monkeypatch, tmp_path):
    monkeypatch.setenv("OBS_FREEZE_TOOL", str(tmp_path / "absent.py"))
    r = freeze_guard.check("python -m obs_agent.maintenance_restart")
    assert not r.blocked and "not found" in r.error


def test_crashing_tool_fails_open(monkeypatch, tmp_path):
    bad = tmp_path / "freeze.py"
    bad.write_text("import sys\nraise SystemExit(7)\n")
    monkeypatch.setenv("OBS_FREEZE_TOOL", str(bad))
    r = freeze_guard.check("python -m obs_agent.maintenance_restart")
    assert not r.blocked and "exited 7" in r.error


def test_timeout_fails_open(monkeypatch, tmp_path):
    slow = tmp_path / "freeze.py"
    slow.write_text("import time\ntime.sleep(5)\n")
    monkeypatch.setenv("OBS_FREEZE_TOOL", str(slow))
    monkeypatch.setattr(freeze_guard, "TIMEOUT_SECONDS", 0.5)
    r = freeze_guard.check("python -m obs_agent.maintenance_restart")
    assert not r.blocked and "could not run" in r.error


def test_corrupt_state_fails_open(monkeypatch, tmp_path):
    state = Path(os.environ["OPS_FREEZE_STATE"])
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json")
    assert not freeze_guard.check("python -m obs_agent.maintenance_restart").blocked


# --- maintenance_restart CLI -------------------------------------------------


@needs_tool
def test_cli_refuses_during_freeze_and_ack_proceeds(monkeypatch, capsys):
    _declare("t-cli", "obs-restart")
    with patch.object(maint, "find_daemon_pids", return_value=[4242]), patch.object(maint.os, "kill") as kill, \
            patch.object(maint, "plain_restart", return_value=0) as plain:
        assert maint.main([]) == maint.FREEZE_BLOCKED_RC
        assert maint.main(["--plain"]) == maint.FREEZE_BLOCKED_RC
        kill.assert_not_called()
        plain.assert_not_called()
        assert "t-cli" in capsys.readouterr().err
        monkeypatch.setenv("OPS_FREEZE_ACK", "t-cli")
        assert maint.main([]) == 0
        kill.assert_called_once_with(4242, maint.MAINTENANCE_SIGNAL)


def test_cli_proceeds_when_freeze_tool_broken(monkeypatch, tmp_path):
    monkeypatch.setenv("OBS_FREEZE_TOOL", str(tmp_path / "absent.py"))
    with patch.object(maint, "find_daemon_pids", return_value=[4242]), patch.object(maint.os, "kill") as kill:
        assert maint.main([]) == 0
    kill.assert_called_once()


def test_cli_proceeds_when_guard_raises(monkeypatch):
    with patch.object(freeze_guard, "check", side_effect=RuntimeError("boom")), \
            patch.object(maint, "find_daemon_pids", return_value=[4242]), patch.object(maint.os, "kill") as kill:
        assert maint.main([]) == 0
    kill.assert_called_once()


# --- Daniel's Telegram /restart: honoured with a warning; others refused ------

DANIEL = 227177188
TEST_IDENTITY = 5129431382  # authorized, agent-driven test account: NOT the principal


def _update(args, user_id=DANIEL):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.chat_id = 67890
    update.effective_message.message_thread_id = None
    context = MagicMock()
    context.args = args
    return update, context


@needs_tool
@pytest.mark.asyncio
async def test_telegram_restart_warns_but_restarts_during_freeze(config):
    from tests.test_maintenance_restart import _bot

    _declare("t-tg", "obs-restart")
    bot = _bot(config)
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        bot, "request_maintenance_restart", AsyncMock()
    ) as maint_req, patch.object(bot, "request_plain_restart", AsyncMock()) as plain, patch.object(
        bot, "_collect_maintenance_entries", return_value=[]
    ), patch.object(bot, "_send_system_message", AsyncMock()) as send:
        await bot.handle_restart(*_update([]))
        maint_req.assert_awaited_once()
        await bot.handle_restart(*_update(["plain"]))
        plain.assert_awaited_once()
    texts = [c.kwargs["text"] for c in send.await_args_list]
    warnings = [t for t in texts if t.startswith("warning: an active shared-service freeze")]
    assert len(warnings) == 2 and all("t-tg" in w for w in warnings)


@needs_tool
@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [TEST_IDENTITY, 1350518665, 12345])
async def test_telegram_restart_refused_during_freeze_for_non_principal(config, user_id):
    """G1: the freeze override is Daniel's own, not every authorized user's."""
    from tests.test_maintenance_restart import _bot

    _declare("t-tg-np", "obs-restart")
    bot = _bot(config)
    assert config.telegram_principal_user_id == DANIEL
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        bot, "request_maintenance_restart", AsyncMock()
    ) as maint_req, patch.object(bot, "request_plain_restart", AsyncMock()) as plain, patch.object(
        bot, "_collect_maintenance_entries", return_value=[]
    ), patch.object(bot, "_send_system_message", AsyncMock()) as send:
        await bot.handle_restart(*_update([], user_id=user_id))
        await bot.handle_restart(*_update(["plain"], user_id=user_id))
        await bot.handle_maintenance_restart(*_update([], user_id=user_id))
    maint_req.assert_not_awaited()
    plain.assert_not_awaited()
    texts = [c.kwargs["text"] for c in send.await_args_list]
    refusals = [t for t in texts if t.startswith("refused: an active shared-service freeze")]
    assert len(refusals) == 3 and all("t-tg-np" in t for t in refusals)
    assert not any(t.startswith("warning: an active shared-service freeze") for t in texts)


@pytest.mark.asyncio
async def test_telegram_restart_non_principal_proceeds_without_freeze(config):
    from tests.test_maintenance_restart import _bot

    bot = _bot(config)
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        bot, "request_maintenance_restart", AsyncMock()
    ) as maint_req, patch.object(bot, "_collect_maintenance_entries", return_value=[]), patch.object(
        bot, "_send_system_message", AsyncMock()
    ):
        await bot.handle_restart(*_update([], user_id=TEST_IDENTITY))
    maint_req.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_restart_non_principal_fails_open_on_guard_crash(config):
    from tests.test_maintenance_restart import _bot

    bot = _bot(config)
    with patch.object(freeze_guard, "check", side_effect=RuntimeError("boom")), patch.object(
        bot, "_is_authorized", return_value=True
    ), patch.object(bot, "request_maintenance_restart", AsyncMock()) as maint_req, patch.object(
        bot, "_collect_maintenance_entries", return_value=[]
    ), patch.object(bot, "_send_system_message", AsyncMock()):
        await bot.handle_restart(*_update([], user_id=TEST_IDENTITY))
    maint_req.assert_awaited_once()


def test_principal_id_env_override(monkeypatch):
    from obs_agent.config import OBSConfig

    monkeypatch.setenv("OBS_TELEGRAM_PRINCIPAL_USER_ID", "42")
    assert OBSConfig.from_env().telegram_principal_user_id == 42


@pytest.mark.asyncio
async def test_telegram_restart_no_warning_without_freeze(config):
    from tests.test_maintenance_restart import _bot

    bot = _bot(config)
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        bot, "request_maintenance_restart", AsyncMock()
    ) as maint_req, patch.object(bot, "_collect_maintenance_entries", return_value=[]), patch.object(
        bot, "_send_system_message", AsyncMock()
    ) as send:
        await bot.handle_restart(*_update([]))
    maint_req.assert_awaited_once()
    assert not any(c.kwargs["text"].startswith("warning:") for c in send.await_args_list)


@pytest.mark.asyncio
async def test_telegram_restart_survives_guard_crash(config):
    from tests.test_maintenance_restart import _bot

    bot = _bot(config)
    with patch.object(freeze_guard, "check", side_effect=RuntimeError("boom")), patch.object(
        bot, "_is_authorized", return_value=True
    ), patch.object(bot, "request_maintenance_restart", AsyncMock()) as maint_req, patch.object(
        bot, "_collect_maintenance_entries", return_value=[]
    ), patch.object(bot, "_send_system_message", AsyncMock()):
        await bot.handle_restart(*_update([]))
    maint_req.assert_awaited_once()


# --- cache-gate pre-push (deploy) ---------------------------------------------


def _load_gate_script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "cache_gate.py"
    spec = importlib.util.spec_from_file_location("cache_gate_script_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@needs_tool
def test_pre_push_refuses_live_checkout_push_during_obs_source_freeze(monkeypatch, capsys):
    mod = _load_gate_script()
    _declare("t-push", "obs-source")
    line = "refs/heads/main " + "a" * 40 + " refs/heads/main " + "b" * 40 + "\n"
    monkeypatch.setattr(mod.os, "popen", lambda cmd: io.StringIO("/workspace/obs\n"))
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(line))
    with patch.object(mod.cache_gate, "check") as gate:
        assert mod._pre_push([]) == 3
        gate.assert_not_called()
    assert "t-push" in capsys.readouterr().err
    monkeypatch.setenv("OPS_FREEZE_ACK", "t-push")
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(line))
    ok = MagicMock(ok=True, message="gate ok")
    with patch.object(mod.cache_gate, "check", return_value=ok):
        assert mod._pre_push([]) == 0


@needs_tool
def test_pre_push_allows_other_checkout(monkeypatch):
    mod = _load_gate_script()
    _declare("t-push2", "obs-source")
    line = "refs/heads/main " + "a" * 40 + " refs/heads/main " + "b" * 40 + "\n"
    monkeypatch.setattr(mod.os, "popen", lambda cmd: io.StringIO("/workspace/runtime/worktrees/scratch\n"))
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(line))
    with patch.object(mod.cache_gate, "check", return_value=MagicMock(ok=True, message="gate ok")):
        assert mod._pre_push([]) == 0
