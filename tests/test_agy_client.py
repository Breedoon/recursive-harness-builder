import json
from pathlib import Path

import pytest

from obs_agent import agy_client
from obs_agent.agy_client import AgyClient, AgyResultMessage


def test_models_are_explicitly_scoped():
    assert agy_client.is_agy_model("gemini")
    assert not agy_client.is_agy_model("terra")
    with pytest.raises(agy_client.AgyError, match="not available"):
        AgyClient("gemini-pro", Path("/"), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_result_and_denied_action(tmp_path, monkeypatch, denied):
    executable = tmp_path / "agy"
    executable.write_text('''#!/usr/bin/env python3
import json, sys
print(json.dumps({"event":"init","conversation_id":"test-session","init":{"model":"gemini-3.8-flash-low","permission_mode":"strict"}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    assert "reasoning-only" in msg["message"]["content"]
    print(json.dumps({"event":"result","result":{"conversation_id":"test-session","status":"SUCCESS","response":"hello","denied_actions":DENIED}}), flush=True)
'''.replace("DENIED", repr([{"action": "read_file"}] if denied else [])))
    executable.chmod(0o755)
    monkeypatch.setattr(agy_client, "AGY_BINARY", str(executable))
    client = AgyClient("gemini-3.8-flash-low", tmp_path, {})
    await client.connect()
    try:
        await client.query("test")
        messages = [message async for message in client.receive_response()]
        assert isinstance(messages[-1], AgyResultMessage)
        assert messages[-1].is_error is denied
        assert ("reasoning only" in messages[-1].result) is denied
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_unverified_permissions_fail_closed(tmp_path, monkeypatch):
    executable = tmp_path / "agy"
    executable.write_text('''#!/usr/bin/env python3
import json
print(json.dumps({"event":"init","conversation_id":"test-session","init":{"model":"gemini-3.8-flash-low","permission_mode":"auto"}}), flush=True)
''')
    executable.chmod(0o755)
    monkeypatch.setattr(agy_client, "AGY_BINARY", str(executable))
    client = AgyClient("gemini-3.8-flash-low", tmp_path, {})
    with pytest.raises(RuntimeError, match="strict permissions"):
        await client.connect()


@pytest.mark.asyncio
@pytest.mark.real_get_client
async def test_manager_rejects_hooks_and_existing_session(tmp_path):
    from obs_agent.config import OBSConfig
    from obs_agent.session import SessionManager
    from obs_agent.runner import _is_recoverable

    manager = SessionManager(config=OBSConfig(vault_path=tmp_path))
    manager.model_override = "gemini-3.8-flash-low"
    manager.user_hooks = {"PreToolUse": "example.py::check"}
    with pytest.raises(agy_client.AgyError, match="hooks") as exc:
        await manager.get_client()
    assert not _is_recoverable(exc.value)
    manager.user_hooks = None
    manager.set_session_id("existing-session")
    with pytest.raises(agy_client.AgyError, match="resume"):
        await manager.get_client()


@pytest.mark.asyncio
@pytest.mark.real_get_client
async def test_denied_tool_result_fails_runner_turn(tmp_path, monkeypatch):
    from obs_agent.config import OBSConfig
    from obs_agent.hooks import HookState
    from obs_agent.runner import ConversationRunner, TextEvent, TurnEndEvent
    from obs_agent.session import SessionManager

    executable = tmp_path / "agy"
    executable.write_text('''#!/usr/bin/env python3
import json, sys
print(json.dumps({"event":"init","conversation_id":"test-session","init":{"model":"gemini-3.8-flash-low","permission_mode":"strict"}}), flush=True)
for line in sys.stdin:
    print(json.dumps({"event":"result","result":{"conversation_id":"test-session","status":"SUCCESS","response":"","denied_actions":[{"action":"read_file"}]}}), flush=True)
''')
    executable.chmod(0o755)
    monkeypatch.setattr(agy_client, "AGY_BINARY", str(executable))
    config = OBSConfig(vault_path=tmp_path)
    state = HookState()
    manager = SessionManager(config=config, hook_state=state)
    manager.model_override = "gemini-3.8-flash-low"
    manager.effort_override = "low"
    runner = ConversationRunner(manager, state, config)
    try:
        events = [event async for event in runner.run("Read a file")]
        errors = [event for event in events if isinstance(event, TurnEndEvent) and event.is_error]
        text = [event.text for event in events if isinstance(event, TextEvent)]
        assert len(errors) == 1
        assert "tool access was denied" in errors[0].error_text
        assert len(text) == 1 and "tool access was denied" in text[0]
    finally:
        await manager.disconnect()
