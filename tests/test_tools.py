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
            "CronCreate",
            "CronList",
            "CronDelete",
            "SendInboxMessage",
            "ReadInbox",
            "ForkTask",
            "ForkTaskOutput",
            "ForkTaskStop",
            "session_info",
            "context_info",
            "session_lineage",
            "get_family",
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
        # name param is now display name (lineage), not agent_name (per naming redesign)
        assert launch_args["agent_name"] is None
        assert launch_args["description"] == "worker-a"

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
                native_agent_name="obs-agent-child-123",
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
                native_agent_name="obs-agent-root-123",
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
                native_agent_name="obs-agent-root-123",
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
            }
        )

    @pytest.mark.asyncio
    async def test_send_inbox_message_reports_undelivered_when_recipient_is_dead(
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

        # Per always-deliver design: messages always go to inbox even if validator says not deliverable
        assert "is_error" not in result  # Not an error — delivered to inbox
        inbox_path = tmp_path / ".claude" / "teams" / "team-alpha" / "inboxes" / "worker-a.json"
        assert inbox_path.exists()  # Message was written to inbox file

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
        # CronDelete is now blocked for agents (schedule rearchitecture)
        assert ("does not provide task orchestration" in delete_result["content"][0]["text"]
                or "disabled for agents" in delete_result["content"][0]["text"])

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
        # CronDelete is now blocked for agents — either "id is required" or "disabled"
        assert ("id is required" in result["content"][0]["text"]
                or "disabled for agents" in result["content"][0]["text"])
