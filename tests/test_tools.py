"""Tests for obs_agent.tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState


def _capture_tools(monkeypatch):
    captured: dict[str, object] = {}

    def fake_create_sdk_mcp_server(name, tools):
        captured["name"] = name
        captured["tools"] = tools
        return {"type": "fake-server", "tools": tools}

    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", fake_create_sdk_mcp_server)
    return captured


def _tool_handler(tools, name: str):
    for tool in tools:
        if tool.name == name:
            return tool.handler
    raise AssertionError(f"tool {name!r} not found")


@pytest.fixture
def skill_vault(tmp_path):
    vault = tmp_path / "vault"
    claude = vault / ".claude"
    (claude / "skills").mkdir(parents=True)
    (claude / "system").mkdir(parents=True)
    (claude / "memory").mkdir(parents=True)
    (vault / "CLAUDE.md").write_text("# OBS Agent\nTest.\n")
    return vault


@pytest.fixture
def skill_config(skill_vault):
    return OBSConfig(vault_path=Path(skill_vault))


class TestForkTaskTool:
    def test_create_obs_tools_registers_fork_task(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        server = create_obs_tools(skill_config, lambda: "sid-123")

        assert server["type"] == "fake-server"
        assert captured["name"] == "obs-agent"
        tool_names = [tool.name for tool in captured["tools"]]
        assert tool_names == [
            "AgentTask",
            "AgentTaskOutput",
            "AgentTaskStop",
            "SendInboxMessage",
            "ReadInbox",
            "ForkTask",
            "ForkTaskOutput",
            "ForkTaskStop",
            "session_info",
            "context_info",
        ]

    @pytest.mark.asyncio
    async def test_fork_task_requires_prompt(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"description": "No prompt"})

        assert result["is_error"] is True
        assert "prompt is required" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_allows_missing_session_id_when_transport_handles_context(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: None, hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fork_task_requires_transport_launcher(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work"})

        assert result["is_error"] is True
        assert "does not provide task orchestration" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_validates_timeout_ms(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work", "timeout_ms": "abc"})

        assert result["is_error"] is True
        assert "timeout_ms must be an integer" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fork_task_accepts_string_run_in_background_true(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work", "run_in_background": "true"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fork_task_launches_via_transport_callback(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={
                "content": [
                    {
                        "type": "text",
                        "text": "ForkTask launched successfully.\nagentId: task-123\noutput_file: /tmp/task-123.jsonl\ntelegram_topic: https://t.me/c/1/2",
                    }
                ]
            }
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler(
            {
                "prompt": "Read the file and report back",
                "description": "Audit",
                "timeout_ms": 5000,
                "max_turns": 12,
            }
        )

        state.fork_task_launcher.assert_awaited_once_with(
            {
                "session_id": "sid-123",
                "prompt": "Read the file and report back",
                "description": "Audit",
                "resume": None,
                "run_in_background": True,
                "timeout_ms": 5000,
                "max_turns": 12,
                "fork": True,
                "team_name": None,
                "agent_name": None,
                "task_tool_name": "ForkTask",
                "tool_use_id": None,
            }
        )
        assert "ForkTask launched successfully." in result["content"][0]["text"]
        assert "agentId: task-123" in result["content"][0]["text"]
        assert "telegram_topic: https://t.me/c/1/2" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_treats_false_resume_as_missing(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work", "resume": "false"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["resume"] is None

    @pytest.mark.asyncio
    async def test_super_task_passes_fork_false(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "fork": False})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["fork"] is False

    @pytest.mark.asyncio
    async def test_super_task_passes_team_fields(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {
                "prompt": "Do work",
                "team_name": "team-alpha",
                "name": "worker-a",
            }
        )

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["team_name"] == "team-alpha"
        assert launch_args["agent_name"] == "worker-a"

    @pytest.mark.asyncio
    async def test_fork_task_validates_max_turns(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work", "max_turns": "abc"})
        assert result["is_error"] is True
        assert "max_turns must be an integer" in result["content"][0]["text"]

        result = await handler({"prompt": "Do work", "max_turns": 0})
        assert result["is_error"] is True
        assert "max_turns must be positive" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fork_task_surfaces_launcher_errors(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()

        async def fail_launcher(args):
            raise RuntimeError("launch exploded")

        state.fork_task_launcher = fail_launcher
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTask")

        result = await handler({"prompt": "Do work"})

        assert result["is_error"] is True
        assert result["content"][0]["text"] == "ForkTask failed: RuntimeError: launch exploded"

    @pytest.mark.asyncio
    async def test_fork_task_output_delegates(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(
            return_value={"content": [{"type": "text", "text": "<retrieval_status>completed</retrieval_status>"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTaskOutput")

        result = await handler({"task_id": "task-123", "block": False, "timeout": 1})

        state.fork_task_outputter.assert_awaited_once_with(
            {"task_id": "task-123", "block": False, "timeout": 1, "tool_use_id": None}
        )
        assert "<retrieval_status>completed</retrieval_status>" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_output_validates_args(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "ForkTaskOutput")

        result = await handler({"task_id": "", "block": False, "timeout": 1})
        assert result["is_error"] is True
        assert "task_id is required" in result["content"][0]["text"]

        result = await handler({"task_id": "task-123", "block": "maybe", "timeout": 1})
        assert result["is_error"] is True
        assert "block must be true or false" in result["content"][0]["text"]

        result = await handler({"task_id": "task-123", "block": False, "timeout": "abc"})
        assert result["is_error"] is True
        assert "timeout must be an integer" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_output_accepts_string_bool(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(
            return_value={"content": [{"type": "text", "text": "<retrieval_status>completed</retrieval_status>"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTaskOutput")

        result = await handler({"task_id": "task-123", "block": "true", "timeout": "1"})

        state.fork_task_outputter.assert_awaited_once_with(
            {"task_id": "task-123", "block": True, "timeout": 1, "tool_use_id": None}
        )
        assert "<retrieval_status>completed</retrieval_status>" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_stop_delegates(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_stopper = AsyncMock(
            return_value={"content": [{"type": "text", "text": "{\"task_id\":\"task-123\"}"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTaskStop")

        result = await handler({"task_id": "task-123"})

        state.fork_task_stopper.assert_awaited_once_with(
            {"task_id": "task-123", "tool_use_id": None}
        )
        assert "\"task_id\":\"task-123\"" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_stop_accepts_shell_id_alias(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_stopper = AsyncMock(
            return_value={"content": [{"type": "text", "text": "{\"task_id\":\"task-123\"}"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "ForkTaskStop")

        result = await handler({"shell_id": "task-123"})

        state.fork_task_stopper.assert_awaited_once_with(
            {"task_id": "task-123", "tool_use_id": None}
        )
        assert "\"task_id\":\"task-123\"" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_fork_task_stop_requires_task_id(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "ForkTaskStop")

        result = await handler({})

        assert result["is_error"] is True
        assert "task_id is required" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_send_inbox_message_and_read_inbox(self, monkeypatch, skill_config, tmp_path):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")
        read_handler = _tool_handler(captured["tools"], "ReadInbox")

        send_result = await send_handler(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "content": "hello team",
                "summary": "greeting",
                "sender": "lead",
            }
        )
        assert json.loads(send_result["content"][0]["text"])["success"] is True

        read_result = await read_handler(
            {
                "team_name": "team-alpha",
                "agent": "worker-a",
                "mark_read": True,
            }
        )
        payload = json.loads(read_result["content"][0]["text"])
        assert payload["count"] == 1
        assert payload["messages"][0]["text"] == "hello team"
        assert payload["messages"][0]["from"] == "lead"

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[0]["read"] is True

    @pytest.mark.asyncio
    async def test_send_inbox_message_notifies_transport_hook(self, monkeypatch, skill_config, tmp_path):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        state = HookState()
        state.inbox_message_notifier = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        result = await send_handler(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "content": "hello team",
                "summary": "greeting",
                "sender": "lead",
            }
        )

        assert json.loads(result["content"][0]["text"])["success"] is True
        state.inbox_message_notifier.assert_awaited_once_with(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "sender": "lead",
                "content": "hello team",
                "summary": "greeting",
            }
        )

    @pytest.mark.asyncio
    async def test_send_inbox_message_concurrent_writes_are_not_lost(self, monkeypatch, skill_config, tmp_path):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")
        read_handler = _tool_handler(captured["tools"], "ReadInbox")

        async def _send(idx: int):
            return await send_handler(
                {
                    "team_name": "team-alpha",
                    "recipient": "worker-a",
                    "content": f"m-{idx}",
                    "sender": f"s-{idx}",
                }
            )

        await asyncio.gather(*[_send(i) for i in range(40)])

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert len(persisted) == 40
        assert all(isinstance(item, dict) for item in persisted)

        read_result = await read_handler(
            {
                "team_name": "team-alpha",
                "agent": "worker-a",
                "include_read": True,
                "mark_read": False,
                "limit": 100,
            }
        )
        payload = json.loads(read_result["content"][0]["text"])
        assert payload["count"] == 40
