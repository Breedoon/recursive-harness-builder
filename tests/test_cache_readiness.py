"""Cache-readiness guard on agent-originated wakes (bead vault-vokl)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from obs_agent import cache_readiness as cr
from obs_agent.telegram import TelegramBot, TelegramRoute

from tests.test_telegram import _TEST_GAP, _state


def _jsonl(tmp_path, *, model, age_seconds, tokens=400_000, name="s.jsonl"):
    path = tmp_path / name
    stamp = datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": "hi"}},
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {
                "model": model,
                "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 5},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # mtime is "now"; the assistant timestamp must win
    return path


class TestFamiliesAndTtls:
    def test_model_family(self):
        assert cr.model_family("claude-opus-5-5") == "claude"
        assert cr.model_family("opus") == "claude"
        assert cr.model_family("gpt-6.1-sol") == "gpt"
        assert cr.model_family("gpt-6.1-terra") == "gpt"
        assert cr.model_family("gpt-5.6-luna") == "gpt"
        assert cr.model_family("local-sparks-glm-5.3-flash") == "local"
        assert cr.model_family("glm") == "local"
        assert cr.model_family("") is None

    def test_default_ttls_and_env_override(self):
        assert cr.load_ttls("") == {"claude": 3480, "gpt": 1800, "local": None}
        ttls = cr.load_ttls("claude=600, gpt=off, local=120, junk")
        assert ttls == {"claude": 600, "gpt": None, "local": 120}


class TestAssess:
    def test_claude_cold_after_58_minutes(self, tmp_path):
        warm = cr.assess(jsonl_path=_jsonl(tmp_path, model="claude-opus-5-5", age_seconds=50 * 60, name="w.jsonl"), ttls=cr.DEFAULT_TTLS)
        cold = cr.assess(jsonl_path=_jsonl(tmp_path, model="claude-opus-5-5", age_seconds=59 * 60, name="c.jsonl"), ttls=cr.DEFAULT_TTLS)
        assert not warm.cold and cold.cold
        assert cold.family == "claude" and cold.context_tokens == 400_015

    def test_gpt_cold_after_30_minutes(self, tmp_path):
        assert cr.assess(jsonl_path=_jsonl(tmp_path, model="gpt-6.1-terra", age_seconds=31 * 60), ttls=cr.DEFAULT_TTLS).cold
        assert not cr.assess(jsonl_path=_jsonl(tmp_path, model="gpt-6.1-terra", age_seconds=29 * 60, name="b.jsonl"), ttls=cr.DEFAULT_TTLS).cold

    def test_local_never_cold(self, tmp_path):
        r = cr.assess(jsonl_path=_jsonl(tmp_path, model="local-sparks-glm-5.3", age_seconds=10 * 86400), ttls=cr.DEFAULT_TTLS)
        assert not r.cold and "off" in r.reason

    def test_running_target_is_warm(self, tmp_path):
        r = cr.assess(jsonl_path=_jsonl(tmp_path, model="claude-opus-5-5", age_seconds=86400), running=True)
        assert not r.cold

    def test_unknown_transcript_is_not_blocked(self, tmp_path):
        assert not cr.assess(jsonl_path=None).cold
        assert not cr.assess(jsonl_path=tmp_path / "missing.jsonl").cold

    def test_mtime_fallback_when_no_assistant_record(self, tmp_path):
        path = tmp_path / "u.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {}}) + "\n")
        old = time.time() - 2 * 3600
        os.utime(path, (old, old))
        r = cr.assess(jsonl_path=path, model_hint="claude-opus-5-5", ttls=cr.DEFAULT_TTLS)
        assert r.cold and r.family == "claude"

    def test_warning_names_transcript_and_override(self, tmp_path):
        r = cr.assess(jsonl_path=_jsonl(tmp_path, model="claude-opus-5-5", age_seconds=3 * 3600), ttls=cr.DEFAULT_TTLS)
        text = cr.cold_warning(r, action="message to", target="agent-x")
        assert "allow_cold_wake=true" in text and str(r.jsonl_path) in text and "400,015 tokens" in text


class TestInboxValidator:
    def _bot_with_target(self, config, tmp_path, age_seconds, *, busy=False, model="claude-opus-5-5"):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        target = _state(bot, thread_id=55)
        target.session_manager.set_session_id("sid-target")
        target.busy = busy
        bot._route_inbox_targets[("2026-08-12-08-05-root", "target-agent")] = target.route
        bot._route_inbox_target_keys_by_route[target.route] = ("2026-08-12-08-05-root", "target-agent")
        path = _jsonl(tmp_path, model=model, age_seconds=age_seconds)
        return bot, target, path

    async def _validate(self, bot, path, **extra):
        validator = bot._make_inbox_recipient_validator(TelegramRoute(chat_id=67890, thread_id=1))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            return await validator({"team_name": "2026-08-12-08-05-root", "recipient": "target-agent", **extra})

    async def test_cold_target_blocked_by_default(self, config, tmp_path):
        bot, _, path = self._bot_with_target(config, tmp_path, 3 * 3600)
        result = await self._validate(bot, path)
        assert result["deliverable"] is False and result["cold"] is True
        assert str(path) in result["reason"]

    async def test_override_allows_cold_wake(self, config, tmp_path):
        bot, _, path = self._bot_with_target(config, tmp_path, 3 * 3600)
        assert (await self._validate(bot, path, allow_cold_wake=True))["deliverable"] is True

    async def test_warm_and_busy_targets_pass(self, config, tmp_path):
        bot, _, path = self._bot_with_target(config, tmp_path, 10 * 60)
        assert (await self._validate(bot, path))["deliverable"] is True
        bot, _, path = self._bot_with_target(config, tmp_path, 5 * 3600, busy=True)
        assert (await self._validate(bot, path))["deliverable"] is True

    async def test_local_target_never_blocked(self, config, tmp_path):
        bot, _, path = self._bot_with_target(config, tmp_path, 5 * 86400, model="local-sparks-glm-5.3")
        assert (await self._validate(bot, path))["deliverable"] is True


class TestSendInboxMessageTool:
    async def test_cold_block_returns_cold_outcome_and_writes_nothing(self, config, tmp_path, monkeypatch):
        from obs_agent.hooks import HookState
        from obs_agent.tools import create_obs_tools

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        hook_state = HookState()
        seen = []

        async def validator(payload):
            seen.append(payload)
            if payload.get("allow_cold_wake"):
                return {"deliverable": True}
            return {"deliverable": False, "cold": True, "reason": "cold-cache guard: test"}

        hook_state.inbox_recipient_validator = validator
        create_obs_tools(config, lambda: "sid-1", hook_state=hook_state)
        send = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")
        result = await send({"team_name": "t", "recipient": "x", "content": "hello"})
        assert result["tool_use_result"]["outcome"] == "cold_blocked"
        assert result["content"][0]["text"].startswith("cold-cache guard")
        inbox = tmp_path / ".claude" / "teams" / "t" / "inboxes" / "x.json"
        assert not inbox.exists()
        ok = await send({"team_name": "t", "recipient": "x", "content": "hello", "allow_cold_wake": True})
        assert ok["tool_use_result"]["delivered"] is True and inbox.exists()
        assert seen[0]["allow_cold_wake"] is False and seen[-1]["allow_cold_wake"] is True
        schema = next(t for t in captured["tools"] if t.name == "SendInboxMessage").input_schema
        assert schema["properties"]["allow_cold_wake"]["type"] == "boolean"


class TestAgentTaskResumeGuard:
    async def test_resume_of_cold_child_refused_unless_allowed(self, config, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        from obs_agent.telegram import _ForkTaskRecord

        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent = _state(bot, thread_id=None)
        parent.last_bot = MagicMock()
        child = _state(bot, thread_id=77)
        child.session_manager.set_session_id("sid-child")
        bot._fork_tasks_by_id["t-1"] = _ForkTaskRecord(
            task_id="t-1",
            parent_route=parent.route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="u",
            child_route=child.route,
            child_session_id="sid-child",
            prompt="x",
            agent_name="child-agent",
        )
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=4 * 3600)
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            with pytest.raises(ValueError, match="cold-cache guard: resume of child-agent"):
                await bot._launch_fork_task(route=parent.route, args={"resume": "t-1"})
            with patch.object(bot, "_resume_fork_task", AsyncMock(return_value={"ok": True})) as resume:
                result = await bot._launch_fork_task(
                    route=parent.route, args={"resume": "t-1", "allow_cold_wake": True}
                )
        assert result == {"ok": True}
        resume.assert_awaited_once()
