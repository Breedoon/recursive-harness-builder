"""Paired real-OBS agent launch, inbox wake and maintenance resume probes."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.e2e.harness.suites.test_tap_fail_open import _messages, _until_changed

pytestmark = [pytest.mark.suite("obs22-shapes"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]


def _nonce():
    return uuid.uuid4().hex[:8].upper()


async def _launch(lane, topic, parent, payload, nonce):
    mark = lane.events.mark()
    sent = await lane.send(topic, "Call the `mcp__obs-agent__AgentTask` tool exactly once with these exact "
                           "arguments, then reply exactly LAUNCHED-" + nonce + "; use no other tool.\n" + json.dumps(payload))
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    child = await lane.wait("turn_start", after=mark, timeout=120,
                            match=lambda e: e.get("team") == parent.end["team"]
                            and e.get("agent_name") not in (None, parent.end["agent_name"]))
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start)
    messages = await lane.telegram.messages(topic, since=sent, from_bot=True)
    assert not end.get("failed") and f"LAUNCHED-{nonce}" in "\n".join(m.get("text") or "" for m in messages), messages
    return child


async def test_model_agenttask_fork_inherits_context(lane):
    nonce = _nonce()
    topic = await lane.new_topic(f"obs22 fork {nonce}")
    parent = await lane.turn(topic, f"Remember the codeword CW-{nonce}. Reply with exactly: OK-{nonce}")
    assert f"OK-{nonce}" in parent.text and parent.end.get("session_id"), parent.end
    child = await _launch(lane, topic, parent, {"display_name": f"obs22 fork child {nonce}", "fork": True,
                          "prompt": "Reply with the codeword you were given earlier and nothing else."}, nonce)
    end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    messages = await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CW-{nonce}"))
    assert messages and child["topic_id"] != topic.topic_id
    assert end.get("session_id") and end["session_id"] != parent.end["session_id"], (parent.end, end)
    assert not end.get("failed"), end


async def test_model_fresh_child_inbox_wake(lane):
    nonce = _nonce()
    topic = await lane.new_topic(f"obs22 wake {nonce}")
    parent = await lane.turn(topic, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in parent.text, parent.messages
    child = await _launch(lane, topic, parent, {"display_name": f"obs22 wake child {nonce}", "fork": False,
                          "effort": "low", "prompt": f"Reply with exactly: CHILD-UP-{nonce}"}, nonce)
    end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not end.get("failed") and end.get("session_id"), end
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CHILD-UP-{nonce}"))
    mark = lane.events.mark()
    request = await lane.send(topic, "Call `mcp__obs-agent__SendInboxMessage` exactly once for the agent you just "
                              f"launched, with content 'Reply in your topic with exactly: WOKE-{nonce}'. "
                              f"Then reply exactly: SENT-{nonce}. Use no other tool.")
    await _until_changed(lane, lambda: _messages(lane, topic, request, f"SENT-{nonce}"))
    delivered = await lane.wait("inbox_delivered", agent=child["agent_name"], after=mark, match={"mode": "wake"})
    woke = await lane.wait("turn_end", agent=child["agent_name"], after=delivered)
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"WOKE-{nonce}"))
    assert not woke.get("failed") and woke.get("session_id") == end["session_id"], (end, woke)


async def test_maintenance_restart_mid_tool_recovers_by_fork(lane):
    nonce = _nonce()
    topic = await lane.new_topic(f"obs22 resume {nonce}")
    primed = await lane.turn(topic, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in primed.text and primed.end.get("session_id"), primed.end
    child = await _launch(lane, topic, primed, {"display_name": f"obs22 resume child {nonce}", "fork": False,
                          "effort": "low", "hooks": json.dumps({"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}),
                          "prompt": f"Reply with exactly: CHILD-READY-{nonce}"}, nonce)
    child_end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not child_end.get("failed") and child_end.get("session_id"), child_end
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CHILD-READY-{nonce}"))
    marker = lane.lane_dir / "state" / f"obs22-bash-started-{nonce}"
    sent = await lane.send(child["topic_id"], f"Call Bash exactly once in the foreground with command "
                           f"`touch /workspace/runtime/state/{marker.name}; sleep 25; echo SLEPT-{nonce}`. "
                           f"After it returns reply exactly DONE-{nonce}, with no other tools.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    session_id = child_end["session_id"]
    cursor = float(start["ts"])
    async with asyncio.timeout(120):
        while True:
            output = await lane.wait("jsonl_append", match={"session_id": session_id, "type": "assistant"}, after=cursor)
            blocks = (output["row"].get("message") or {}).get("content") or []
            if any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash"
                   and marker.name in str((b.get("input") or {}).get("command", "")) for b in blocks):
                break
            cursor = output
    async def started():
        return marker.exists()
    await _until_changed(lane, started)
    mark = lane.events.mark()
    restarted = await lane.restart_obs("maintenance")
    assert restarted["ready"].get("obs") == "RUNNING", restarted
    try:
        resumed = await lane.wait("turn_end", agent=child["agent_name"], after=mark, timeout=180)
    finally:
        print(json.dumps({"restart_ready": restarted["ready"], "old_session": session_id,
                          "events": lane.find(None, agent=child["agent_name"], after=start)}, default=str))
    sends = lane.find("telegram_send", agent=child["agent_name"], after=mark)
    messages = await lane.telegram.messages(child["topic_id"], since=sent, from_bot=True)
    print(json.dumps({"resumed_end": resumed, "messages": messages}, default=str))
    # An interrupted Bash call can leave an unsafe transcript tail; OBS's run-start check then forks a
    # new session up to a resolved safe target instead of resuming the old id, unless recovery is
    # refused or the fork fails (state machines v2.5, OBS Restart and Recovery: "Run start →
    # recovery fork"). This test pins the forked outcome both CLIs showed; parity is decision D13.
    recoveries = lane.find("recovery", agent=child["agent_name"], after=mark)
    assert any(r.get("outcome") == "forked" and r.get("reason") == "incomplete_tool_use_tail"
               for r in recoveries), recoveries
    assert resumed.get("session_id") and resumed["session_id"] != session_id and not resumed.get("failed"), resumed
    assert sends and any(f"DONE-{nonce}" in (m.get("text") or "") for m in messages), messages
