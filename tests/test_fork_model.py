"""Fork children run on their SOURCE's model (Daniel 2026-10-15 session_source fix)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import fork_model as fm
from obs_agent.telegram import TelegramBot

from tests.test_telegram import _TEST_GAP, _state


def _jsonl(path: Path, model: str):
    rows = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"model": model, "usage": {}}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_jsonl_model_skips_synthetic(tmp_path):
    assert fm.jsonl_model(_jsonl(tmp_path / "a.jsonl", "claude-opus-5-5")) == "claude-opus-5-5"
    assert fm.jsonl_model(tmp_path / "missing.jsonl") is None


def test_choose_rules():
    src = fm.SourceModel("claude-opus-5-5", "high", "route")
    assert fm.choose(None, src, source_label="s") == ("claude-opus-5-5", True)
    assert fm.choose("source", src, source_label="s") == ("claude-opus-5-5", True)
    assert fm.choose("claude-sonnet-5", src, source_label="s") == ("claude-sonnet-5", False)
    with pytest.raises(fm.ForkModelError, match="family claude.*family gpt"):
        fm.choose("gpt-6.1-sol", src, source_label="s")
    with pytest.raises(fm.ForkModelError):
        fm.choose("glm", fm.SourceModel("gpt-6.1-sol", None, "jsonl"), source_label="s")


def _bot(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    launcher = _state(bot, thread_id=10)
    launcher.session_manager.set_session_id("sid-glm")
    launcher.session_manager.model_override = "local-sparks-glm-5.3-flash-uncensored"
    launcher.last_bot = MagicMock()
    return bot, launcher


def test_source_model_from_live_route_then_jsonl(config, tmp_path):
    bot, launcher = _bot(config)
    owner = _state(bot, thread_id=20)
    owner.session_manager.set_session_id("sid-opus")
    owner.session_manager.model_override = "claude-opus-5-5[1m]"
    owner.session_manager.effort_override = "high"
    bot._route_by_session_id["sid-opus"] = owner.route
    got = bot._fork_source_model(state=launcher, source_session_id="sid-opus", source_jsonl_path=None, explicit=True)
    assert (got.model, got.effort, got.origin) == ("claude-opus-5-5[1m]", "high", "route")
    sol = _jsonl(tmp_path / "sol.jsonl", "gpt-6.1-sol")
    got = bot._fork_source_model(state=launcher, source_session_id="sid-sol", source_jsonl_path=sol, explicit=True)
    assert (got.model, got.origin) == ("gpt-6.1-sol", "jsonl")
    got = bot._fork_source_model(state=launcher, source_session_id="sid-glm", source_jsonl_path=None, explicit=False)
    assert got.origin == "launcher" and got.model.startswith("local-sparks-glm")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(fm.ForkModelError, match="cannot determine"):
        bot._fork_source_model(state=launcher, source_session_id="sid-x", source_jsonl_path=empty, explicit=True)


async def _launch(bot, launcher, src_path, **extra):
    descriptor = MagicMock(source_session_id="sid-src", source_jsonl_path=src_path)
    with patch("obs_agent.telegram.resolve_session_source", return_value=descriptor), patch.object(
        bot, "_await_persisted_session_uuid", AsyncMock(return_value="u1")
    ), patch.object(bot, "_await_jsonl_stability", AsyncMock(return_value="u1")), patch.object(
        bot, "_create_child_fork_topic", AsyncMock(side_effect=RuntimeError("stop-after-capture"))
    ) as create:
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            await bot._launch_fork_task(
                route=launcher.route,
                args={"prompt": "reflect", "fork": True, "session_source": str(src_path), "allow_cold_wake": True, **extra},
            )
    return create.await_args.kwargs


async def test_glm_launcher_fork_of_opus_and_sol_runs_on_source_model(config, tmp_path):
    bot, launcher = _bot(config)
    kw = await _launch(bot, launcher, _jsonl(tmp_path / "opus.jsonl", "claude-opus-5-5"))
    assert kw["model"] == "claude-opus-5-5" and kw["effort"] == "auto" and kw["launcher_lineage"] is True
    kw = await _launch(bot, launcher, _jsonl(tmp_path / "sol.jsonl", "gpt-6.1-sol"))
    assert kw["model"] == "gpt-6.1-sol" and kw["launcher_lineage"] is True
    kw = await _launch(bot, launcher, _jsonl(tmp_path / "o2.jsonl", "claude-opus-5-5"), model="claude-sonnet-5", effort="low")
    assert kw["model"] == "claude-sonnet-5" and kw["effort"] == "low"


async def test_cross_family_fork_fails_loudly(config, tmp_path):
    bot, launcher = _bot(config)
    with patch("obs_agent.telegram.resolve_session_source",
               return_value=MagicMock(source_session_id="sid-src", source_jsonl_path=_jsonl(tmp_path / "o.jsonl", "claude-opus-5-5"))), \
         patch.object(bot, "_await_persisted_session_uuid", AsyncMock(return_value="u1")), \
         patch.object(bot, "_await_jsonl_stability", AsyncMock(return_value="u1")):
        with pytest.raises(fm.ForkModelError, match="cannot fork session sid-src"):
            await bot._launch_fork_task(
                route=launcher.route,
                args={"prompt": "x", "fork": True, "session_source": "s", "model": "glm", "allow_cold_wake": True},
            )
