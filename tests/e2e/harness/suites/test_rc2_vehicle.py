"""VEHICLE ONLY (branch obs-22-rc2-seed; never gated, never promoted): lane proofs for E-RC2 that
are not percentage selection, run with ``--code`` on rc2 (the code under test) or on this vehicle.

- ``test_stop_reaps_live_tool_child``: a /stop while a Bash tool call holds a child that ignores
  TERM/INT/HUP in its own session. On rc2 the stop kill must leave survivors 0 (and the lane log
  names the killed pids); on this vehicle with profile ``rc2-seed-no-kill`` the kill is skipped, so
  the same test must FAIL with survivors > 0 (obs-22.122 seeded failure: the corrected metric can
  still fail).
- ``test_trusted_hook_launch``: an AgentTask launched with the verifier_reasoning_guard hooks string
  starts and answers; an untrusted path/function pair is still refused at launch.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from tests.e2e.harness.suites.test_obs22_agent_shapes import _launch
from tests.e2e.harness.suites.test_tap_fail_open import _messages, _until_changed

pytestmark = [pytest.mark.suite("rc2-vehicle"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]

GUARD = "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/verifier_reasoning_guard.py"
UNTRUSTED = "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/credit_state_handoff.py::check"


def _nonce():
    return uuid.uuid4().hex[:8].upper()


def _log_since(lane, mark):
    log = lane.lane_dir / "logs" / "telegram-prod.log"
    with log.open("rb") as handle:
        handle.seek(mark)
        return handle.read().decode("utf-8", "replace")


def _log_size(lane):
    log = lane.lane_dir / "logs" / "telegram-prod.log"
    return log.stat().st_size if log.exists() else 0


async def test_stop_reaps_live_tool_child(lane):
    nonce = _nonce()
    root = await lane.new_topic(f"rc2 stop child {nonce}")
    primed = await lane.turn(root, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in primed.text and primed.end.get("session_id"), primed.end
    # Root chats are reasoning-only (no Bash); an executor-hooked child runs the tool, as in
    # test_obs22_agent_shapes.test_maintenance_restart_mid_tool_recovers_by_fork.
    child = await _launch(lane, root, primed, {"display_name": f"rc2 stop child {nonce}", "fork": False,
                          "effort": "low", "hooks": json.dumps({"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}),
                          "prompt": f"Reply with exactly: CHILD-READY-{nonce}"}, nonce)
    child_end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not child_end.get("failed"), child_end
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CHILD-READY-{nonce}"))
    topic = int(child["topic_id"])
    tag = f"rc2seed{nonce.lower()}"
    # A background Bash task outlives the CLI's own interrupt of the foreground tool (the first
    # vehicle run showed the CLI tree-kills the foreground tool's descendants itself), so only
    # OBS's stop kill can reap it.
    background = f"exec -a {tag} sleep 600"
    log_mark = _log_size(lane)
    sent = await lane.send(topic, "Make exactly two Bash tool calls, one after the other, and use no other tool. "
                           f"First: the Bash tool with run_in_background set to true and exactly this command: `{background}`. "
                           f"Second, after the first has started: the Bash tool in the foreground with exactly this command: "
                           f"`sleep 60; echo SLEPT`. After both reply exactly DONE-{nonce}.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    # Wait inside the lane (its own PID namespace) until the tagged child exists; at most ~120 s.
    waited = await lane.exec("sh", "-c", f"n=0; until ps -eo args= | grep -q '^{tag}'; do n=$((n+1)); "
                             f"[ $n -gt 600 ] && echo MISSING && exit 0; sleep 0.2; done; echo PRESENT", timeout=150)
    before = [r for r in await lane.procs() if (r.get("args") or [None])[0] == tag]
    if "PRESENT" not in (waited.get("stdout") or ""):
        text = _log_since(lane, log_mark)
        pytest.fail(f"the seeded child never started: {waited} {[l[-300:] for l in text.splitlines() if 'flush_turn' in l]}")
    stop = await lane.command(topic, "/stop")
    end, cleanup = await asyncio.gather(
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=child["agent_name"], after=start),
    )
    after = [r for r in await lane.procs() if (r.get("args") or [None])[0] == tag]
    text = _log_since(lane, log_mark)
    proof = {"run_id": lane.manifest["run_id"], "lane": lane.name, "code_dir": lane.info.get("code_dir"),
             "profile": lane.profile.name, "stop_cleanup": cleanup, "child_before": before, "child_after": after,
             "escalation_lines": [l.split("] ", 1)[-1][:300] for l in text.splitlines() if "[stop_escalation]" in l]}
    for row in after:  # leave nothing behind in the lane
        await lane.exec("sh", "-c", f"kill -9 {int(row['pid'])} 2>/dev/null; true", timeout=10)
    path = lane.run_dir / "tests" / lane.name / "rc2-stop-child.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    assert before, proof  # the seeded child existed when /stop was sent
    assert stop.message_id > sent.message_id
    assert cleanup.get("survivors") == 0, proof


async def test_settings_hook_probe(lane):
    """Diagnostic: which fixture settings PreToolUse:Bash hook denies the seeded command in a lane."""
    command = ("setsid bash -c 'trap \"\" TERM INT HUP; exec -a rc2seedprobe sleep 600' </dev/null >/dev/null 2>&1 & "
               "sleep 60; echo SLEPT")
    payload = {"session_id": "00000000-0000-0000-0000-000000000000", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "cwd": "/workspace/runtime/git/obs-vault-active",
               "tool_input": {"command": command, "description": "probe"}}
    (lane.lane_dir / "state" / "rc2-hook-probe.json").write_text(json.dumps(payload))
    ops = "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops"
    script = "; ".join(
        f"python3 \"{ops}/{name}.py\" {arg} < /workspace/runtime/state/rc2-hook-probe.json > /tmp/o 2> /tmp/e; "
        f"echo \"== {name} exit=$? out=$(head -c 300 /tmp/o) err=$(head -c 600 /tmp/e)\""
        for name, arg in (("bash_guard", "hook"), ("freeze", "hook"), ("safe_sync", "hook"), ("shared_guard", "hook"),
                          ("checkpoint_hook", "pre")))
    result = await lane.exec("bash", "-c", script, timeout=60,
                             env={"CLAUDE_CODE_AGENT_NAME": "rc2-probe", "CLAUDE_CODE_TEAM_NAME": "rc2-probe"})
    path = lane.run_dir / "tests" / lane.name / "rc2-hook-probe.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    assert "exit=2" not in (result.get("stdout") or ""), result


async def test_trusted_hook_launch(lane):
    nonce = _nonce()
    topic = await lane.new_topic(f"rc2 hook {nonce}")
    parent = await lane.turn(topic, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in parent.text and parent.end.get("session_id"), parent.end
    hooks = json.dumps({"PreToolUse": f"{GUARD}::check"})
    proof: dict = {"run_id": lane.manifest["run_id"], "lane": lane.name, "code_dir": lane.info.get("code_dir"), "hooks": hooks}
    try:
        child = await _launch(lane, topic, parent, {"display_name": f"rc2 hook child {nonce}", "fork": False,
                              "effort": "low", "hooks": hooks, "prompt": f"Reply with exactly: GUARDED-{nonce}"}, nonce)
        end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
        proof["trusted"] = {"child_start": child, "child_end": end}
        assert not end.get("failed") and end.get("session_id"), end
        await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"GUARDED-{nonce}"))

        # A fresh parent: the first one is busy with the child's completion notice for a while.
        topic2 = await lane.new_topic(f"rc2 hook untrusted {nonce}")
        parent2 = await lane.turn(topic2, f"Reply with exactly: READY2-{nonce}")
        assert f"READY2-{nonce}" in parent2.text, parent2.messages
        mark, log_mark = lane.events.mark(), _log_size(lane)
        bad = json.dumps({"PreToolUse": UNTRUSTED})
        refused = await lane.turn(topic2, "Call the `mcp__obs-agent__AgentTask` tool exactly once with these exact arguments, "
                                  "then reply with the tool result's text verbatim; use no other tool.\n"
                                  + json.dumps({"display_name": f"rc2 untrusted {nonce}", "fork": False, "effort": "low",
                                                "hooks": bad, "prompt": "Reply with exactly: SHOULD-NOT-RUN"}))
        log = _log_since(lane, log_mark)
        rejected = [l.split("] ", 1)[-1][:300] for l in log.splitlines() if "Rejected user hook" in l]
        new_agents = [e for e in lane.find("turn_start", after=mark)
                      if e.get("agent_name") not in (None, parent.end["agent_name"], parent2.end["agent_name"],
                                                     child["agent_name"])]
        proof["untrusted"] = {"reply": refused.text[:400], "rejected_log": rejected, "new_agent_starts": new_agents}
        assert any("credit_state_handoff.py" in l and "not a trusted hook capability" in l for l in rejected), proof
        assert not new_agents, proof
    finally:
        path = lane.run_dir / "tests" / lane.name / "rc2-hook.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(proof, indent=2, default=str) + "\n")
