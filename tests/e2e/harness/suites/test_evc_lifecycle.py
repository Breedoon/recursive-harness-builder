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


async def _helper(lane, mode, root, shape=None, identities=None):
    args = [PYTHON, str(Path(lane.info["code_dir"]) / HELPER_REL), mode, "--root", root]
    if shape:
        args += ["--shape", shape]
    if identities is not None:
        args += ["--identities", json.dumps(identities)]
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
    is_alt = proof["cell_id"].startswith("RC-alt/")
    if shape in {"S2", "S5m"} or proof.get("require_child") or is_alt:
        from types import SimpleNamespace
        hooks = {"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}
        mark = lane.events.mark()
        child_prompt = "Role: executor. Isolated E2E lifecycle fixture only; no exploration, registration or helpers. " + prompt
        if is_alt:
            payload = {"display_name": f"EV-C fixture {nonce}", "fork": False, "model": "sol", "effort": "low",
                       "hooks": json.dumps(hooks), "env": json.dumps({"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}),
                       "inherit_schedules": "false", "prompt": child_prompt}
            launch = await lane.send(topic, "Call the `mcp__obs-agent__AgentTask` tool exactly once with these exact arguments, "
                                     "then reply LAUNCHED; do not use any other tool.\n" + json.dumps(payload))
            proof["native_MCP_launch_request_message_id"] = launch.message_id
            start = await lane.wait("turn_start", after=mark,
                match=lambda event: event.get("team") == ready.end["team"] and bool(event.get("agent_name"))
                      and event["agent_name"] != ready.end["agent_name"], timeout=90)
            connection = lane.state_db()
            try:
                records = connection.execute("SELECT team_name,agent_name FROM task_handle_state WHERE child_chat_id=? AND child_thread_id=?",
                                             (start["chat_id"], start["topic_id"])).fetchall()
                assert len(records) == 1, records
                child = {"team_name": records[0][0], "agent_name": records[0][1]}
                env_row = connection.execute("SELECT explicit_env_json FROM route_state WHERE chat_id=? AND thread_id=?",
                                             (start["chat_id"], start["topic_id"])).fetchone()
                explicit_env = json.loads(env_row[0]) if env_row and env_row[0] else {}
            finally:
                connection.close()
            proof["fixture_child_launch_mechanism"] = "NORMAL native MCP AgentTask env; C3 launch does not forward env"
            proof["explicit_env_lifecycle_override"] = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": explicit_env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")}
            proof["explicit_env_NAMEs"] = sorted(explicit_env)
            assert proof["explicit_env_lifecycle_override"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "0", proof["explicit_env_lifecycle_override"]
        else:
            result = await lane.control("--json", "--as", "evc-fixture", "launch",
                "--parent-team", ready.end["team"], "--parent", ready.end["agent_name"],
                "--name", f"EV-C fixture {nonce}", "--fresh", "--model", "sol", "--effort", "low",
                "--hooks", json.dumps(hooks), "--prompt", child_prompt)
            proof["fixture_child_control"] = result
            assert result["rc"] == 0, result
            response = json.loads(result["stdout"])
            assert response["ok"], response
            child = {key.strip(): value.strip() for line in response["text"].splitlines()
                     for key, separator, value in [line.partition(":")] if separator}
            assert child.get("team_name") and child.get("agent_name"), child
            proof["fixture_child_launch_mechanism"] = "existing C3 native control launch; default production-shaped lifecycle env"
            start = await lane.wait("turn_start", agent=child["agent_name"], after=mark)
        proof["fixture_child"] = child
        proof["fixture_parent"] = {key: ready.end[key] for key in ("team", "agent_name")}
        proof["fixture_child_hooks"] = hooks
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
    if observed.get("pid") is None:
        return []
    prior = {p["pid"]: p for p in before["processes"] if p["pid"] == observed["pid"]}
    assert len(prior) == 1, {"missing_pre_event_identity": observed}
    checks = [p for p in after.get("identity_checks", []) if p["pid"] in prior]
    assert len(checks) == 1 and checks[0]["outcome"] != "UNAVAILABLE", checks
    return [p for p in checks if p["outcome"] == "SAME_IDENTITY" and p["state"] not in {"Z", "X"}]


async def _control_child_stop(lane, proof, *, observe_only=False):
    child, parent = proof["fixture_child"], proof["fixture_parent"]
    connection = lane.state_db()
    try:
        cursor = connection.execute(
            "SELECT task_id,launch_parent_message_id,team_name,agent_name,status,idle_ready,"
            "parent_chat_id,parent_thread_id,child_chat_id,child_thread_id "
            "FROM task_handle_state WHERE team_name=? AND agent_name=?",
            (child["team_name"], child["agent_name"]))
        rows = cursor.fetchall()
        assert len(rows) == 1, rows
        generation = dict(zip([column[0] for column in cursor.description], rows[0]))
    finally:
        connection.close()
    proof.setdefault("task_generation_before_stop", generation)
    assert generation["launch_parent_message_id"] is not None, generation
    args = ["--json", "--as", "evc-fixture", "stop", "--parent-team", parent["team"],
            "--parent", parent["agent_name"], "--team", child["team_name"],
            "--agent", child["agent_name"], "--expected-task-id", generation["task_id"],
            "--expected-launch-parent-message-id", str(generation["launch_parent_message_id"])]
    if observe_only:
        args.append("--observe-only")
    native = await lane.control(*args)
    proof["native_task_stop_observe" if observe_only else "native_task_stop"] = native
    assert native["rc"] == 0, native
    response = json.loads(native["stdout"])
    assert response["ok"] and response.get("result"), response
    return response["result"]


async def _after_cleanup_observation_epoch(proof, cleanup):
    # cells.yaml observation_defaults explicitly requires stop_cleanup +2s.
    epoch = cleanup["ts"] + 2.0
    proof["required_post_cleanup_observation_epoch"] = epoch
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    handle = loop.call_at(loop.time() + max(0.0, epoch - time.time()), future.set_result, None)
    try:
        await future
    finally:
        handle.cancel()
    proof["post_cleanup_observation_started_epoch"] = time.time()


async def _run(lane, config, shape, path):
    if path not in {"P1a", "P1c", "P2", "P7"}:
        raise NotImplementedError(f"No native trigger implemented for {path}; never count fall-through as RAN-PASS")
    if shape not in {"S2", "S3", "S4", "S5h", "S5m"}:
        raise NotImplementedError(f"No authored native shape implemented for {shape}; S1 must really start background Bash")
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
             "trigger": {"P1c": "24cf5ca:src/obs_agent/telegram.py:6549-6660,13415-13443",
                         "P7": "24cf5ca:src/obs_agent/telegram.py:14024-14033"}.get(path,
                         "24cf5ca:src/obs_agent/telegram.py:5995-6011"),
             "added_hook": shape in {"S4", "S5h"}, "mcp_lane_copy_mechanism": shape == "S5m",
             "require_child": path in {"P1c", "P1d", "P5"}}
    target = _proof_path(lane, case)
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, shape)
        topic, sent, start, observed = await _shape(lane, shape, nonce, root, signals, proof)
        if proof.get("fixture_child"):
            identity = await _control_child_stop(lane, proof, observe_only=True)
            proof["fixture_identity_before_event"] = identity
            proof["target_cli_pid"] = identity["root_pid"]
            assert identity["capture_known"] and identity["root_pid"] and identity["route_active"], identity
        before = await _helper(lane, "snapshot", root)
        proof["pre_event"] = before
        candidates = [p for p in before["processes"] if p.get("cli")]
        if not proof.get("fixture_child") and len(candidates) == 1:
            proof["target_cli_pid"] = candidates[0]["pid"]
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
            await _after_cleanup_observation_epoch(proof, cleanup)
            after = await _helper(lane, "snapshot", root, identities={p["pid"]: p["start"] for p in before["processes"]})
            proof["post_cleanup_proc_states"] = after
            alive = _live_prior_tools(before, after, observed)
            proof["live_prior_tools_excluding_Z_X"] = alive
            assert not alive, alive
        elif path == "P1c":
            receipt = await _control_child_stop(lane, proof)
            proof["native_task_stop_receipt"] = receipt
            assert receipt.get("request_timestamp_basis") == "native_latch", receipt
            assert receipt["stop_requested_at"] >= start["ts"], receipt
            remaining = receipt["stop_requested_at"] + 45 - time.time()
            assert remaining > 0, receipt
            end, cleanup = await asyncio.gather(
                lane.wait("turn_end", agent=topic, after=start, timeout=remaining),
                lane.wait("stop_cleanup", agent=topic, after=start, timeout=remaining))
            proof.update(turn_end=end, stop_cleanup=cleanup)
            await _after_cleanup_observation_epoch(proof, cleanup)
            after = await _helper(lane, "snapshot", root, identities={p["pid"]: p["start"] for p in before["processes"]})
            proof["post_cleanup_proc_states"] = after
            alive = _live_prior_tools(before, after, observed)
            proof["live_prior_tools_excluding_Z_X"] = alive
            assert not alive, alive
            proof["native_task_stop_final_receipt"] = await _control_child_stop(lane, proof, observe_only=True)
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
            after = await _helper(lane, "snapshot", root, identities={p["pid"]: p["start"] for p in before["processes"]})
            proof["after_next_reaper"] = after
            prior = {p["pid"]: p for p in before["processes"] if p.get("cli") or p["pid"] == observed.get("pid")}
            checks = [p for p in after["identity_checks"] if p["pid"] in prior]
            assert len(checks) == len(prior) and all(p["outcome"] != "UNAVAILABLE" for p in checks), checks
            alive = [p for p in checks if p["outcome"] == "SAME_IDENTITY" and p["state"] not in {"Z", "X"}]
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
