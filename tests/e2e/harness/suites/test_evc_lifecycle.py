"""EV-C native R13 constructions. Raw observations only; no obs-cli claim."""
from __future__ import annotations

import asyncio
import json
import shlex
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.harness.events import EventLog, FileTail
from tests.e2e.harness.suites.test_isolation import (
    test_i_lane_sees_only_its_own_processes,
    test_ii_host_pid_is_unreachable,
    test_iii_lane_env_is_the_allow_list,
)

pytestmark = [pytest.mark.suite("evc-lifecycle", "isolation")]
PYTHON = "/workspace/obs/.venv/bin/python"
HELPER_REL = "tests/e2e/harness/evc_fixture.py"
PAIRS = [("RC-prod", "S2"), ("RC-prod", "S3"), ("RC-alt", "S2"), ("RC-alt", "S3"),
         ("RC-alt", "S4"), ("RC-alt", "S5h"), ("RC-alt", "S5m")]


def _proof_path(lane, case):
    root = lane.run_dir / "tests" / lane.name / "evc"
    root.mkdir(parents=True, exist_ok=True)
    return root / (case.replace("/", "_") + ".json")


async def _helper(lane, mode, root, shape=None):
    args = [PYTHON, str(Path(lane.info["code_dir"]) / HELPER_REL), mode, "--root", root]
    if shape:
        args += ["--shape", shape]
    result = await lane.exec(*args)
    assert result["rc"] == 0, {"mode": mode, "rc": result["rc"], "stderr": result["stderr"][-1000:]}
    return json.loads(result["stdout"]) if mode in {"setup", "snapshot"} else result["stdout"]


async def _shape(lane, shape, nonce, root, signals, proof):
    topic = await lane.new_topic(f"EV-C {shape} {nonce}")
    proof["topic_id"] = topic.topic_id
    ready = await lane.turn(topic, f"Reply with exactly READY-{nonce}; do not call tools.")
    assert f"READY-{nonce}" in ready.text, ready.messages
    proof["primed_turn_end"] = ready.end
    if shape == "S3":
        prompt = "Write every integer from1 through8000, one per line, no other text and no tools."
    elif shape == "S5m":
        prompt = "Call the `mcp__evc-hold__hold_probe` tool exactly once now; no other tool, then reply DONE."
    else:
        # The fixture is in the throwaway lane vault, not the protected OBS source tree.
        command = shlex.join(["python3", "./evc_fixture.py", "hold", "--root", root])
        prompt = f"Call Bash once with command `{command}`, timeout120000, foreground only. Wait for its result."
    if shape in {"S2", "S5m"}:
        from types import SimpleNamespace
        hooks = {"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}
        mark = lane.events.mark()
        result = await lane.control("--json", "--as", "evc-fixture", "launch",
            "--parent-team", ready.end["team"], "--parent", ready.end["agent_name"],
            "--name", f"EV-C fixture {nonce}", "--fresh", "--model", "sol", "--effort", "low",
            "--hooks", json.dumps(hooks),
            "--prompt", "Role: executor. Isolated E2E lifecycle fixture only; no exploration, registration or helpers. " + prompt)
        proof["fixture_child_control"] = result
        assert result["rc"] == 0, result
        response = json.loads(result["stdout"])
        child = response["result"]
        proof["fixture_child"] = child
        start = await lane.wait("turn_start", agent=child["agent_name"], after=mark)
        topic = int(start["topic_id"])
        proof["topic_id"] = topic
        sent = SimpleNamespace(message_id=start.get("trigger_message_id"))
    else:
        sent = await lane.send(topic, prompt)
        start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    proof["trigger_message_id"] = sent.message_id
    proof["turn_start"] = start
    if shape == "S3":
        # Thinking is emitted from a real model stream before the final assistant
        # JSONL row; waiting for that final row incorrectly waits for completion.
        event = await lane.wait("telegram_send", agent=topic,
            match=lambda e: "thinking" in (e.get("text_head") or "").lower(), after=start, timeout=60)
        observed = {"kind": "model_streaming_no_tool", "ts": event["ts"], "stream_event": event}
    else:
        expected = "hook_running" if shape in {"S4", "S5h"} else "mcp_running" if shape == "S5m" else "tool_running"
        observed = await signals.wait(expected, timeout=60, after=0)
    proof["shape_signal"] = observed
    return topic, sent, start, observed


def _live_prior_tools(before, after, observed):
    prior = {p["pid"]: p for p in before["processes"] if p["pid"] == observed.get("pid")}
    return [p for p in after["processes"] if p["pid"] in prior and
            p["start"] == prior[p["pid"]]["start"] and p["state"] not in {"Z", "X"}]


async def _run(lane, config, shape, path):
    case = f"{config}/{shape}/{path}/2.1.59/R13"
    nonce = uuid.uuid4().hex[:8]
    root = f"/workspace/runtime/state/evc-{nonce}"
    local_root = lane.lane_dir / "state" / f"evc-{nonce}"
    local_root.mkdir(parents=True, exist_ok=True)
    vault = lane.lane_dir / "vault"
    saved = {name: (vault / name).read_bytes() if (vault / name).exists() else None
             for name in (".claude/settings.json", ".mcp.json")}
    signals = EventLog(local_root / "signals.jsonl", timing=lane.timing)
    signals.start()
    proof = {"cell_id": case, "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "harness_head": lane.manifest.get("code"), "started_utc_epoch": time.time(),
             "configuration_validation": "metadata recorded; EV-G/reference join still required",
             "trigger": "24cf5ca:src/obs_agent/telegram.py:5995-6011" if path != "P7" else "24cf5ca:src/obs_agent/telegram.py:14024-14033",
             "added_hook": shape in {"S4", "S5h"}, "mcp_lane_copy_mechanism": shape == "S5m"}
    target = _proof_path(lane, case)
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, shape)
        topic, sent, start, observed = await _shape(lane, shape, nonce, root, signals, proof)
        before = await _helper(lane, "snapshot", root)
        proof["pre_event"] = before
        if shape == "S4":
            assert not before["tool_started"], before
            proof["pre_event_tool_not_started"] = True
        if path in {"P1a", "P2"}:
            proof["stop_sent"] = (await lane.command(topic, "/stop")).message_id
            ack, end, cleanup = await asyncio.gather(
                lane.wait("telegram_send", agent=topic, match=lambda e: "interrupt sent" in (e.get("text_head") or "").lower(), after=observed["ts"]),
                lane.wait("turn_end", agent=topic, after=start),
                lane.wait("stop_cleanup", agent=topic, after=start, timeout=45),
            )
            proof.update(interrupt_ack=ack, turn_end=end, stop_cleanup=cleanup)
            after = await _helper(lane, "snapshot", root)
            proof["post_cleanup_proc_states"] = after
            alive = _live_prior_tools(before, after, observed)
            proof["live_prior_tools_excluding_Z_X"] = alive
            assert not alive, alive
        elif path == "P7":
            log_path = lane.lane_dir / "logs/telegram-prod.log"
            offset = log_path.stat().st_size
            proof["restart_requested_epoch"] = time.time()
            proof["next_daemon_ready"] = await lane.restart_obs("maintenance")
            future = asyncio.get_running_loop().create_future()
            def line(text, seen):
                if "[orphan_reaper]" in text and ("no orphaned" in text or "terminated orphaned" in text):
                    if not future.done():
                        future.set_result({"line": text[-1200:], "observed_epoch": seen})
            tail = FileTail(log_path, line)
            tail.offset = offset
            tail.start()
            try:
                proof["next_reaper_terminal"] = await asyncio.wait_for(future, 60)
            finally:
                tail.close()
            after = await _helper(lane, "snapshot", root)
            proof["after_next_reaper"] = after
            prior = {p["pid"]: p for p in before["processes"] if p.get("cli") or p["pid"] == observed.get("pid")}
            alive = [p for p in after["processes"] if p["pid"] in prior and p["start"] == prior[p["pid"]]["start"] and p["state"] not in {"Z", "X"}]
            assert not alive, alive
            await _helper(lane, "release", root)
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2000:])
        raise
    finally:
        if proof.get("raw_status") == "RAN-FAIL" and proof.get("topic_id"):
            try:
                proof["failure_cleanup_stop"] = (await lane.command(proof["topic_id"], "/stop")).message_id
            except Exception as exc:
                proof["failure_cleanup_error"] = type(exc).__name__
        proof["finished_epoch"] = time.time()
        target.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        signals.close()
        for name, original in saved.items():
            if original is None:
                (vault / name).unlink(missing_ok=True)
            else:
                (vault / name).write_bytes(original)


@pytest.mark.parametrize("configuration,shape", PAIRS)
async def test_native_stop_shapes(lane, configuration, shape):
    await _run(lane, configuration, shape, "P1a")


@pytest.mark.parametrize("shape", ["S2", "S3"])
async def test_native_p7_after_next_reaper(lane, shape):
    await _run(lane, "RC-prod", shape, "P7")
