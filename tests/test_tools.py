"""Tests for obs_agent.tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.lineage import ObsBootstrap


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
    vault = Path(skill_vault)
    return OBSConfig(vault_path=vault, team_storage_root=vault / ".claude" / "teams")


class TestAgentTaskTools:
    def test_create_obs_tools_registers_tools(self, monkeypatch, skill_config):
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
            "CronCreate",
            "CronList",
            "CronDelete",
            "SendInboxMessage",
            "ReadInbox",
            "session_info",
            "context_info",
            "session_lineage",
            "search_team",
            "PlaceholderTool",
        ]
        # ForkTask tools should NOT be registered (retired)
        assert "ForkTask" not in tool_names
        assert "ForkTaskOutput" not in tool_names
        assert "ForkTaskStop" not in tool_names

    def test_send_inbox_message_schema_marks_only_recipient_and_content_required_with_optional_reply_flags(
        self,
        monkeypatch,
        skill_config,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "SendInboxMessage")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["required"] == ["recipient", "content"]
        assert "team_name" in schema["properties"]
        assert "sender" in schema["properties"]
        assert "needs_reply" in schema["properties"]
        assert "question" in schema["properties"]["needs_reply"]["description"].lower()
        assert "must_reply" in schema["properties"]

    @pytest.mark.asyncio
    async def test_send_inbox_message_accepts_backend_needs_reply_arg(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        result = await send_handler(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "content": "please answer",
                "sender": "lead",
                "needs_reply": True,
            }
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[-1]["must_reply"] is True
        assert persisted[-1]["replied"] is False

    @pytest.mark.asyncio
    async def test_send_inbox_message_accepts_legacy_must_reply_backend_arg(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        result = await send_handler(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "content": "please answer",
                "sender": "lead",
                "must_reply": True,
            }
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[-1]["must_reply"] is True
        assert persisted[-1]["replied"] is False

    def test_read_inbox_schema_has_no_required_fields(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "ReadInbox")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["required"] == []
        assert "team_name" in schema["properties"]
        assert "agent" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_agent_task_schema_allows_prompt_and_prompt_file_together(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "AgentTask")
        prompt_description = tool.input_schema["properties"]["prompt"]["description"]
        prompt_file_description = tool.input_schema["properties"]["prompt_file"]["description"]
        assert "Mutually exclusive" not in prompt_description
        assert "Mutually exclusive" not in prompt_file_description
        assert "May be combined" in prompt_description
        assert "May be combined" in prompt_file_description

    def test_agent_task_schema_includes_session_source(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "AgentTask")
        schema = tool.input_schema
        assert "session_source" in schema["properties"]
        assert "JSONL file path" in schema["properties"]["session_source"]["description"]
        assert "session_source" not in schema["required"]

    @pytest.mark.asyncio
    async def test_agent_task_passes_session_source_to_transport(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "session_source": "source-session"})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["session_source"] == "source-session"
        assert launch_args["fork"] is True

    @pytest.mark.asyncio
    async def test_agent_task_rejects_session_source_with_resume(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {"prompt": "Do work", "resume": "agent-1", "session_source": "source-session"}
        )

        assert result["is_error"] is True
        assert "resume and session_source are mutually exclusive" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_task_rejects_session_source_with_fork_false(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {"prompt": "Do work", "fork": False, "session_source": "source-session"}
        )

        assert result["is_error"] is True
        assert "session_source is only supported with fork=true" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_task_requires_prompt(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"description": "No prompt"})

        assert result["is_error"] is True
        assert "prompt" in result["content"][0]["text"] and "required" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_allows_missing_session_id_when_transport_handles_context(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: None, hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == "Do work"
        assert "prompt_file" not in launch_args
        assert "prompt_file_content" not in launch_args

    @pytest.mark.asyncio
    async def test_agent_task_requires_transport_launcher(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work"})

        assert result["is_error"] is True
        assert "does not provide task orchestration" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_validates_timeout_ms(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "timeout_ms": "abc"})

        assert result["is_error"] is True
        assert "timeout_ms must be an integer" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_task_accepts_string_run_in_background_true(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "run_in_background": "true"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_task_launches_via_transport_callback(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={
                "content": [
                    {
                        "type": "text",
                        "text": "AgentTask launched.\nagentId: task-123\noutput_file: /tmp/task-123.jsonl\ntelegram_topic: https://t.me/c/1/2",
                    }
                ]
            }
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {
                "prompt": "Read the file and report back",
                "description": "Audit",
                "timeout_ms": 5000,
                "max_turns": 12,
                "fork": "true",
            }
        )

        state.fork_task_launcher.assert_awaited_once_with(
            {
                "session_id": "sid-123",
                "prompt": "Read the file and report back",
                "description": "Audit",
                "resume": None,
                "session_source": None,
                "run_in_background": True,
                "timeout_ms": 5000,
                "max_turns": 12,
                "fork": True,
                "model": None,
                "team_name": None,
                "agent_name": None,
                "task_tool_name": "AgentTask",
                "tool_use_id": None,
                "inherit_schedules": True,
                "env": None,
                "temperature": None,
                "hooks": None,
                "inherit_hooks": False,
            }
        )
        assert "AgentTask launched." in result["content"][0]["text"]
        assert "agentId: task-123" in result["content"][0]["text"]
        assert "telegram_topic: https://t.me/c/1/2" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_alias_maps_to_transport_description(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {
                "prompt": "Do work",
                "alias": "child-researcher",
                "fork": True,
            }
        )

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["description"] == "child-researcher"

    @pytest.mark.asyncio
    async def test_agent_task_treats_false_resume_as_missing(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

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
        # name param is now display name (lineage), not agent_name (per naming redesign)
        assert launch_args["agent_name"] is None
        assert launch_args["description"] == "worker-a"

    @pytest.mark.asyncio
    async def test_agent_task_validates_max_turns(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "max_turns": "abc"})
        assert result["is_error"] is True
        assert "max_turns must be an integer" in result["content"][0]["text"]

        result = await handler({"prompt": "Do work", "max_turns": 0})
        assert result["is_error"] is True
        assert "max_turns must be positive" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_task_rejects_cross_model_fork(self, monkeypatch, skill_config):
        """fork=true with an explicit non-inherit model must be rejected."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "fork": True, "model": "gpt-5.5"})

        assert result["is_error"] is True
        assert "cross-model forking is not supported" in result["content"][0]["text"]
        assert "fork=false" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_task_allows_fork_with_inherit_model(self, monkeypatch, skill_config):
        """fork=true with model='inherit' must be allowed (same as omitting model)."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "fork": True, "model": "inherit"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["model"] is None
        assert launch_args["fork"] is True

    @pytest.mark.asyncio
    async def test_agent_task_allows_fork_with_no_model(self, monkeypatch, skill_config):
        """fork=true with model omitted must be allowed (inherits parent model)."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "fork": True})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["model"] is None
        assert launch_args["fork"] is True

    @pytest.mark.asyncio
    async def test_agent_task_allows_different_model_with_fork_false(self, monkeypatch, skill_config):
        """fork=false with any model must be allowed."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "fork": False, "model": "gpt-5.5"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["model"] == "gpt-5.5"
        assert launch_args["fork"] is False

    @pytest.mark.asyncio
    async def test_agent_task_surfaces_launcher_errors(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()

        async def fail_launcher(args):
            raise RuntimeError("launch exploded")

        state.fork_task_launcher = fail_launcher
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work"})

        assert result["is_error"] is True
        assert result["content"][0]["text"] == "AgentTask failed: RuntimeError: launch exploded"

    @pytest.mark.asyncio
    async def test_agent_task_output_delegates(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(
            return_value={"content": [{"type": "text", "text": "<retrieval_status>completed</retrieval_status>"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

        result = await handler({"task_id": "task-123", "block": False, "timeout": 1})

        state.fork_task_outputter.assert_awaited_once_with(
            {"task_id": "task-123", "block": False, "timeout": 1, "tool_use_id": None}
        )
        assert "<retrieval_status>completed</retrieval_status>" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_output_validates_args(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

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
    async def test_agent_task_output_accepts_string_bool(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(
            return_value={"content": [{"type": "text", "text": "<retrieval_status>completed</retrieval_status>"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

        result = await handler({"task_id": "task-123", "block": "true", "timeout": "1"})

        state.fork_task_outputter.assert_awaited_once_with(
            {"task_id": "task-123", "block": True, "timeout": 1, "tool_use_id": None}
        )
        assert "<retrieval_status>completed</retrieval_status>" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_stop_delegates(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_stopper = AsyncMock(
            return_value={"content": [{"type": "text", "text": "{\"task_id\":\"task-123\"}"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

        result = await handler({"task_id": "task-123"})

        state.fork_task_stopper.assert_awaited_once_with(
            {"task_id": "task-123", "tool_use_id": None}
        )
        assert "\"task_id\":\"task-123\"" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_stop_accepts_shell_id_alias(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_stopper = AsyncMock(
            return_value={"content": [{"type": "text", "text": "{\"task_id\":\"task-123\"}"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

        result = await handler({"shell_id": "task-123"})

        state.fork_task_stopper.assert_awaited_once_with(
            {"task_id": "task-123", "tool_use_id": None}
        )
        assert "\"task_id\":\"task-123\"" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_stop_requires_task_id(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

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

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[0]["read"] is True

    @pytest.mark.asyncio
    async def test_inbox_tools_infer_current_team_and_agent(self, monkeypatch, skill_config, tmp_path):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='1' />",
                lineage=("Root", "Child"),
                origin="agent_task_fresh",
                is_fork=False,
                session_id="sid-123",
                agent_id="task-123",
                parent_session_id="sid-parent",
                root_team_key="obs-tree-root-123",
                agent_name="obs-agent-child-123",
                parent_agent_name="obs-tree-root-123",
                parent_display_name="Root",
            ),
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")
        read_handler = _tool_handler(captured["tools"], "ReadInbox")

        send_result = await send_handler(
            {
                "recipient": "obs-agent-peer-999",
                "content": "hello inferred team",
            }
        )
        assert json.loads(send_result["content"][0]["text"])["success"] is True

        inbox_path = (
            skill_config.team_storage_root
            / "obs-tree-root-123"
            / "inboxes"
            / "obs-agent-peer-999.json"
        )
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[0]["from"] == "obs-agent-child-123"

        self_inbox = (
            skill_config.team_storage_root
            / "obs-tree-root-123"
            / "inboxes"
            / "obs-agent-child-123.json"
        )
        self_inbox.parent.mkdir(parents=True, exist_ok=True)
        self_inbox.write_text(
            json.dumps(
                [
                    {
                        "from": "obs-agent-peer-999",
                        "text": "reply payload",
                        "summary": "reply",
                        "timestamp": "2026-03-14T00:00:00Z",
                        "read": False,
                    }
                ]
            ),
            encoding="utf-8",
        )
        read_result = await read_handler({})
        payload = json.loads(read_result["content"][0]["text"])
        assert payload["team_name"] == "obs-tree-root-123"
        assert payload["agent"] == "obs-agent-child-123"
        assert payload["count"] == 1

    @pytest.mark.asyncio
    async def test_session_lineage_prefers_pending_child_bootstrap_over_stale_fork_parent_bootstrap(
        self,
        monkeypatch,
        skill_config,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        pending_child_bootstrap = (
            "<obs-bootstrap version='2'>"
            "<obs-lineage>"
            "<obs-node display_name='Root' agent_name='2026-03-31-10-00-root' />"
            "<obs-node display_name='Alpha' agent_name='aaaaaaaaaa-alpha' />"
            "</obs-lineage>"
            "<fork_context><origin>agent_task_fork</origin><is_fork>true</is_fork>"
            "<session_id>sid-child</session_id><parent_session_id>sid-root</parent_session_id>"
            "</fork_context>"
            "<team_context>"
            "<root_team_key>2026-03-31-10-00-root</root_team_key>"
            "<agent_name>aaaaaaaaaa-alpha</agent_name>"
            "<parent_agent_name>2026-03-31-10-00-root</parent_agent_name>"
            "<parent_display_name>Root</parent_display_name>"
            "</team_context>"
            "</obs-bootstrap>"
        )
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2'><obs-lineage><obs-node display_name='Root' agent_name='2026-03-31-10-00-root' /></obs-lineage><fork_context><origin>trunk_start</origin><is_fork>false</is_fork><session_id>sid-child</session_id></fork_context><team_context><root_team_key>2026-03-31-10-00-root</root_team_key><agent_name>2026-03-31-10-00-root</agent_name></team_context></obs-bootstrap>",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-child",
                agent_id=None,
                parent_session_id=None,
                root_team_key="2026-03-31-10-00-root",
                agent_name="2026-03-31-10-00-root",
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        hook_state = HookState()
        hook_state.pending_obs_bootstrap_xml = pending_child_bootstrap
        create_obs_tools(skill_config, lambda: "sid-child", hook_state=hook_state)
        handler = _tool_handler(captured["tools"], "session_lineage")

        result = await handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["lineage"] == ["Root", "Alpha"]
        assert payload["lineage_length"] == 2
        assert payload["agent_name"] == "aaaaaaaaaa-alpha"
        assert payload["parent_agent_name"] == "2026-03-31-10-00-root"

    @pytest.mark.asyncio
    async def test_send_inbox_message_uses_pending_child_bootstrap_for_default_sender(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        pending_child_bootstrap = (
            "<obs-bootstrap version='2'>"
            "<obs-lineage>"
            "<obs-node display_name='Root' agent_name='2026-03-31-10-00-root' />"
            "<obs-node display_name='Alpha' agent_name='aaaaaaaaaa-alpha' />"
            "</obs-lineage>"
            "<fork_context><origin>agent_task_fork</origin><is_fork>true</is_fork>"
            "<session_id>sid-child</session_id><parent_session_id>sid-root</parent_session_id>"
            "</fork_context>"
            "<team_context>"
            "<root_team_key>2026-03-31-10-00-root</root_team_key>"
            "<agent_name>aaaaaaaaaa-alpha</agent_name>"
            "<parent_agent_name>2026-03-31-10-00-root</parent_agent_name>"
            "<parent_display_name>Root</parent_display_name>"
            "</team_context>"
            "</obs-bootstrap>"
        )
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2'><obs-lineage><obs-node display_name='Root' agent_name='2026-03-31-10-00-root' /></obs-lineage><fork_context><origin>trunk_start</origin><is_fork>false</is_fork><session_id>sid-child</session_id></fork_context><team_context><root_team_key>2026-03-31-10-00-root</root_team_key><agent_name>2026-03-31-10-00-root</agent_name></team_context></obs-bootstrap>",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-child",
                agent_id=None,
                parent_session_id=None,
                root_team_key="2026-03-31-10-00-root",
                agent_name="2026-03-31-10-00-root",
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        hook_state = HookState()
        hook_state.pending_obs_bootstrap_xml = pending_child_bootstrap
        create_obs_tools(skill_config, lambda: "sid-child", hook_state=hook_state)
        handler = _tool_handler(captured["tools"], "SendInboxMessage")

        result = await handler({"recipient": "2026-03-31-10-00-root", "content": "hello parent"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True

        inbox_path = (
            skill_config.team_storage_root
            / "2026-03-31-10-00-root"
            / "inboxes"
            / "2026-03-31-10-00-root.json"
        )
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[-1]["from"] == "aaaaaaaaaa-alpha"

    @pytest.mark.asyncio
    async def test_send_inbox_message_needs_reply_false_wins_over_legacy_must_reply_true(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        result = await send_handler(
            {
                "team_name": "team-alpha",
                "recipient": "worker-a",
                "content": "hello team",
                "sender": "lead",
                "needs_reply": False,
                "must_reply": True,
            }
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert "must_reply" not in persisted[-1]
        assert "replied" not in persisted[-1]

    @pytest.mark.asyncio
    async def test_session_lineage_returns_current_bootstrap(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='1'><obs-lineage><obs-node name='Root' /></obs-lineage></obs-bootstrap>",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-123",
                agent_id=None,
                parent_session_id=None,
                root_team_key="obs-tree-root-123",
                agent_name="obs-agent-root-123",
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "session_lineage")

        result = await handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["lineage"] == ["Root"]
        assert payload["lineage_length"] == 1
        assert payload["origin"] == "trunk_start"
        assert payload["root_team_key"] == "obs-tree-root-123"
        assert "xml" not in payload
        assert payload["agent_names"] == ["obs-tree-root-123"]

        result_with_xml = await handler({"include_xml": True})
        payload_with_xml = json.loads(result_with_xml["content"][0]["text"])
        assert payload_with_xml["xml"].startswith("<obs-bootstrap")

    @pytest.mark.asyncio
    async def test_session_lineage_falls_back_to_current_session_id_when_bootstrap_omits_it(
        self,
        monkeypatch,
        skill_config,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='1'><obs-lineage><obs-node name='Root' /></obs-lineage></obs-bootstrap>",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id=None,
                agent_id=None,
                parent_session_id=None,
                root_team_key="obs-tree-root-123",
                agent_name="obs-agent-root-123",
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        create_obs_tools(skill_config, lambda: "sid-live", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "session_lineage")

        result = await handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["session_id"] == "sid-live"

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
                "_direct_send": True,
            }
        )

    @pytest.mark.asyncio
    async def test_send_inbox_message_reports_underdelivered_when_recipient_is_unbound(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        state = HookState()
        state.inbox_recipient_validator = AsyncMock(
            return_value={
                "deliverable": False,
                "reason": "recipient was deleted",
            }
        )
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

        assert result["is_error"] is True
        assert "underdelivered" in result["content"][0]["text"]
        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        assert not inbox_path.exists()

    @pytest.mark.asyncio
    async def test_send_inbox_message_creates_inbox_when_transport_confirms_binding(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        state = HookState()
        state.inbox_recipient_validator = AsyncMock(return_value={"deliverable": True})
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

        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True
        assert payload["delivered"] is True
        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        assert inbox_path.exists()

    @pytest.mark.asyncio
    async def test_send_inbox_message_rolls_back_when_notifier_reports_underdelivery(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        state = HookState()
        state.inbox_recipient_validator = AsyncMock(return_value={"deliverable": True})
        state.inbox_message_notifier = AsyncMock(
            return_value={"delivered": False, "reason": "recipient topic was deleted"}
        )
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

        assert result["is_error"] is True
        assert "underdelivered" in result["content"][0]["text"]
        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        assert not inbox_path.exists()

    @pytest.mark.asyncio
    async def test_send_inbox_message_returns_underdelivered_when_notifier_raises(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        state = HookState()
        state.inbox_recipient_validator = AsyncMock(return_value={"deliverable": True})
        state.inbox_message_notifier = AsyncMock(side_effect=RuntimeError("wake failed"))
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

        assert result["is_error"] is True
        assert "underdelivered" in result["content"][0]["text"]
        payload = result["tool_use_result"]
        assert payload["success"] is False
        assert payload["delivered"] is False
        assert payload["reason"] == "recipient wake failed"
        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        assert not inbox_path.exists()

    @pytest.mark.asyncio
    async def test_send_inbox_message_resolves_direct_child_alias_only(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-root",
                agent_id=None,
                parent_session_id=None,
                root_team_key="2026-03-30-10-10-root",
                agent_name="2026-03-30-10-10-root",
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        create_obs_tools(skill_config, lambda: "sid-root", hook_state=HookState())
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        child_name = "e96857c58f-alpha-child"
        child_path = skill_config.team_storage_root / "2026-03-30-10-10-root" / "inboxes" / f"{child_name}.json"
        child_path.parent.mkdir(parents=True, exist_ok=True)
        child_path.write_text("[]", encoding="utf-8")

        result = await send_handler(
            {
                "recipient": "Alpha Child",
                "content": "ping",
            }
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True
        assert payload["recipient"] == child_name
        persisted = json.loads(child_path.read_text(encoding="utf-8"))
        assert persisted[-1]["text"] == "ping"

    @pytest.mark.asyncio
    async def test_search_team_children_sort_by_activity_and_accept_tree_children_alias(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        root_agent = "2026-03-30-10-10-root"
        newer_child = "e96857c58f-newer"
        older_child = "e96857c58f-older"
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-root",
                agent_id=None,
                parent_session_id=None,
                root_team_key=root_agent,
                agent_name=root_agent,
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        team_dir = skill_config.team_storage_root / root_agent
        inbox_dir = team_dir / "inboxes"
        inbox_dir.mkdir(parents=True)
        for agent_name in (root_agent, older_child, newer_child):
            (inbox_dir / f"{agent_name}.json").write_text("[]", encoding="utf-8")
        (team_dir / "config.json").write_text(
            json.dumps(
                {
                    "members": [
                        {"name": root_agent, "obs": {"display_name": "Root", "lineage": ["Root"], "lineage_length": 1, "updated_at": 1}},
                        {"name": older_child, "obs": {"display_name": "Older", "lineage": ["Root", "Older"], "lineage_length": 2, "parent_agent_name": root_agent, "created_at": 10}},
                        {"name": newer_child, "obs": {"display_name": "Newer", "lineage": ["Root", "Newer"], "lineage_length": 2, "parent_agent_name": root_agent, "created_at": 20}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        create_obs_tools(skill_config, lambda: "sid-root", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "tree_children"})

        payload = json.loads(result["content"][0]["text"])
        assert payload["mode"] == "children"
        assert payload["children"] == [newer_child, older_child]

    @pytest.mark.asyncio
    async def test_search_team_tree_applies_limit_before_runtime_status_lookup(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        root_agent = "2026-03-30-10-10-root"
        child_a = "e96857c58f-a"
        child_b = "e96857c58f-b"
        child_c = "e96857c58f-c"
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-root",
                agent_id=None,
                parent_session_id=None,
                root_team_key=root_agent,
                agent_name=root_agent,
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        team_dir = skill_config.team_storage_root / root_agent
        inbox_dir = team_dir / "inboxes"
        inbox_dir.mkdir(parents=True)
        for agent_name in (root_agent, child_a, child_b, child_c):
            (inbox_dir / f"{agent_name}.json").write_text("[]", encoding="utf-8")
        (team_dir / "config.json").write_text(
            json.dumps(
                {
                    "members": [
                        {"name": root_agent, "agentType": "general-purpose", "model": "claude-opus-4-7", "obs": {"display_name": "Root", "lineage": ["Root"], "lineage_length": 1, "updated_at": 1}},
                        {"name": child_a, "agentType": "general-purpose", "model": "claude-haiku-4-5", "obs": {"display_name": "A", "status": "completed", "lineage": ["Root", "A"], "lineage_length": 2, "parent_agent_name": root_agent, "created_at": 30}},
                        {"name": child_b, "agentType": "general-purpose", "model": "claude-sonnet-4-6", "obs": {"display_name": "B", "status": "failed", "lineage": ["Root", "B"], "lineage_length": 2, "parent_agent_name": root_agent, "created_at": 20}},
                        {"name": child_c, "agentType": "general-purpose", "model": "claude-sonnet-4-6", "obs": {"display_name": "C", "status": "failed", "lineage": ["Root", "C"], "lineage_length": 2, "parent_agent_name": root_agent, "created_at": 10}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        state = HookState()
        status_lookups = []

        async def status_provider(payload):
            status_lookups.append(payload["agent_name"])
            if payload["agent_name"] == child_a:
                return {"running": True, "status": "running", "model": "claude-haiku-4-5", "last_active_at": 40}
            return {"running": False}

        state.team_status_provider = status_provider
        create_obs_tools(skill_config, lambda: "sid-root", hook_state=state)
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "tree", "limit": 2})

        payload = json.loads(result["content"][0]["text"])
        assert payload["limit"] == 2
        assert payload["tree"] == [child_a, child_b]
        assert [member["agent_name"] for member in payload["tree_members"]] == [child_a, child_b]
        assert status_lookups == [child_a, child_b]
        assert payload["omitted"] == 2
        assert payload["tree_members"][0]["running"] is True
        assert payload["tree_members"][0]["status"] == "running"
        assert payload["tree_members"][0]["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_search_team_family_member_metadata_respects_team_name_limit_and_status(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        default_root = "2026-03-30-10-10-default-root"
        requested_team = "team-beta"
        child_a = "e96857c58f-a"
        child_b = "e96857c58f-b"
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root",),
                origin="trunk_start",
                is_fork=False,
                session_id="sid-root",
                agent_id=None,
                parent_session_id=None,
                root_team_key=default_root,
                agent_name=default_root,
                parent_agent_name=None,
                parent_display_name=None,
            ),
        )
        requested_team_dir = skill_config.team_storage_root / requested_team
        requested_inbox_dir = requested_team_dir / "inboxes"
        requested_inbox_dir.mkdir(parents=True)
        for agent_name in (default_root, child_a, child_b):
            (requested_inbox_dir / f"{agent_name}.json").write_text("[]", encoding="utf-8")
        (requested_team_dir / "config.json").write_text(
            json.dumps(
                {
                    "members": [
                        {"name": default_root, "obs": {"display_name": "Root", "lineage": ["Root"], "lineage_length": 1, "created_at": 1}},
                        {"name": child_a, "agentType": "general-purpose", "obs": {"display_name": "A", "lineage": ["Root", "A"], "lineage_length": 2, "parent_agent_name": default_root, "created_at": 30}},
                        {"name": child_b, "agentType": "general-purpose", "obs": {"display_name": "B", "lineage": ["Root", "B"], "lineage_length": 2, "parent_agent_name": default_root, "created_at": 20}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        state = HookState()

        async def status_provider(payload):
            assert payload["team_name"] == requested_team
            if payload["agent_name"] == child_a:
                return {"running": True, "status": "running", "model": "claude-sonnet-4-6", "last_active_at": 40}
            return {"running": False}

        state.team_status_provider = status_provider
        create_obs_tools(skill_config, lambda: "sid-root", hook_state=state)
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "family", "team_name": requested_team, "limit": 1})

        payload = json.loads(result["content"][0]["text"])
        assert payload["team_name"] == requested_team
        assert payload["limit"] == 1
        assert payload["children"] == [child_a]
        assert [member["agent_name"] for member in payload["children_members"]] == [child_a]
        assert payload["children_members"][0]["running"] is True
        assert payload["children_members"][0]["status"] == "running"
        assert payload["children_members"][0]["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_send_inbox_message_does_not_resolve_parent_alias(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root", "Child"),
                origin="agent_task_fresh",
                is_fork=False,
                session_id="sid-child",
                agent_id=None,
                parent_session_id="sid-root",
                root_team_key="2026-03-30-10-10-root",
                agent_name="e96857c58f-child",
                parent_agent_name="2026-03-30-10-10-root",
                parent_display_name="Root",
            ),
        )
        state = HookState()
        state.inbox_recipient_validator = AsyncMock(
            return_value={
                "deliverable": False,
                "reason": "recipient has no current route binding",
            }
        )
        create_obs_tools(skill_config, lambda: "sid-child", hook_state=state)
        send_handler = _tool_handler(captured["tools"], "SendInboxMessage")

        root_path = skill_config.team_storage_root / "2026-03-30-10-10-root" / "inboxes" / "2026-03-30-10-10-root.json"
        root_path.parent.mkdir(parents=True, exist_ok=True)
        root_path.write_text("[]", encoding="utf-8")

        result = await send_handler(
            {
                "recipient": "Root",
                "content": "ping-parent",
            }
        )

        assert result["is_error"] is True
        assert "underdelivered" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_read_inbox_marks_only_returned_messages_as_read(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        read_handler = _tool_handler(captured["tools"], "ReadInbox")

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(
            json.dumps(
                [
                    {"from": "s-1", "text": "m-1", "summary": "", "timestamp": "2026-03-30T00:00:00Z", "read": False},
                    {"from": "s-2", "text": "m-2", "summary": "", "timestamp": "2026-03-30T00:00:01Z", "read": False},
                    {"from": "s-3", "text": "m-3", "summary": "", "timestamp": "2026-03-30T00:00:02Z", "read": False},
                ]
            ),
            encoding="utf-8",
        )

        result = await read_handler(
            {
                "team_name": "team-alpha",
                "agent": "worker-a",
                "mark_read": True,
                "limit": 1,
            }
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["count"] == 1
        assert payload["messages"][0]["text"] == "m-3"
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert [item["read"] for item in persisted] == [False, False, True]

    @pytest.mark.asyncio
    async def test_search_team_reports_family_and_tree(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: ObsBootstrap(
                raw_xml="<obs-bootstrap version='2' />",
                lineage=("Root", "Branch", "Leaf"),
                origin="agent_task_fresh",
                is_fork=False,
                session_id="sid-leaf",
                agent_id=None,
                parent_session_id="sid-branch",
                root_team_key="2026-03-30-10-10-root",
                agent_name="8fb0d4bb4f-leaf",
                parent_agent_name="e96857c58f-branch",
                parent_display_name="Branch",
            ),
        )
        create_obs_tools(skill_config, lambda: "sid-leaf", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        inboxes = skill_config.team_storage_root / "2026-03-30-10-10-root" / "inboxes"
        inboxes.mkdir(parents=True, exist_ok=True)
        for name in [
            "2026-03-30-10-10-root",
            "e96857c58f-branch",
            "8fb0d4bb4f-leaf",
            "8fb0d4bb4f-sibling",
            "83a3e652c4-child-a",
            "83a3e652c4-child-b",
            "aaaaaaaaaa-cousin",
        ]:
            (inboxes / f"{name}.json").write_text("[]", encoding="utf-8")
        team_config = skill_config.team_storage_root / "2026-03-30-10-10-root" / "config.json"
        team_config.write_text(
            json.dumps(
                {
                    "members": [
                        {
                            "agentId": "2026-03-30-10-10-root@2026-03-30-10-10-root",
                            "name": "2026-03-30-10-10-root",
                            "obs": {
                                "lineage": ["Root"],
                                "display_name": "Root",
                                "lineage_length": 1,
                            },
                        },
                        {
                            "agentId": "e96857c58f-branch@2026-03-30-10-10-root",
                            "name": "e96857c58f-branch",
                            "obs": {
                                "lineage": ["Root", "Branch"],
                                "display_name": "Branch",
                                "parent_agent_name": "2026-03-30-10-10-root",
                                "parent_display_name": "Root",
                                "lineage_length": 2,
                            },
                        },
                        {
                            "agentId": "8fb0d4bb4f-leaf@2026-03-30-10-10-root",
                            "name": "8fb0d4bb4f-leaf",
                            "obs": {
                                "lineage": ["Root", "Branch", "Leaf"],
                                "display_name": "Leaf",
                                "parent_agent_name": "e96857c58f-branch",
                                "parent_display_name": "Branch",
                                "lineage_length": 3,
                            },
                        },
                        {
                            "agentId": "8fb0d4bb4f-sibling@2026-03-30-10-10-root",
                            "name": "8fb0d4bb4f-sibling",
                            "obs": {
                                "lineage": ["Root", "Branch", "Sibling"],
                                "display_name": "Sibling",
                                "parent_agent_name": "e96857c58f-branch",
                                "parent_display_name": "Branch",
                                "lineage_length": 3,
                            },
                        },
                        {
                            "agentId": "83a3e652c4-child-a@2026-03-30-10-10-root",
                            "name": "83a3e652c4-child-a",
                            "obs": {
                                "lineage": ["Root", "Branch", "Leaf", "Child A"],
                                "display_name": "Child A",
                                "parent_agent_name": "8fb0d4bb4f-leaf",
                                "parent_display_name": "Leaf",
                                "lineage_length": 4,
                            },
                        },
                        {
                            "agentId": "83a3e652c4-child-b@2026-03-30-10-10-root",
                            "name": "83a3e652c4-child-b",
                            "obs": {
                                "lineage": ["Root", "Branch", "Leaf", "Child B"],
                                "display_name": "Child B",
                                "parent_agent_name": "8fb0d4bb4f-leaf",
                                "parent_display_name": "Leaf",
                                "lineage_length": 4,
                            },
                        },
                        {
                            "agentId": "aaaaaaaaaa-cousin@2026-03-30-10-10-root",
                            "name": "aaaaaaaaaa-cousin",
                            "obs": {
                                "lineage": ["Root", "Other Branch", "Cousin"],
                                "display_name": "Cousin",
                                "parent_agent_name": "bbbbbbbbbb-other-branch",
                                "parent_display_name": "Other Branch",
                                "lineage_length": 3,
                            },
                        },
                    ]
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        family = await handler({"mode": "family"})
        family_payload = json.loads(family["content"][0]["text"])
        assert family_payload["parent"] == "e96857c58f-branch"
        assert family_payload["siblings"] == ["8fb0d4bb4f-sibling"]
        assert family_payload["children"] == ["83a3e652c4-child-a", "83a3e652c4-child-b"]

        ancestors = await handler({"mode": "ancestors"})
        ancestors_payload = json.loads(ancestors["content"][0]["text"])
        assert ancestors_payload["ancestors"] == [
            "2026-03-30-10-10-root",
            "e96857c58f-branch",
        ]

        descendants = await handler({"mode": "descendants"})
        descendants_payload = json.loads(descendants["content"][0]["text"])
        assert descendants_payload["descendants"] == [
            "83a3e652c4-child-a",
            "83a3e652c4-child-b",
        ]

        tree = await handler({"mode": "tree"})
        tree_payload = json.loads(tree["content"][0]["text"])
        assert tree_payload["tree"] == sorted(
            [
                "2026-03-30-10-10-root",
                "e96857c58f-branch",
                "8fb0d4bb4f-leaf",
                "8fb0d4bb4f-sibling",
                "83a3e652c4-child-a",
                "83a3e652c4-child-b",
                "aaaaaaaaaa-cousin",
            ]
        )
        tree_members = tree_payload["tree_members"]
        root_member = next(item for item in tree_members if item["agent_name"] == "2026-03-30-10-10-root")
        assert root_member["display_name"] == "Root"
        assert root_member["lineage"] == ["Root"]
        leaf_member = next(item for item in tree_members if item["agent_name"] == "8fb0d4bb4f-leaf")
        assert leaf_member["relation"] == "self"
        assert leaf_member["display_name"] == "Leaf"
        assert leaf_member["parent_agent_name"] == "e96857c58f-branch"
        assert leaf_member["parent_display_name"] == "Branch"
        sibling_member = next(item for item in tree_members if item["agent_name"] == "8fb0d4bb4f-sibling")
        assert sibling_member["relation"] == "sibling"
        child_member = next(item for item in tree_members if item["agent_name"] == "83a3e652c4-child-a")
        assert child_member["relation"] == "child"
        cousin_member = next(item for item in tree_members if item["agent_name"] == "aaaaaaaaaa-cousin")
        assert cousin_member["relation"] == "tree"

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

        inbox_path = skill_config.team_storage_root / "team-alpha" / "inboxes" / "worker-a.json"
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


class TestCronTools:
    @pytest.mark.asyncio
    async def test_cron_create_requires_cron_and_prompt(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "CronCreate")

        missing_cron = await handler({"schedule_mode": "cron", "prompt": "hello"})
        assert missing_cron["is_error"] is True
        assert "cron is required" in missing_cron["content"][0]["text"]

        missing_prompt = await handler({"schedule_mode": "cron", "cron": "*/2 * * * *"})
        assert missing_prompt["is_error"] is True
        assert "prompt is required" in missing_prompt["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_cron_create_validates_fields(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.cron_creator = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "CronCreate")

        bad_interval = await handler(
            {"cron": "*/2 * * * *", "prompt": "hi", "interval_seconds": -1}
        )
        assert bad_interval["is_error"] is True
        assert "interval_seconds must be non-negative" in bad_interval["content"][0]["text"]

        bad_runs = await handler(
            {"cron": "*/2 * * * *", "prompt": "hi", "max_runs": 0}
        )
        assert bad_runs["is_error"] is True
        assert "max_runs must be positive" in bad_runs["content"][0]["text"]

        bad_mode = await handler(
            {"cron": "*/2 * * * *", "prompt": "hi", "run_mode": "boom"}
        )
        assert bad_mode["is_error"] is True
        assert "run_mode must be continue or reset_session" in bad_mode["content"][0]["text"]

        bad_inherit = await handler(
            {"cron": "*/2 * * * *", "prompt": "hi", "inherit": "children"}
        )
        assert bad_inherit["is_error"] is True
        assert "inherit must be none, fork, or all" in bad_inherit["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_cron_create_delegates_to_transport(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.cron_creator = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "CronCreate")

        result = await handler(
            {
                "cron": "*/2 * * * *",
                "prompt": "run job",
                "interval_seconds": 0,
                "run_mode": "reset_session",
                "description": "maint",
                "until": "2026-03-11T12:00:00Z",
                "inherit": "none",
            }
        )

        assert result["content"][0]["text"] == "ok"
        state.cron_creator.assert_awaited_once_with(
            {
                "schedule_mode": "interval",
                "cron": "*/2 * * * *",
                "prompt": "run job",
                "interval_seconds": 0,
                "reset_session": True,
                "description": "maint",
                "max_runs": 1,
                "from": None,
                "until": "2026-03-11T12:00:00Z",
                "inherit": "none",
                "tool_use_id": None,
            }
        )

    @pytest.mark.asyncio
    async def test_cron_list_and_delete_require_transport(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        list_handler = _tool_handler(captured["tools"], "CronList")
        delete_handler = _tool_handler(captured["tools"], "CronDelete")

        list_result = await list_handler({})
        assert list_result["is_error"] is True
        assert "does not provide task orchestration" in list_result["content"][0]["text"]

        delete_result = await delete_handler({"id": "abc"})
        assert delete_result["is_error"] is True
        assert "does not provide task orchestration" in delete_result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_cron_delete_requires_id(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.cron_deleter = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "CronDelete")

        result = await handler({})
        assert result["is_error"] is True
        assert "id is required" in result["content"][0]["text"]


class TestPromptFile:
    """Tests for the prompt_file parameter on AgentTask."""

    @pytest.mark.asyncio
    async def test_prompt_file_vault_relative(self, monkeypatch, skill_config, tmp_path):
        """Vault-relative path resolves correctly and file content becomes the prompt."""
        from obs_agent.tools import create_obs_tools

        # Write a prompt file inside the vault
        prompt_path = skill_config.vault_path / "procedures" / "research.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("Search the codebase for bugs", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "procedures/research.md"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == ""
        assert launch_args["prompt_file_content"] == "Search the codebase for bugs"

    @pytest.mark.asyncio
    async def test_prompt_file_absolute_path(self, monkeypatch, skill_config, tmp_path):
        """Absolute path is used as-is without vault prefix."""
        from obs_agent.tools import create_obs_tools

        prompt_path = tmp_path / "external" / "task.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("External task content", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": str(prompt_path)})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == ""
        assert launch_args["prompt_file_content"] == "External task content"

    @pytest.mark.asyncio
    async def test_prompt_file_tilde_path(self, monkeypatch, skill_config, tmp_path):
        """Tilde paths get expanded to user home directory."""
        from obs_agent.tools import create_obs_tools

        # Create a file under a fake home
        home_file = tmp_path / "docs" / "task.md"
        home_file.parent.mkdir(parents=True, exist_ok=True)
        home_file.write_text("Home dir task", encoding="utf-8")

        # Monkeypatch expanduser to use tmp_path as home
        original_expanduser = Path.expanduser

        def fake_expanduser(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(tmp_path) + s[1:])
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", fake_expanduser)

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "~/docs/task.md"})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == ""
        assert launch_args["prompt_file_content"] == "Home dir task"

    @pytest.mark.asyncio
    async def test_prompt_file_not_found_returns_error(self, monkeypatch, skill_config):
        """Missing file returns a clear error without crashing."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "nonexistent/file.md"})

        assert result["is_error"] is True
        assert "prompt_file not found" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompt_and_prompt_file_both_are_combined(self, monkeypatch, skill_config):
        """Providing both prompt and prompt_file preserves inline prompt and file context separately."""
        from obs_agent.tools import create_obs_tools

        prompt_path = skill_config.vault_path / "task.md"
        prompt_path.write_text("file content", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "inline prompt", "prompt_file": "task.md"})

        assert result["content"][0]["text"] == "ok"
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == "inline prompt"
        assert launch_args["prompt_file"] == "task.md"
        assert launch_args["prompt_file_content"] == "file content"

    @pytest.mark.asyncio
    async def test_neither_prompt_nor_prompt_file_errors(self, monkeypatch, skill_config):
        """Providing neither prompt nor prompt_file returns an error."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"display_name": "No prompt agent"})

        assert result["is_error"] is True
        assert "prompt or prompt_file is required" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompt_file_path_in_payload(self, monkeypatch, skill_config):
        """The prompt_file path is included in the launch payload for service message display."""
        from obs_agent.tools import create_obs_tools

        prompt_path = skill_config.vault_path / "procedures" / "audit.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("Audit the vault", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "procedures/audit.md"})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt_file"] == "procedures/audit.md"
        assert launch_args["prompt"] == ""
        assert launch_args["prompt_file_content"] == "Audit the vault"

    @pytest.mark.asyncio
    async def test_prompt_file_whitespace_stripped(self, monkeypatch, skill_config):
        """File content with leading/trailing whitespace is stripped."""
        from obs_agent.tools import create_obs_tools

        prompt_path = skill_config.vault_path / "padded.md"
        prompt_path.write_text("\n\n  Do the thing  \n\n", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "padded.md"})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["prompt"] == ""
        assert launch_args["prompt_file_content"] == "Do the thing"

    @pytest.mark.asyncio
    async def test_prompt_file_empty_file_errors(self, monkeypatch, skill_config):
        """An empty file (whitespace-only) is treated as no prompt."""
        from obs_agent.tools import create_obs_tools

        prompt_path = skill_config.vault_path / "empty.md"
        prompt_path.write_text("   \n  \n  ", encoding="utf-8")

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt_file": "empty.md"})

        assert result["is_error"] is True
        assert "prompt or prompt_file is required" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()


class TestHooksParameter:
    """Tests for hooks and inherit_hooks parameter validation on AgentTask."""

    @pytest.mark.asyncio
    async def test_hooks_invalid_json_returns_error(self, monkeypatch, skill_config):
        """Invalid JSON string for hooks is rejected with a clear error."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "test", "hooks": "{not valid json"})

        assert result["is_error"] is True
        assert "hooks must be a valid JSON object" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hooks_non_dict_json_returns_error(self, monkeypatch, skill_config):
        """Valid JSON that is not a dict (e.g. a list) is rejected."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "test", "hooks": '["a", "b"]'})

        assert result["is_error"] is True
        assert "hooks must be a JSON object, got list" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hooks_entry_missing_separator_returns_error(self, monkeypatch, skill_config):
        """Hook spec missing '::' separator is rejected with a clear error."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {"prompt": "test", "hooks": '{"PreToolUse": "path/to/file.py"}'}
        )

        assert result["is_error"] is True
        assert "hooks['PreToolUse']" in result["content"][0]["text"]
        assert "file_path::function_name" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hooks_valid_format_passes_through(self, monkeypatch, skill_config):
        """Valid hooks dict in file.py::function_name format passes validation."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {
                "prompt": "test",
                "hooks": '{"PreToolUse": "guard.py::check_access", "PostToolUse": "log.py::log_result"}',
            }
        )

        assert result.get("is_error") is not True
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["hooks"] == {
            "PreToolUse": "guard.py::check_access",
            "PostToolUse": "log.py::log_result",
        }

    @pytest.mark.asyncio
    async def test_hooks_entry_non_string_value_returns_error(self, monkeypatch, skill_config):
        """Hook spec with a non-string value (e.g. int) is rejected."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {"prompt": "test", "hooks": '{"PreToolUse": 42}'}
        )

        assert result["is_error"] is True
        assert "hooks['PreToolUse']" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inherit_hooks_true_coercion(self, monkeypatch, skill_config):
        """String 'true' is coerced to boolean True for inherit_hooks."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "test", "inherit_hooks": "true"})

        assert result.get("is_error") is not True
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["inherit_hooks"] is True

    @pytest.mark.asyncio
    async def test_inherit_hooks_false_coercion(self, monkeypatch, skill_config):
        """String 'false' is coerced to boolean False for inherit_hooks."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "test", "inherit_hooks": "false"})

        assert result.get("is_error") is not True
        state.fork_task_launcher.assert_awaited_once()
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["inherit_hooks"] is False

    @pytest.mark.asyncio
    async def test_inherit_hooks_invalid_string_returns_error(self, monkeypatch, skill_config):
        """Non-boolean string for inherit_hooks is rejected."""
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock()
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "test", "inherit_hooks": "maybe"})

        assert result["is_error"] is True
        assert "inherit_hooks must be true or false" in result["content"][0]["text"]
        state.fork_task_launcher.assert_not_awaited()
