"""Start-step check on root AgentTask launches (bead vault-a6qm)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from obs_agent import start_step
from obs_agent.hooks import HookState
from obs_agent.lineage import build_obs_bootstrap_xml


@pytest.fixture
def beads(tmp_path, monkeypatch):
    path = tmp_path / "issues.jsonl"
    path.write_text("\n".join(json.dumps({"id": i, "status": "open"}) for i in ("ops-1.5", "vault-maw0", "ac-1")) + "\n")
    monkeypatch.setenv("OBS_BEADS_ISSUES_FILE", str(path))
    monkeypatch.setenv("OBS_START_STEP_LOG", str(tmp_path / "warnings.jsonl"))
    monkeypatch.delenv("OBS_START_STEP_CHECK", raising=False)
    return path


def test_ids_confirmed_against_store(beads, tmp_path):
    known = start_step.known_bead_ids(tmp_path)
    assert start_step.find_bead_ids("see ops-1.5 and vault-maw0.", known) == {"ops-1.5", "vault-maw0"}
    assert start_step.find_bead_ids("re-run the cache-key check, e-mail me", known) == set()
    assert start_step.find_bead_ids("x-ac-1 b41d7e9d27-ac-1", known) == set()


def test_transcript_scan(beads, tmp_path):
    known = start_step.known_bead_ids(tmp_path)
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": "work on ac-1 please"}}) + "\n")
    assert start_step.transcript_has_bead(jsonl, known)
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": "no id here"}}) + "\n")
    assert not start_step.transcript_has_bead(jsonl, known)


def test_injected_context_does_not_count(beads, tmp_path):
    """CLAUDE.md (entry-file context) names real bead ids; every root transcript carries it, so it
    must not exempt the root (live defect 2026-10-16, ops-1.12: the warning never fired)."""
    known = start_step.known_bead_ids(tmp_path)
    jsonl = tmp_path / "s.jsonl"
    injected = (
        '<obs-agent-entry-file-context source="CLAUDE.md">\nmission epic vault-maw0, ac-1\n'
        "</obs-agent-entry-file-context>\n<obs-platform-context>\nsee ops-1.5\n</obs-platform-context>\n"
    )
    jsonl.write_text(
        json.dumps({"type": "queue-operation", "content": injected + "please fix the login"}) + "\n"
        + json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": injected},
                                                              {"type": "text", "text": "no id here"}]}}) + "\n"
    )
    assert not start_step.transcript_has_bead(jsonl, known)
    # The user's own words in the same message still count.
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": injected}, {"type": "text", "text": "continue ac-1"}]}}) + "\n")
    assert start_step.transcript_has_bead(jsonl, known)


def _tools(monkeypatch, config, *, lineage, jsonl=None):
    from obs_agent.tools import create_obs_tools

    captured = {}

    def fake_server(name, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", fake_server)
    monkeypatch.setattr(
        "obs_agent.tools.find_session_jsonl_index", lambda **kw: {s: jsonl for s in kw["session_ids"]}
    )
    state = HookState()
    state.session_id = "sid-root"
    state.pending_obs_bootstrap_xml = build_obs_bootstrap_xml(
        lineage=lineage,
        origin="trunk_start",
        is_fork=False,
        session_id="sid-root",
        root_team_key="2026-10-15-10-00-root",
        agent_name="2026-10-15-10-00-root",
    )
    state.fork_task_launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "AgentTask launched."}]})
    create_obs_tools(config, lambda: "sid-root", hook_state=state)
    handler = next(t.handler for t in captured["tools"] if t.name == "AgentTask")
    return handler, state


@pytest.mark.asyncio
async def test_root_without_bead_warns_and_launches(beads, monkeypatch, config, tmp_path):
    jsonl = tmp_path / "root.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    handler, state = _tools(monkeypatch, config, lineage=("Root",), jsonl=jsonl)
    result = await handler({"prompt": "do the thing", "display_name": "Worker"})
    state.fork_task_launcher.assert_awaited_once()
    texts = [c["text"] for c in result["content"]]
    assert texts[0] == "AgentTask launched." and "start-step check" in texts[1] and "register.py" in texts[1]
    logged = [json.loads(line) for line in (tmp_path / "warnings.jsonl").read_text().splitlines()]
    assert logged[-1]["child"] == "Worker" and logged[-1]["mode"] == "warn"


@pytest.mark.asyncio
async def test_bead_in_prompt_or_session_is_silent(beads, monkeypatch, config, tmp_path):
    handler, _ = _tools(monkeypatch, config, lineage=("Root",), jsonl=None)
    result = await handler({"prompt": "Implement vault-maw0", "display_name": "W"})
    assert len(result["content"]) == 1
    jsonl = tmp_path / "root.jsonl"
    jsonl.write_text(json.dumps({"type": "tool_result", "content": '{"task": "ops-1.5"}'}) + "\n")
    handler, _ = _tools(monkeypatch, config, lineage=("Root",), jsonl=jsonl)
    result = await handler({"prompt": "one-sentence fork prompt", "display_name": "W"})
    assert len(result["content"]) == 1


@pytest.mark.asyncio
async def test_children_unaffected_and_modes(beads, monkeypatch, config, tmp_path):
    handler, _ = _tools(monkeypatch, config, lineage=("Root", "Child"), jsonl=None)
    assert len((await handler({"prompt": "x", "display_name": "W"}))["content"]) == 1
    monkeypatch.setenv("OBS_START_STEP_CHECK", "off")
    handler, _ = _tools(monkeypatch, config, lineage=("Root",), jsonl=None)
    assert len((await handler({"prompt": "x", "display_name": "W"}))["content"]) == 1
    monkeypatch.setenv("OBS_START_STEP_CHECK", "block")
    handler, state = _tools(monkeypatch, config, lineage=("Root",), jsonl=None)
    result = await handler({"prompt": "x", "display_name": "W"})
    assert result.get("is_error") and "launch refused" in result["content"][0]["text"]
    state.fork_task_launcher.assert_not_awaited()
