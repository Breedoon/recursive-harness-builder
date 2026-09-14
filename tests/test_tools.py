"""Tests for obs_agent.tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.lineage import ObsBootstrap, agent_name_for_lineage, lineage_fingerprint


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


class _TrackedJsonlHandle:
    def __init__(self, owner, lines: list[str]) -> None:
        self.owner = owner
        self.lines = iter(lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.lines)
        self.owner.lines_read += 1
        return line


class _StatOnlyPath:
    def __init__(
        self,
        label: str,
        mtime: float | None = None,
        *,
        fail: bool = False,
        raw_lines: list[str] | None = None,
    ) -> None:
        self.label = label
        self.mtime = mtime
        self.fail = fail
        self.raw_lines = list(raw_lines or [])
        self.stat_calls = 0
        self.read_calls = 0
        self.open_calls = 0
        self.lines_read = 0

    def stat(self):
        self.stat_calls += 1
        if self.fail:
            raise OSError("stat unavailable")
        return type("Stat", (), {"st_mtime": self.mtime})()

    def open(self, *args, **kwargs):
        self.open_calls += 1
        return _TrackedJsonlHandle(self, self.raw_lines)

    def read_text(self, *args, **kwargs):
        self.read_calls += 1
        raise AssertionError(f"activity lookup read JSONL content: {self.label}")

    def __str__(self) -> str:
        return self.label


def _search_bootstrap(team_name: str, agent_name: str, lineage: tuple[str, ...], session_id: str = "caller") -> ObsBootstrap:
    return ObsBootstrap(
        raw_xml="<obs-bootstrap version='2' />",
        lineage=lineage,
        origin="agent_task_fresh" if len(lineage) > 1 else "trunk_start",
        is_fork=len(lineage) > 1,
        session_id=session_id,
        agent_id=None,
        parent_session_id=None,
        root_team_key=team_name,
        agent_name=agent_name,
        parent_agent_name=None,
        parent_display_name=None,
    )


def _search_member(
    name: str,
    lineage: tuple[str, ...],
    *,
    display_name: str | None = None,
    parent_agent_name: str | None = None,
    parent_display_name: str | None = None,
    **obs_fields,
) -> dict:
    obs = {
        "lineage": list(lineage),
        "lineage_length": len(lineage),
        "display_name": display_name or lineage[-1],
    }
    if parent_agent_name is not None:
        obs["parent_agent_name"] = parent_agent_name
    if parent_display_name is not None:
        obs["parent_display_name"] = parent_display_name
    obs.update(obs_fields)
    return {"name": name, "obs": obs}


def _write_search_team(tmp_path: Path, team_name: str, members: list[dict], *, inbox_names: list[str] | None = None) -> Path:
    team_dir = tmp_path / ".claude" / "teams" / team_name
    inbox_dir = team_dir / "inboxes"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for name in inbox_names or [member["name"] for member in members]:
        (inbox_dir / f"{name}.json").write_text("[]", encoding="utf-8")
    (team_dir / "config.json").write_text(json.dumps({"members": members}), encoding="utf-8")
    return team_dir


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

    def test_send_inbox_message_schema_marks_only_recipient_and_content_required(
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
        assert "needs_reply" not in schema["properties"]
        assert "must_reply" not in schema["properties"]

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

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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

    def test_search_team_schema_has_no_required_fields(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "search_team")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["required"] == []
        assert schema["properties"]["running_only"]["type"] == "boolean"
        assert schema["properties"]["limit"]["type"] == "integer"
        assert schema["properties"]["cursor"]["type"] == "string"

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

    def test_agent_task_schema_exposes_session_source(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123")

        tool = next(tool for tool in captured["tools"] if tool.name == "AgentTask")
        session_source = tool.input_schema["properties"]["session_source"]
        assert session_source["type"] == "string"
        assert "session ID or JSONL file path" in session_source["description"]

    @pytest.mark.asyncio
    async def test_agent_task_passes_session_source_to_launcher(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler({"prompt": "Do work", "session_source": "foreign-session", "fork": True})

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["session_source"] == "foreign-session"
        assert launch_args["fork"] is True

    @pytest.mark.asyncio
    async def test_agent_task_rejects_session_source_with_fork_false_or_resume(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        fork_false = await handler({"prompt": "Do work", "session_source": "foreign", "fork": False})
        with_resume = await handler({"prompt": "Do work", "session_source": "foreign", "resume": "task-1"})

        assert fork_false["is_error"] is True
        assert "session_source is only supported with fork=true" in fork_false["content"][0]["text"]
        assert with_resume["is_error"] is True
        assert "resume and session_source are mutually exclusive" in with_resume["content"][0]["text"]
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
                        "text": "AgentTask launched.\nteam_name: team-alpha\nagent_name: abc123-worker\nsession_id: sid-123\nagentId: task-123\ntask_id_scope: deprecated/internal\noutput_file: /tmp/task-123.jsonl\ntelegram_topic: https://t.me/c/1/2",
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
        assert "task_id_scope: deprecated/internal" in result["content"][0]["text"]
        assert "telegram_topic: https://t.me/c/1/2" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_resolves_relative_hook_paths_against_vault(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTask")

        result = await handler(
            {
                "prompt": "Do work",
                "hooks": json.dumps(
                    {
                        "PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/router_guard.py::check",
                        "Stop": "~/obs-hooks/feedback.py::spawn_feedback",
                    }
                ),
            }
        )

        assert result["content"][0]["text"] == "ok"
        launch_args = state.fork_task_launcher.await_args.args[0]
        assert launch_args["hooks"]["PreToolUse"] == str(
            skill_config.vault_path
            / "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/router_guard.py"
        ) + "::check"
        assert launch_args["hooks"]["Stop"].endswith("/obs-hooks/feedback.py::spawn_feedback")

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
            {
                "task_id": "task-123",
                "team_name": "",
                "agent_name": "",
                "block": False,
                "timeout": 1,
                "tool_use_id": None,
            }
        )

    @pytest.mark.asyncio
    async def test_agent_task_output_ignores_caller_output_path(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(return_value={"content": [{"type": "text", "text": "safe"}]})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

        result = await handler(
            {
                "team_name": "2026-03-31-10-00-root",
                "agent_name": "abcdef1234-worker",
                "output_file": "/tmp/attacker.jsonl",
                "path": "../../outside.jsonl",
                "block": False,
                "timeout": 1,
            }
        )

        assert result["content"][0]["text"] == "safe"
        forwarded = state.fork_task_outputter.await_args.args[0]
        assert "output_file" not in forwarded
        assert "path" not in forwarded

    @pytest.mark.asyncio
    async def test_agent_task_output_accepts_stable_identity(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_outputter = AsyncMock(return_value={"ok": True})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

        result = await handler({"team_name": "2026-03-31-10-00-root", "agent_name": "abcdef1234-worker", "block": False, "timeout": 1})

        state.fork_task_outputter.assert_awaited_once_with(
            {
                "task_id": "",
                "team_name": "2026-03-31-10-00-root",
                "agent_name": "abcdef1234-worker",
                "block": False,
                "timeout": 1,
                "tool_use_id": None,
            }
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_agent_task_output_validates_args(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTaskOutput")

        result = await handler({"task_id": "", "block": False, "timeout": 1})
        assert result["is_error"] is True
        assert "task_id or agent_name is required" in result["content"][0]["text"]

        result = await handler({"agent_name": "abcdef1234-worker", "block": False, "timeout": 1})
        assert result["is_error"] is True
        assert "team_name" in result["content"][0]["text"] or "caller" in result["content"][0]["text"]

        result = await handler({"task_id": "task-123", "block": "maybe", "timeout": 1})
        assert result["is_error"] is True
        assert "block must be true or false" in result["content"][0]["text"]

        result = await handler({"task_id": "task-123", "block": False, "timeout": "abc"})
        assert result["is_error"] is True
        assert "timeout must be an integer" in result["content"][0]["text"]

        result = await handler({"team_name": "2026-03-31-10-00-root", "agent_name": "../worker", "block": False, "timeout": 1})
        assert result["is_error"] is True

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
            {
                "task_id": "task-123",
                "team_name": "",
                "agent_name": "",
                "block": True,
                "timeout": 1,
                "tool_use_id": None,
            }
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
            {"task_id": "task-123", "team_name": "", "agent_name": "", "tool_use_id": None}
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
            {"task_id": "task-123", "team_name": "", "agent_name": "", "tool_use_id": None}
        )
        assert "\"task_id\":\"task-123\"" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_stop_accepts_stable_identity(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        state = HookState()
        state.fork_task_stopper = AsyncMock(return_value={"ok": True})
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=state)
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

        result = await handler({"team_name": "2026-03-31-10-00-root", "agent_name": "abcdef1234-worker"})

        assert result == {"ok": True}
        state.fork_task_stopper.assert_awaited_once_with(
            {
                "task_id": "",
                "team_name": "2026-03-31-10-00-root",
                "agent_name": "abcdef1234-worker",
                "tool_use_id": None,
            }
        )

    @pytest.mark.asyncio
    async def test_agent_task_stop_requires_task_id(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

        result = await handler({})

        assert result["is_error"] is True
        assert "task_id or agent_name is required" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_agent_task_stop_rejects_ambiguous_identity(self, monkeypatch, skill_config):
        from obs_agent.tools import create_obs_tools

        captured = _capture_tools(monkeypatch)
        create_obs_tools(skill_config, lambda: "sid-123", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "AgentTaskStop")

        result = await handler({"task_id": "task-123", "team_name": "2026-03-31-10-00-root", "agent_name": "abcdef1234-worker"})
        assert result["is_error"] is True
        assert "cannot be combined" in result["content"][0]["text"]

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
            tmp_path
            / ".claude"
            / "teams"
            / "obs-tree-root-123"
            / "inboxes"
            / "obs-agent-peer-999.json"
        )
        persisted = json.loads(inbox_path.read_text(encoding="utf-8"))
        assert persisted[0]["from"] == "obs-agent-child-123"

        self_inbox = (
            tmp_path
            / ".claude"
            / "teams"
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
            tmp_path
            / ".claude"
            / "teams"
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

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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
        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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
        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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
        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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
        child_path = tmp_path / ".claude" / "teams" / "2026-03-30-10-10-root" / "inboxes" / f"{child_name}.json"
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
        team_dir = tmp_path / ".claude" / "teams" / root_agent
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

        xml_root = ET.fromstring(result["content"][0]["text"])
        assert xml_root.tag == "search_team"
        assert xml_root.attrib["mode"] == "children"
        payload = result["tool_use_result"]
        assert payload["mode"] == "children"
        assert payload["children"] == [newer_child, older_child]
        assert [member["agent_name"] for member in payload["members"]] == [newer_child, older_child]

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

        root_path = tmp_path / ".claude" / "teams" / "2026-03-30-10-10-root" / "inboxes" / "2026-03-30-10-10-root.json"
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

        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
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

        inboxes = tmp_path / ".claude" / "teams" / "2026-03-30-10-10-root" / "inboxes"
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
        team_config = tmp_path / ".claude" / "teams" / "2026-03-30-10-10-root" / "config.json"
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
        family_payload = family["tool_use_result"]
        assert ET.fromstring(family["content"][0]["text"]).tag == "search_team"
        assert family_payload["parent"] == "e96857c58f-branch"
        assert family_payload["siblings"] == ["8fb0d4bb4f-sibling"]
        assert family_payload["children"] == ["83a3e652c4-child-a", "83a3e652c4-child-b"]

        ancestors = await handler({"mode": "ancestors"})
        ancestors_payload = ancestors["tool_use_result"]
        assert ancestors_payload["ancestors"] == [
            "2026-03-30-10-10-root",
            "e96857c58f-branch",
        ]

        descendants = await handler({"mode": "descendants"})
        descendants_payload = descendants["tool_use_result"]
        assert descendants_payload["descendants"] == [
            "83a3e652c4-child-a",
            "83a3e652c4-child-b",
        ]

        tree = await handler({"mode": "tree"})
        tree_payload = tree["tool_use_result"]
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
    @pytest.mark.parametrize("target", ["root", "middle", "leaf"])
    async def test_search_team_covers_all_modes_from_root_middle_and_leaf(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
        target,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-lineage"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        middle = agent_name_for_lineage(("Root", "Middle"), team_key=team_name)
        middle_sibling = agent_name_for_lineage(("Root", "Other Middle"), team_key=team_name)
        leaf = agent_name_for_lineage(("Root", "Middle", "Leaf"), team_key=team_name)
        leaf_sibling = agent_name_for_lineage(("Root", "Middle", "Other Leaf"), team_key=team_name)
        grandchild = agent_name_for_lineage(("Root", "Middle", "Leaf", "Grandchild"), team_key=team_name)
        members = [
            _search_member(root, ("Root",)),
            _search_member(middle, ("Root", "Middle"), parent_agent_name=root, parent_display_name="Root"),
            _search_member(middle_sibling, ("Root", "Other Middle"), parent_agent_name=root, parent_display_name="Root"),
            _search_member(leaf, ("Root", "Middle", "Leaf"), parent_agent_name=middle, parent_display_name="Middle"),
            _search_member(leaf_sibling, ("Root", "Middle", "Other Leaf"), parent_agent_name=middle, parent_display_name="Middle"),
            _search_member(grandchild, ("Root", "Middle", "Leaf", "Grandchild"), parent_agent_name=leaf, parent_display_name="Leaf"),
        ]
        _write_search_team(tmp_path, team_name, members)
        target_names = {"root": (root, ("Root",)), "middle": (middle, ("Root", "Middle")), "leaf": (leaf, ("Root", "Middle", "Leaf"))}
        caller_agent, caller_lineage = target_names[target]
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, caller_agent, caller_lineage),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        for mode in ("parent", "children", "siblings", "ancestors", "descendants", "family", "tree"):
            result = await handler({"mode": mode})
            payload = result["tool_use_result"]
            assert payload["target_agent"] == caller_agent
            assert payload["team_name"] == team_name
            assert ET.fromstring(result["content"][0]["text"]).tag == "search_team"
            member_names = {member["agent_name"] for member in payload["members"]}
            assert payload["returned"] == len(payload["members"])
            assert payload["returned"] <= payload["limit"]
            for compatibility_key in ("children", "siblings", "ancestors", "descendants", "tree"):
                if compatibility_key in payload:
                    assert len(payload[compatibility_key]) <= payload["limit"]
            if mode == "tree":
                assert len(payload["tree_members"]) == len(payload["members"])
                assert [member["agent_name"] for member in payload["tree_members"]] == [
                    member["agent_name"] for member in payload["members"]
                ]
            if mode != "tree":
                assert caller_agent not in member_names
            if mode == "parent":
                assert "parent" in payload
                expected_parent = {
                    "root": set(),
                    "middle": {root},
                    "leaf": {middle},
                }[target]
                assert member_names == expected_parent
            elif mode == "children":
                assert "children" in payload
                expected_children = {
                    "root": {middle, middle_sibling},
                    "middle": {leaf, leaf_sibling},
                    "leaf": {grandchild},
                }[target]
                assert member_names == expected_children
            elif mode == "siblings":
                assert "siblings" in payload
                expected_siblings = {
                    "root": set(),
                    "middle": {middle_sibling},
                    "leaf": {leaf_sibling},
                }[target]
                assert member_names == expected_siblings
            elif mode == "ancestors":
                assert "ancestors" in payload
                expected_ancestors = {
                    "root": set(),
                    "middle": {root},
                    "leaf": {root, middle},
                }[target]
                assert member_names == expected_ancestors
            elif mode == "descendants":
                assert "descendants" in payload
                expected_descendants = {
                    "root": {middle, middle_sibling, leaf, leaf_sibling, grandchild},
                    "middle": {leaf, leaf_sibling, grandchild},
                    "leaf": {grandchild},
                }[target]
                assert member_names == expected_descendants
                assert caller_agent not in member_names
            elif mode == "family":
                assert {"parent", "children", "siblings"} <= payload.keys()
                expected_family = {
                    "root": {middle, middle_sibling},
                    "middle": {root, leaf, leaf_sibling, middle_sibling},
                    "leaf": {middle, leaf_sibling, grandchild},
                }[target]
                assert member_names == expected_family
                assert set(payload["children"]) == ({middle, middle_sibling} if target == "root" else {leaf, leaf_sibling} if target == "middle" else {grandchild})
            else:
                assert "tree" in payload
                assert "tree_members" in payload
                assert member_names == {root, middle, middle_sibling, leaf, leaf_sibling, grandchild}
                assert payload["tree"] == sorted(member_names)
                assert next(member for member in payload["tree_members"] if member["agent_name"] == caller_agent)["relation"] == "self"

    @pytest.mark.asyncio
    async def test_search_team_target_defaults_cross_team_root_and_agent_inference(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        caller_team = "team-caller"
        remote_team = "team-remote"
        caller_root = agent_name_for_lineage(("Caller Root",), team_key=caller_team)
        caller_child = agent_name_for_lineage(("Caller Root", "Caller Child"), team_key=caller_team)
        remote_root = agent_name_for_lineage(("Remote Root",), team_key=remote_team)
        remote_child = agent_name_for_lineage(("Remote Root", "Remote Child"), team_key=remote_team)
        _write_search_team(
            tmp_path,
            caller_team,
            [
                _search_member(caller_root, ("Caller Root",)),
                _search_member(caller_child, ("Caller Root", "Caller Child"), parent_agent_name=caller_root),
            ],
        )
        _write_search_team(
            tmp_path,
            remote_team,
            [
                _search_member(remote_root, ("Remote Root",)),
                _search_member(remote_child, ("Remote Root", "Remote Child"), parent_agent_name=remote_root),
            ],
        )
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(caller_team, caller_child, ("Caller Root", "Caller Child")),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        caller_default = await handler({"mode": "parent"})
        assert caller_default["tool_use_result"]["target_agent"] == caller_child
        assert caller_default["tool_use_result"]["parent"] == caller_root

        inferred_agent = await handler({"mode": "parent", "agent_name": caller_child})
        assert inferred_agent["tool_use_result"]["team_name"] == caller_team
        assert inferred_agent["tool_use_result"]["target_agent"] == caller_child

        remote_agent_only = await handler({"mode": "parent", "agent_name": remote_child})
        assert remote_agent_only["is_error"] is True
        assert "not locally known" in remote_agent_only["content"][0]["text"]

        remote_target = await handler({"mode": "parent", "team_name": remote_team, "agent_name": remote_child})
        assert remote_target["tool_use_result"]["current_agent"] == caller_child
        assert remote_target["tool_use_result"]["target_agent"] == remote_child
        assert remote_target["tool_use_result"]["parent"] == remote_root

        remote_root_target = await handler({"mode": "children", "team_name": remote_team})
        assert remote_root_target["tool_use_result"]["current_agent"] == caller_child
        assert remote_root_target["tool_use_result"]["target_agent"] == remote_root
        assert remote_root_target["tool_use_result"]["children"] == [remote_child]

        ambiguous_team = "team-ambiguous"
        ambiguous_a = agent_name_for_lineage(("Root A",))
        ambiguous_b = agent_name_for_lineage(("Root B",))
        assert ambiguous_a != ambiguous_b
        _write_search_team(
            tmp_path,
            ambiguous_team,
            [_search_member(ambiguous_a, ("Root A",)), _search_member(ambiguous_b, ("Root B",))],
        )
        ambiguous = await handler({"mode": "tree", "team_name": ambiguous_team})
        assert ambiguous["is_error"] is True
        assert "unambiguous" in ambiguous["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_search_team_rejects_unsafe_and_unknown_targets_before_activity_lookup(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-safe"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        _write_search_team(tmp_path, team_name, [_search_member(root, ("Root",))])
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        lookup_calls: list[set[str]] = []
        monkeypatch.setattr(
            "obs_agent.tools.find_session_jsonl_index",
            lambda *, session_ids, cwd: lookup_calls.append(set(session_ids)) or {},
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        traversal = await handler({"mode": "tree", "team_name": "../team-safe"})
        assert traversal["is_error"] is True
        assert "known local identity" in traversal["content"][0]["text"]
        assert lookup_calls == []

        unknown = await handler({"mode": "tree", "agent_name": "not-known"})
        assert unknown["is_error"] is True
        assert "not locally known" in unknown["content"][0]["text"]
        assert lookup_calls == []

    @pytest.mark.asyncio
    async def test_search_team_exposes_status_activity_precedence_precision_and_running_only(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-status"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        names = {
            label: agent_name_for_lineage(("Root", label.title()), team_key=team_name)
            for label in ("live", "idle", "completed", "failed", "stopped", "fallback", "projection", "unknown")
        }
        members = [_search_member(root, ("Root",))]
        for label, name in names.items():
            members.append(
                _search_member(
                    name,
                    ("Root", label.title()),
                    parent_agent_name=root,
                    session_id=f"sid-{label}" if label not in {"unknown", "projection"} else ("sid-projection" if label == "projection" else None),
                    updated_at=12_345 if label == "projection" else None,
                    root_team_key=team_name,
                    topic_chat_id=123 if label == "live" else None,
                    topic_thread_id=456 if label == "live" else None,
                )
            )
        _write_search_team(tmp_path, team_name, members)
        paths = {
            f"sid-{label}": _StatOnlyPath(
                f"sid-{label}",
                mtime=80.123456 if label in {"completed", "failed"} else 100.123456 - index * 10,
                raw_lines=[
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": 80.123456 if label in {"completed", "failed"} else 100.123456 - index * 10,
                        }
                    )
                ],
            )
            for index, label in enumerate(("live", "idle", "completed", "failed", "stopped"))
        }
        paths["sid-fallback"] = _StatOnlyPath(
            "sid-fallback",
            mtime=55.0,
            fail=False,
            raw_lines=[
                "{malformed",
                json.dumps({"timestamp": "not-a-timestamp"}),
                json.dumps({"timestamp": 10**1000}),
            ],
        )
        paths["sid-projection"] = _StatOnlyPath("sid-projection", fail=True)
        provider = {
            (team_name, names["live"]): {
                "team_name": team_name, "agent_name": names["live"], "session_id": "sid-live",
                "running": True, "runtime_status": "running", "task_status": "launched", "idle_ready": False,
            },
            (team_name, names["idle"]): {
                "team_name": team_name, "agent_name": names["idle"], "session_id": "sid-idle",
                "running": False, "runtime_status": "idle", "task_status": "completed", "idle_ready": True,
            },
            (team_name, names["completed"]): {
                "team_name": team_name, "agent_name": names["completed"], "session_id": "sid-completed",
                "running": False, "runtime_status": "completed", "task_status": "completed", "idle_ready": False,
            },
            (team_name, names["failed"]): {
                "team_name": team_name, "agent_name": names["failed"], "session_id": "sid-failed",
                "running": False, "runtime_status": "failed", "task_status": "failed", "idle_ready": False,
            },
            (team_name, names["stopped"]): {
                "team_name": team_name, "agent_name": names["stopped"], "session_id": "sid-stopped",
                "running": False, "runtime_status": "stopped", "task_status": "stopped", "idle_ready": False,
            },
            (team_name, names["fallback"]): {
                "team_name": team_name, "agent_name": names["fallback"], "session_id": "sid-fallback",
                "running": False, "runtime_status": "unknown", "last_activity": 45.5,
            },
            (team_name, names["projection"]): {
                "team_name": team_name, "agent_name": names["projection"], "session_id": "sid-projection",
                "running": False, "runtime_status": "unknown",
            },
        }
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        index_calls: list[set[str]] = []
        monkeypatch.setattr(
            "obs_agent.tools.find_session_jsonl_index",
            lambda *, session_ids, cwd: index_calls.append(set(session_ids)) or paths,
        )
        state = HookState(team_status_provider=lambda **_: provider)
        create_obs_tools(skill_config, lambda: "caller", hook_state=state)
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "children"})
        payload = result["tool_use_result"]
        by_name = {member["agent_name"]: member for member in payload["members"]}
        assert by_name[names["live"]]["session_id"] == "sid-live"
        assert by_name[names["live"]]["display_name"] == "Live"
        assert by_name[names["live"]]["parent_agent_name"] == root
        assert by_name[names["live"]]["lineage"] == ["Root", "Live"]
        assert by_name[names["live"]]["lineage_length"] == 2
        assert by_name[names["live"]]["relation"] == "child"
        assert by_name[names["live"]]["running"] is True
        assert by_name[names["live"]]["runtime_status"] == "running"
        assert by_name[names["live"]]["task_status"] == "launched"
        assert by_name[names["idle"]]["runtime_status"] == "idle"
        assert by_name[names["idle"]]["idle_ready"] is True
        assert by_name[names["completed"]]["runtime_status"] == "completed"
        assert by_name[names["failed"]]["runtime_status"] == "failed"
        assert by_name[names["stopped"]]["runtime_status"] == "stopped"
        assert by_name[names["live"]]["last_activity_epoch"] == 100.123456
        assert by_name[names["live"]]["last_activity_source"] == "jsonl_event_timestamp"
        assert by_name[names["live"]]["last_activity_at"].endswith(".123456Z")
        assert by_name[names["live"]]["root_team_key"] == team_name
        assert by_name[names["live"]]["topic_chat_id"] == 123
        assert by_name[names["live"]]["topic_thread_id"] == 456
        assert by_name[names["fallback"]]["last_activity_epoch"] == 55.0
        assert by_name[names["fallback"]]["last_activity_source"] == "jsonl_mtime_fallback"
        assert paths["sid-fallback"].open_calls == 1
        assert paths["sid-fallback"].lines_read == 3
        assert paths["sid-fallback"].stat_calls == 1
        assert by_name[names["projection"]]["last_activity_epoch"] == 12_345
        assert by_name[names["projection"]]["last_activity_source"] == "projection_timestamp"
        assert by_name[names["unknown"]]["last_activity_source"] == "unknown"
        ordered_names = [member["agent_name"] for member in payload["members"]]
        assert ordered_names[-1] == names["unknown"]
        tied = [name for name in ordered_names if by_name[name].get("last_activity_epoch") == 80.123456]
        assert tied == sorted(tied)
        assert paths["sid-live"].open_calls == 1
        assert paths["sid-live"].lines_read == 1
        assert len(index_calls) == 1

        running = await handler({"mode": "children", "running_only": True})
        assert [member["agent_name"] for member in running["tool_use_result"]["members"]] == [names["live"]]
        assert len(index_calls) == 2

        again = await handler({"mode": "children"})
        assert again["tool_use_result"]["returned"] == len(names)
        assert len(index_calls) == 3

    @pytest.mark.asyncio
    async def test_search_team_activity_filters_are_exact_inclusive_conjunctive_and_single_now(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-activity"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child_names = {
            value: agent_name_for_lineage(("Root", value.title()), team_key=team_name)
            for value in ("equal-after", "equal-before", "recent", "old", "unknown")
        }
        members = [_search_member(root, ("Root",))]
        for value, name in child_names.items():
            members.append(_search_member(name, ("Root", value.title()), parent_agent_name=root, session_id=f"sid-{value}" if value != "unknown" else None))
        _write_search_team(tmp_path, team_name, members)
        paths = {
            "sid-equal-after": _StatOnlyPath("after", 100.0, raw_lines=[json.dumps({"timestamp": 100.0})]),
            "sid-equal-before": _StatOnlyPath("before", 200.0, raw_lines=[json.dumps({"timestamp": 200.0})]),
            "sid-recent": _StatOnlyPath("recent", 150.0, raw_lines=[json.dumps({"timestamp": 150.0})]),
            "sid-old": _StatOnlyPath("old", 50.0, raw_lines=[json.dumps({"timestamp": 50.0})]),
        }
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr("obs_agent.tools.time.time", lambda: 200.0)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        now_calls = 0

        def controlled_now() -> float:
            nonlocal now_calls
            now_calls += 1
            return 200.0

        monkeypatch.setattr("obs_agent.tools.time.time", controlled_now)
        monkeypatch.setattr(
            "obs_agent.tools.find_session_jsonl_index",
            lambda *, session_ids, cwd: paths,
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        after = await handler({"mode": "children", "activity_after": 100})
        assert [member["agent_name"] for member in after["tool_use_result"]["members"]] == [child_names["equal-before"], child_names["recent"]]

        before = await handler({"mode": "children", "activity_before": "1970-01-01T00:03:20Z"})
        assert [member["agent_name"] for member in before["tool_use_result"]["members"]] == [child_names["recent"], child_names["equal-after"], child_names["old"]]

        recent = await handler({"mode": "children", "active_within_seconds": 50})
        assert [member["agent_name"] for member in recent["tool_use_result"]["members"]] == [child_names["equal-before"], child_names["recent"]]

        conjunctive = await handler({"mode": "children", "running_only": True, "activity_after": 90, "activity_before": 160})
        assert conjunctive["tool_use_result"]["members"] == []
        assert now_calls == 4

        unfiltered = await handler({"mode": "children"})
        assert unfiltered["tool_use_result"]["members"][-1]["last_activity_source"] == "unknown"

        malformed = await handler({"mode": "children", "activity_after": "2026-08-12T12:00:00"})
        assert malformed["is_error"] is True
        assert "RFC3339" in malformed["content"][0]["text"]
        assert now_calls == 6

    @pytest.mark.asyncio
    async def test_search_team_uses_newest_valid_jsonl_event_timestamp_and_scans_complete_file(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-event-activity"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child = agent_name_for_lineage(("Root", "Worker"), team_key=team_name)
        _write_search_team(
            tmp_path,
            team_name,
            [
                _search_member(root, ("Root",)),
                _search_member(child, ("Root", "Worker"), parent_agent_name=root, session_id="sid-worker"),
            ],
        )
        path = _StatOnlyPath(
            "sid-worker",
            mtime=500.0,
            raw_lines=[
                json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:05:00Z"}),
                "not-json",
                json.dumps({"type": "assistant", "timestamp": "not-a-timestamp"}),
                json.dumps({"type": "assistant", "timestamp": 10**1000}),
                json.dumps({"type": "user", "timestamp": "1970-01-01T00:03:20Z"}),
            ],
        )
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        monkeypatch.setattr(
            "obs_agent.tools.find_session_jsonl_index",
            lambda *, session_ids, cwd: {"sid-worker": path},
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "children", "activity_after": 299})
        member = result["tool_use_result"]["members"][0]
        assert member["last_activity_epoch"] == 300.0
        assert member["last_activity_source"] == "jsonl_event_timestamp"
        assert path.open_calls == 1
        assert path.lines_read == 5
        assert path.stat_calls == 0

        filtered = await handler({"mode": "children", "activity_after": 300})
        assert filtered["tool_use_result"]["members"] == []

    @pytest.mark.asyncio
    async def test_search_team_paginates_after_filtering_and_validates_bounds(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-pages"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        children = [
            agent_name_for_lineage(("Root", f"Worker {idx}"), team_key=team_name)
            for idx in range(205)
        ]
        members = [_search_member(root, ("Root",))]
        members.extend(
            _search_member(name, ("Root", f"Worker {idx}"), parent_agent_name=root, updated_at=idx)
            for idx, name in enumerate(children)
        )
        _write_search_team(tmp_path, team_name, members)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        default_page = await handler({"mode": "children"})
        default_payload = default_page["tool_use_result"]
        assert default_payload["limit"] == 50
        assert default_payload["total_matching"] == 205
        assert default_payload["returned"] == 50
        assert default_payload["has_more"] is True
        assert default_payload["next_offset"] == 50
        assert len(default_payload["members"]) == default_payload["returned"]
        assert len(default_payload["children"]) == default_payload["returned"]
        assert default_payload["children"] == [member["agent_name"] for member in default_payload["members"]]

        filtered_page = await handler({"mode": "children", "activity_after": 200, "limit": 2})
        filtered_payload = filtered_page["tool_use_result"]
        assert filtered_payload["total_matching"] == 4
        assert filtered_payload["returned"] == 2
        assert filtered_payload["offset"] == 0
        assert filtered_payload["next_offset"] == 2
        assert len(filtered_payload["children"]) == filtered_payload["returned"]
        assert filtered_payload["children"] == [member["agent_name"] for member in filtered_payload["members"]]

        last_page = await handler({"mode": "children", "limit": 200, "offset": 200})
        assert last_page["tool_use_result"]["returned"] == 5
        assert last_page["tool_use_result"]["has_more"] is False
        assert "next_offset" not in last_page["tool_use_result"]

        empty_page = await handler({"mode": "children", "offset": 999})
        assert empty_page["tool_use_result"]["members"] == []
        assert empty_page["tool_use_result"]["returned"] == 0
        assert empty_page["tool_use_result"]["has_more"] is False

        for args in ({"limit": 0}, {"limit": -1}, {"limit": 201}, {"offset": -1}):
            invalid = await handler({"mode": "children", **args})
            assert invalid["is_error"] is True

    @pytest.mark.asyncio
    async def test_search_team_xml_escapes_omits_unknowns_and_keeps_structured_compatibility(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-xml"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child = agent_name_for_lineage(("Root", "A & <B>"), team_key=team_name)
        _write_search_team(
            tmp_path,
            team_name,
            [
                _search_member(root, ("Root",)),
                _search_member(child, ("Root", "A & <B>"), display_name="A & <B>", parent_agent_name=root),
            ],
            inbox_names=[root, child, "bad name", ".malformed", "bad@name", "bad$name"],
        )
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "children"})
        xml_root = ET.fromstring(result["content"][0]["text"])
        xml_members = xml_root.findall("member")
        assert len(xml_members) == 1
        assert xml_members[0].attrib["display_name"] == "A & <B>"
        assert "session_id" not in xml_members[0].attrib
        assert "last_activity_epoch" not in xml_members[0].attrib
        assert result["content"][0]["text"].count("<member ") == 1
        compatibility = result["tool_use_result"]
        assert compatibility["children"] == [child]
        assert len(compatibility["members"]) == 1
        assert all("/" not in member["agent_name"] for member in compatibility["members"])
        assert all(".." not in member["agent_name"] for member in compatibility["members"])
        assert all("malformed" not in member["agent_name"] for member in compatibility["members"])
        tree_result = await handler({"mode": "tree"})
        tree_payload = tree_result["tool_use_result"]
        assert tree_payload["tree"] == sorted([root, child])
        assert {member["agent_name"] for member in tree_payload["tree_members"]} == {root, child}
        assert "legacy_json" not in compatibility

    @pytest.mark.parametrize("member_count", [1000, 5000])
    @pytest.mark.asyncio
    async def test_search_team_large_tree_keeps_xml_page_bounded(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
        member_count,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = f"team-large-{member_count}"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        synthetic = {root: {"team_name": team_name, "agent_name": root, "lineage": ["Root"], "lineage_length": 1}}
        prefix = lineage_fingerprint(("Root",))
        for idx in range(member_count):
            name = f"{prefix}-worker-{idx:04d}"
            synthetic[name] = {
                "team_name": team_name,
                "agent_name": name,
                "display_name": f"Worker {idx}",
                "lineage": ["Root", f"Worker {idx}"],
                "lineage_length": 2,
            }
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr("obs_agent.tools._load_team_projection_metadata", lambda _: synthetic)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "tree"})
        payload = result["tool_use_result"]
        xml_text = result["content"][0]["text"]
        assert payload["total_matching"] == member_count + 1
        assert payload["offset"] == 0
        assert payload["limit"] == 50
        assert payload["returned"] == 50
        assert payload["has_more"] is True
        assert payload["next_offset"] == 50
        assert len(payload["members"]) == 50
        assert len(payload["tree"]) == 50
        assert len(payload["tree_members"]) == 50
        assert [member["agent_name"] for member in payload["tree_members"]] == [
            member["agent_name"] for member in payload["members"]
        ]
        assert len(json.dumps(payload)) < 100_000
        assert len(xml_text) < 100_000
        xml_root = ET.fromstring(xml_text)
        assert xml_root.attrib["total_matching"] == str(member_count + 1)
        assert xml_root.attrib["offset"] == "0"
        assert xml_root.attrib["limit"] == "50"
        assert xml_root.attrib["returned"] == "50"
        assert xml_root.attrib["has_more"] == "true"
        assert xml_root.attrib["next_offset"] == "50"
        assert len(xml_root.findall("member")) == 50

        next_page = await handler({"mode": "tree", "offset": payload["next_offset"]})
        next_payload = next_page["tool_use_result"]
        next_xml_text = next_page["content"][0]["text"]
        assert next_payload["total_matching"] == member_count + 1
        assert next_payload["offset"] == 50
        assert next_payload["limit"] == 50
        assert next_payload["returned"] == 50
        assert next_payload["has_more"] is True
        assert next_payload["next_offset"] == 100
        assert len(next_payload["members"]) == 50
        assert len(next_payload["tree"]) == 50
        assert len(next_payload["tree_members"]) == 50
        assert [member["agent_name"] for member in next_payload["tree_members"]] == [
            member["agent_name"] for member in next_payload["members"]
        ]
        assert len(json.dumps(next_payload)) < 100_000
        assert len(next_xml_text) < 100_000
        next_xml_root = ET.fromstring(next_xml_text)
        assert next_xml_root.attrib["total_matching"] == str(member_count + 1)
        assert next_xml_root.attrib["offset"] == "50"
        assert next_xml_root.attrib["limit"] == "50"
        assert next_xml_root.attrib["returned"] == "50"
        assert next_xml_root.attrib["has_more"] == "true"
        assert next_xml_root.attrib["next_offset"] == "100"
        assert len(next_xml_root.findall("member")) == 50

    @pytest.mark.asyncio
    async def test_search_team_preserves_unknown_running_and_running_only_requires_explicit_true(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from obs_agent.tools import create_obs_tools

        team_name = "team-tri-running"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        names = {
            label: agent_name_for_lineage(("Root", label.title()), team_key=team_name)
            for label in ("explicit-true", "explicit-false", "missing", "null-val", "string-val", "true-no-status")
        }
        members = [_search_member(root, ("Root",))]
        for label, name in names.items():
            members.append(_search_member(name, ("Root", label.title()), parent_agent_name=root))
        _write_search_team(tmp_path, team_name, members)

        provider = {
            (team_name, names["explicit-true"]): {
                "team_name": team_name, "agent_name": names["explicit-true"],
                "running": True, "runtime_status": "running",
            },
            (team_name, names["explicit-false"]): {
                "team_name": team_name, "agent_name": names["explicit-false"],
                "running": False, "runtime_status": "idle",
            },
            (team_name, names["missing"]): {
                "team_name": team_name, "agent_name": names["missing"],
            },
            (team_name, names["null-val"]): {
                "team_name": team_name, "agent_name": names["null-val"],
                "running": None, "runtime_status": "completed",
            },
            (team_name, names["string-val"]): {
                "team_name": team_name, "agent_name": names["string-val"],
                "running": "yes", "runtime_status": "banana",
            },
            (team_name, names["true-no-status"]): {
                "team_name": team_name, "agent_name": names["true-no-status"],
                "running": True,
            },
        }
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        monkeypatch.setattr(
            "obs_agent.tools.find_session_jsonl_index",
            lambda *, session_ids, cwd: {},
        )
        state = HookState(team_status_provider=lambda **_: provider)
        create_obs_tools(skill_config, lambda: "caller", hook_state=state)
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "children"})
        payload = result["tool_use_result"]
        by_name = {m["agent_name"]: m for m in payload["members"]}

        # Explicit True preserved
        assert by_name[names["explicit-true"]]["running"] is True
        assert by_name[names["explicit-true"]]["runtime_status"] == "running"

        # Explicit False preserved
        assert by_name[names["explicit-false"]]["running"] is False
        assert by_name[names["explicit-false"]]["runtime_status"] == "idle"

        # Missing running becomes None, not False
        assert by_name[names["missing"]]["running"] is None
        assert by_name[names["missing"]]["runtime_status"] == "unknown"

        # Null running becomes None, existing valid runtime_status preserved
        assert by_name[names["null-val"]]["running"] is None
        assert by_name[names["null-val"]]["runtime_status"] == "completed"

        # Non-boolean running becomes None, invalid runtime_status becomes unknown
        assert by_name[names["string-val"]]["running"] is None
        assert by_name[names["string-val"]]["runtime_status"] == "unknown"

        # True with absent runtime_status → status inferred as "running"
        assert by_name[names["true-no-status"]]["running"] is True
        assert by_name[names["true-no-status"]]["runtime_status"] == "running"

        # XML omits running when null, includes when boolean
        xml_root = ET.fromstring(result["content"][0]["text"])
        xml_by_name = {m.attrib["agent_name"]: m for m in xml_root.findall("member")}
        assert xml_by_name[names["explicit-true"]].attrib["running"] == "true"
        assert xml_by_name[names["explicit-false"]].attrib["running"] == "false"
        assert "running" not in xml_by_name[names["missing"]].attrib
        assert "running" not in xml_by_name[names["null-val"]].attrib
        assert "running" not in xml_by_name[names["string-val"]].attrib
        assert xml_by_name[names["true-no-status"]].attrib["running"] == "true"

        # running_only selects only explicit True
        running_result = await handler({"mode": "children", "running_only": True})
        running_names = {m["agent_name"] for m in running_result["tool_use_result"]["members"]}
        assert running_names == {names["explicit-true"], names["true-no-status"]}

    @pytest.mark.asyncio
    async def test_search_team_snapshot_cursor_is_stable_across_activity_and_membership_mutation(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from collections import OrderedDict
        from obs_agent.tools import create_obs_tools

        team_name = "team-cursor-stable"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child_names = [
            agent_name_for_lineage(("Root", f"W {i}"), team_key=team_name) for i in range(5)
        ]
        members = [_search_member(root, ("Root",))]
        for i, name in enumerate(child_names):
            members.append(
                _search_member(name, ("Root", f"W {i}"), parent_agent_name=root, updated_at=i)
            )
        team_dir = _write_search_team(tmp_path, team_name, members)

        monkeypatch.setattr("obs_agent.tools._cursor_store", OrderedDict())
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        # Page 1: get first 2 of 5 children, should return next_cursor
        page1 = await handler({"mode": "children", "limit": 2})
        p1 = page1["tool_use_result"]
        assert p1["returned"] == 2
        assert p1["total_matching"] == 5
        assert p1["has_more"] is True
        assert "next_cursor" in p1
        cursor = p1["next_cursor"]
        page1_names = [m["agent_name"] for m in p1["members"]]

        # Mutate the underlying team: add a member, rewrite config
        new_child = agent_name_for_lineage(("Root", "New"), team_key=team_name)
        members.append(_search_member(new_child, ("Root", "New"), parent_agent_name=root, updated_at=999))
        _write_search_team(tmp_path, team_name, members)

        # Page 2 via cursor: frozen snapshot, unaffected by mutation
        page2 = await handler({"cursor": cursor})
        p2 = page2["tool_use_result"]
        assert p2["total_matching"] == 5  # frozen, not 6
        assert p2["returned"] == 2
        page2_names = [m["agent_name"] for m in p2["members"]]
        assert not set(page1_names) & set(page2_names)  # no duplicates

        # Page 3 via cursor: remaining 1 member
        all_cursor_names = list(page1_names)
        if p2.get("has_more"):
            cursor2 = p2["next_cursor"]
            page3 = await handler({"cursor": cursor2})
            p3 = page3["tool_use_result"]
            assert p3["total_matching"] == 5
            page3_names = [m["agent_name"] for m in p3["members"]]
            all_cursor_names += page2_names + page3_names
        else:
            all_cursor_names += page2_names

        # No member duplicated or skipped relative to frozen snapshot
        assert len(all_cursor_names) == len(set(all_cursor_names))
        assert set(all_cursor_names) == set(child_names)

        # A fresh live query now sees the new member
        fresh = await handler({"mode": "children"})
        assert fresh["tool_use_result"]["total_matching"] == 6

    @pytest.mark.asyncio
    async def test_search_team_snapshot_cursor_rejects_malformed_expired_and_mixed_requests(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        from collections import OrderedDict
        from obs_agent.tools import create_obs_tools

        team_name = "team-cursor-reject"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child_names = [
            agent_name_for_lineage(("Root", f"W {i}"), team_key=team_name) for i in range(5)
        ]
        members = [_search_member(root, ("Root",))]
        for i, name in enumerate(child_names):
            members.append(
                _search_member(name, ("Root", f"W {i}"), parent_agent_name=root, updated_at=i)
            )
        _write_search_team(tmp_path, team_name, members)

        monkeypatch.setattr("obs_agent.tools._cursor_store", OrderedDict())
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        wall = [1000.0]
        monkeypatch.setattr("obs_agent.tools.time.time", lambda: wall[0])
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        # Get a valid cursor
        p1 = await handler({"mode": "children", "limit": 2})
        assert p1["tool_use_result"]["has_more"] is True
        token = p1["tool_use_result"]["next_cursor"]

        # Malformed token fails closed
        r = await handler({"cursor": "bogus-token-xyz"})
        assert r["is_error"] is True

        # Cursor + any other argument is rejected
        r = await handler({"cursor": token, "mode": "tree"})
        assert r["is_error"] is True
        assert "must not include other" in r["content"][0]["text"]

        r = await handler({"cursor": token, "limit": 10})
        assert r["is_error"] is True

        # Valid cursor succeeds (consumes the token)
        r = await handler({"cursor": token})
        assert not r.get("is_error")
        assert r["tool_use_result"]["total_matching"] == 5

        # Reuse consumed cursor fails (no live-query fallback)
        r = await handler({"cursor": token})
        assert r["is_error"] is True

        # Expired token: get new cursor, advance time past TTL
        p2 = await handler({"mode": "children", "limit": 2})
        token2 = p2["tool_use_result"]["next_cursor"]
        wall[0] = 1000.0 + 301  # 301 > 300 TTL
        r = await handler({"cursor": token2})
        assert r["is_error"] is True

    @pytest.mark.asyncio
    async def test_search_team_resolves_and_parses_nonempty_real_session_jsonl_end_to_end(
        self,
        monkeypatch,
        skill_config,
        tmp_path,
    ):
        import os
        from obs_agent.context_jsonl import _encode_project_path
        from obs_agent.tools import create_obs_tools

        team_name = "team-real-jsonl"
        root = agent_name_for_lineage(("Root",), team_key=team_name)
        child = agent_name_for_lineage(("Root", "Worker"), team_key=team_name)
        session_id = "sid-real-worker"

        members = [
            _search_member(root, ("Root",)),
            _search_member(child, ("Root", "Worker"), parent_agent_name=root, session_id=session_id),
        ]
        _write_search_team(tmp_path, team_name, members)

        # Create real nonempty JSONL in the correct Claude projects directory
        cwd = skill_config.vault_path
        project_slug = _encode_project_path(cwd)
        projects_root = tmp_path / ".claude" / "projects"
        project_dir = projects_root / project_slug
        project_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = project_dir / f"{session_id}.jsonl"
        jsonl_lines = [
            json.dumps({"type": "user", "timestamp": 100.0, "message": {"content": "hello"}}),
            json.dumps({"type": "assistant", "timestamp": 200.0, "message": {"content": [{"type": "text", "text": "reply"}]}}),
            "not-valid-json",
            json.dumps({"type": "assistant", "timestamp": "not-a-timestamp"}),
            json.dumps({"type": "assistant", "timestamp": float("inf")}),
            json.dumps({"type": "assistant", "timestamp": 300.0, "message": {"content": [{"type": "text", "text": "final"}]}}),
        ]
        jsonl_path.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")

        # Set file mtime to a DISTINCT value lower than the best event timestamp
        os.utime(jsonl_path, (50.0, 50.0))

        # Do NOT monkeypatch find_session_jsonl_index — use the real function
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "obs_agent.tools.find_latest_obs_bootstrap_for_session",
            lambda **_: _search_bootstrap(team_name, root, ("Root",)),
        )
        create_obs_tools(skill_config, lambda: "caller", hook_state=HookState())
        handler = _tool_handler(captured["tools"], "search_team")

        result = await handler({"mode": "children"})
        payload = result["tool_use_result"]
        member = next(m for m in payload["members"] if m["agent_name"] == child)

        # Valid event timestamp (300.0) beats distinct file mtime (50.0)
        assert member["last_activity_epoch"] == 300.0
        assert member["last_activity_source"] == "jsonl_event_timestamp"
        assert member["last_activity_epoch"] != 50.0

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
            "PreToolUse": str(skill_config.vault_path / "guard.py") + "::check_access",
            "PostToolUse": str(skill_config.vault_path / "log.py") + "::log_result",
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
