"""vault-5lko (sender team on inbox rows and wakes) and the new-session effort default."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from obs_agent.config import OBSConfig
from obs_agent.telegram import TelegramBot, TelegramRoute


def _send_tool(config, tmp_path, monkeypatch, *, root_team_key: str):
    from obs_agent.hooks import HookState
    from obs_agent.tools import create_obs_tools

    captured: dict = {}

    def _fake_server(name, tools):
        captured["tools"] = tools
        return {"type": "fake-server", "tools": tools}

    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
    monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "obs_agent.tools.find_latest_obs_bootstrap_for_session",
        lambda **_: SimpleNamespace(
            root_team_key=root_team_key, agent_name="sender-agent", lineage=("Root",)
        ),
    )
    hook_state = HookState()
    payloads: list[dict] = []

    async def notifier(payload):
        payloads.append(payload)
        return {"delivered": True}

    hook_state.inbox_message_notifier = notifier
    create_obs_tools(config, lambda: "sid-1", hook_state=hook_state)
    send = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")
    schema = next(t for t in captured["tools"] if t.name == "SendInboxMessage").input_schema
    return send, payloads, schema


class TestSenderTeam:
    async def test_inbox_row_and_notifier_carry_sender_team(self, config, tmp_path, monkeypatch):
        send, payloads, schema = _send_tool(config, tmp_path, monkeypatch, root_team_key="team-a")
        result = await send({"team_name": "team-b", "recipient": "x", "content": "hello"})
        assert result["tool_use_result"]["delivered"] is True
        inbox = tmp_path / ".claude" / "teams" / "team-b" / "inboxes" / "x.json"
        row = json.loads(inbox.read_text())[0]
        assert row["from"] == "sender-agent"
        assert row["from_team"] == "team-a"
        assert payloads[0]["sender_team"] == "team-a"
        # No new MCP schema field (a tool-text change would re-cache the fleet).
        assert "from_team" not in schema["properties"]
        assert "sender_team" not in schema["properties"]

    async def test_cross_team_wake_names_sender_team(self, config):
        bot = TelegramBot(config, enable_background_poller=False)
        seen: dict = {}

        async def fake_wake(**kwargs):
            seen.update(kwargs)
            return True

        state = SimpleNamespace(route=TelegramRoute(chat_id=1, thread_id=2))
        bot._resolve_team_worker_record = lambda **_: None  # type: ignore[assignment]
        bot._resolve_route_inbox_target = lambda **_: state  # type: ignore[assignment]
        bot._maybe_wake_route_inbox_target = fake_wake  # type: ignore[assignment]
        result = await bot._handle_inbox_message_notification(
            sender_route=state.route,
            payload={
                "team_name": "team-b",
                "recipient": "x",
                "sender": "sender-agent",
                "sender_team": "team-a",
                "content": "hi",
                "_direct_send": True,
            },
        )
        assert result == {"delivered": True}
        assert seen["sender"] == "sender-agent (team team-a)"
        # Dedup key still uses the raw sender, so the poller won't re-wake.
        assert any(key[2].startswith("sender-agent:") for key in bot._notified_inbox_keys)

    async def test_same_team_wake_unchanged(self, config):
        bot = TelegramBot(config, enable_background_poller=False)
        seen: dict = {}

        async def fake_wake(**kwargs):
            seen.update(kwargs)
            return True

        state = SimpleNamespace(route=TelegramRoute(chat_id=1, thread_id=2))
        bot._resolve_team_worker_record = lambda **_: None  # type: ignore[assignment]
        bot._resolve_route_inbox_target = lambda **_: state  # type: ignore[assignment]
        bot._maybe_wake_route_inbox_target = fake_wake  # type: ignore[assignment]
        await bot._handle_inbox_message_notification(
            sender_route=state.route,
            payload={"team_name": "team-a", "recipient": "x", "sender": "s", "sender_team": "team-a"},
        )
        assert seen["sender"] == "s"

    def test_latest_unread_sender_team(self, config, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = TelegramBot(config, enable_background_poller=False)
        path = bot._team_inbox_path("team", "agent")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([
            {"from": "a", "text": "1", "read": True, "from_team": "old"},
            {"from": "b", "text": "2", "read": False, "from_team": "team-z", "timestamp": "2030-01-01T00:00:00Z"},
        ]))
        assert bot._latest_unread_sender_team(team_name="team", agent_name="agent") == "team-z"
        path.write_text(json.dumps([{"from": "b", "text": "2", "read": False}]))
        assert bot._latest_unread_sender_team(team_name="team", agent_name="agent") is None


class TestNewSessionEffort:
    def test_default_is_max_and_env_can_disable(self, monkeypatch):
        assert OBSConfig.__dataclass_fields__["new_session_effort_level"].default == "max"
        monkeypatch.setenv("OBS_NEW_SESSION_EFFORT_LEVEL", "auto")
        assert OBSConfig.from_env().new_session_effort_level is None
        monkeypatch.setenv("OBS_NEW_SESSION_EFFORT_LEVEL", "xhigh")
        assert OBSConfig.from_env().new_session_effort_level == "xhigh"

    def test_new_route_gets_max_restored_route_untouched(self, config):
        bot = TelegramBot(config, enable_background_poller=False)
        bot._config.new_session_effort_level = "max"
        route = TelegramRoute(chat_id=5, thread_id=9)
        state = bot._get_state(route, create=True)
        assert state.session_manager.effort_override == "max"
        # An existing (restored) route keeps whatever it had: _get_state does not touch it.
        state.session_manager.effort_override = None
        again = bot._get_state(route, create=True)
        assert again.session_manager.effort_override is None
