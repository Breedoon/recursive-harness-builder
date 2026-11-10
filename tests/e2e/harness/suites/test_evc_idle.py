"""Native eligible AgentTask idle-prune construction, with an explicit completion barrier."""
import asyncio
import json
import re
import time
import uuid

import pytest

from tests.e2e.harness.events import EventLog, FileTail
from tests.e2e.harness.suites.test_evc_lifecycle import (
    _control_child_stop, _helper, _live_prior_tools, _proof_path, _recorded_background_call, _shape,
)
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-idle", "isolation")]


def _task_record(lane, proof):
    connection = lane.state_db()
    try:
        child = proof["fixture_child"]
        cursor = connection.execute("SELECT task_id,team_name,agent_name,status,idle_ready,completed_at,terminal_request "
                                    "FROM task_handle_state WHERE team_name=? AND agent_name=?",
                                    (child["team_name"], child["agent_name"]))
        rows = cursor.fetchall()
        assert len(rows) == 1, rows
        return dict(zip([column[0] for column in cursor.description], rows[0]))
    finally:
        connection.close()


async def test_native_eligible_background_child_killed_on_idle(lane):
    nonce = uuid.uuid4().hex[:8]
    root = "/workspace/runtime/state/evc-" + nonce
    barrier = "/workspace/runtime/state/idle-completion-" + nonce
    local = lane.lane_dir / "state" / ("evc-" + nonce)
    barrier_local = lane.lane_dir / "state" / ("idle-completion-" + nonce)
    local.mkdir(parents=True, exist_ok=True)
    barrier_local.mkdir(parents=True, exist_ok=True)
    signals = EventLog(local / "signals.jsonl", timing=lane.timing)
    completion_signals = EventLog(barrier_local / "signals.jsonl", timing=lane.timing)
    signals.start()
    completion_signals.start()
    log = lane.lane_dir / "logs/telegram-prod.log"
    offset = log.stat().st_size
    proof = {"cell_id": "RC-alt/S1/P5/2.1.59/R13", "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_utc_epoch": time.time(), "idle_completion_barrier": barrier, "require_child": True,
             "trigger": "24cf5ca:src/obs_agent/telegram.py:2951-3090,13296;session.py:744-787",
             "configuration_validation": "native recorded RC-alt mechanism; reference/void-rule judgment remains external"}
    tail = None
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, "S1")
        await _helper(lane, "setup", barrier, "S3")
        topic, sent, start, observed = await _shape(lane, "S1", nonce, root, signals, proof)
        proof["completion_barrier_running"] = await completion_signals.wait("tool_running", timeout=60, after=0)
        identity = await _control_child_stop(lane, proof, observe_only=True)
        assert identity["capture_known"] and identity["root_pid"] and identity["route_active"], identity
        proof["pre_idle_observe_receipt"] = identity
        proof["target_cli_pid"] = identity["root_pid"]
        before = await _helper(lane, "snapshot", root)
        proof["pre_event"] = before
        cli = next(value for value in before["processes"] if value["pid"] == identity["root_pid"])
        assert cli["flags"].get("OBS_CLAUDE_KILL_ON_IDLE") == "1", cli["flags"]
        assert cli["flags"].get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "0", cli["flags"]
        proof["background_bash_call"] = _recorded_background_call(lane, root)
        record = _task_record(lane, proof)
        proof["task_before_completion"] = record
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def line(text, seen):
            if ("Claude idle process pruning mode=kill_on_idle" in text and record["task_id"] in text
                    and re.search(r"\bpruned=[1-9][0-9]*\b", text)):
                if not future.done():
                    future.set_result({"line": text[-3000:], "observed_epoch": seen})
        tail = FileTail(log, line)
        tail.offset = offset
        tail.start()
        proof["completion_barrier_released"] = await _helper(lane, "release", barrier)
        proof["turn_end"] = await lane.wait("turn_end", agent=topic, after=start, timeout=60)
        prune = await asyncio.wait_for(future, 70)
        proof["idle_prune_terminal"] = prune
        after_record = _task_record(lane, proof)
        proof["task_after_idle_prune"] = after_record
        assert after_record["idle_ready"] == 1 and after_record["status"] == "completed", after_record
        assert after_record["terminal_request"] is None, after_record
        proof["six_eligibility_conditions"] = {
            "record_exists": True, "idle_ready": bool(after_record["idle_ready"]),
            "team_and_name": bool(after_record["team_name"] and after_record["agent_name"]),
            "nonrunning_record": "completed; native completion-trigger special case",
            "child_idle_busy_execution_false": "entailed by unchanged2951-2984 and3063-3073 guards plus this task's successful native prune",
            "connected_client": "pre-event exact CLI connected/owned; successful disconnect_idle_client(direct_kill=True) entailed by pruned count; not direct postkill memory introspection"}
        epoch = prune["observed_epoch"] + 10
        proof["required_observation_epoch"] = epoch
        ready = loop.create_future()
        handle = loop.call_at(loop.time() + max(0, epoch - time.time()), ready.set_result, None)
        try:
            await ready
        finally:
            handle.cancel()
        after = await _helper(lane, "snapshot", root, identities={p["pid"]: p["start"] for p in before["processes"]})
        proof["post_cleanup_proc_states"] = after
        check = next(value for value in after["identity_checks"] if value["pid"] == cli["pid"])
        proof["pruned_cli_identity_check"] = check
        assert check["outcome"] in {"ABSENT", "REUSED_PID"} or (check["outcome"] == "SAME_IDENTITY" and check["state"] in {"Z", "X"}), check
        proof["background_liveness_at_observation"] = _live_prior_tools(before, after, observed)
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2500:])
        raise
    finally:
        if tail:
            tail.close()
        try:
            proof["owned_background_release"] = await _helper(lane, "release", root)
            proof["owned_completion_release"] = await _helper(lane, "release", barrier)
        except Exception as exc:
            proof.update(raw_status="RAN-FAIL", cleanup_error=type(exc).__name__ + ": " + str(exc)[-1000:])
            raise
        finally:
            if proof.get("raw_status") == "RAN-FAIL" and proof.get("topic_id"):
                proof["failure_cleanup_stop"] = (await lane.command(proof["topic_id"], "/stop")).message_id
            proof["finished_epoch"] = time.time()
            _proof_path(lane, proof["cell_id"]).write_text(json.dumps(proof, indent=2) + "\n")
            signals.close()
            completion_signals.close()
