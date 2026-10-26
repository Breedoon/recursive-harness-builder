"""Native isolated midturn close injections, plus the diagnosed MCP-session latch caller."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.harness.events import EventLog, FileTail, JsonlTail
from tests.e2e.harness.suites.test_evc_latch import _native_tool_session, run_latch
from tests.e2e.harness.suites.test_evc_lifecycle import PYTHON, _control_child_stop, _helper, _proof_path, _shape
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-midturn", "isolation")]


async def _at_epoch(epoch):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    handle = loop.call_at(loop.time() + max(0, epoch - time.time()), future.set_result, None)
    try:
        await future
    finally:
        handle.cancel()


def _retry_user(event):
    if event.get("type") != "user":
        return False
    content = (event["row"].get("message") or {}).get("content")
    return isinstance(content, str) and content.startswith("(System:")


async def run_midturn(lane, kind):
    names = {"reconnect": "MIDTURN-STREAM-ERROR-RECONNECT", "soft-reset": "MIDTURN-SOFT-RESET", "route-drop": "MIDTURN-ROUTE-DROP"}
    nonce = uuid.uuid4().hex[:8]
    root = "/workspace/runtime/state/evc-midturn-" + nonce
    local = lane.lane_dir / "state" / ("evc-midturn-" + nonce)
    local.mkdir(parents=True, exist_ok=True)
    signals = EventLog(local / "signals.jsonl", timing=lane.timing)
    signals.start()
    proof = {"cell_id": "NAMED/" + names[kind], "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_utc_epoch": time.time(), "require_child": True, "injection_kind": kind,
             "trigger": {"reconnect": "24cf5ca:runner.py448-495/session.py811-828 real CLI ProcessError -> SDK disconnect/reconnect",
                         "soft-reset": "24cf5ca:runner.py494-495 second stream failure propagates -> telegram.py10271-10298 -> session.py890-897",
                         "route-drop": "24cf5ca:telegram.py1221-1226 TopicDeletedError ->11037-11082 drop route/SDK disconnect"}[kind],
             "configuration_validation": "normal explicit-Sol test-child route; only this isolated lane's captured CLI/topic is faulted; no OBS src, proxy, controller, guard or SDK edit",
             "raw_PASS_scope": "native named close path and event+10 process observation constructed, NOT cleanup/parity/RC/obs-cli success"}
    log = lane.lane_dir / "logs/telegram-prod.log"
    tail, jsonl = None, None
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, "S2")
        topic, sent, start, observed = await _shape(lane, "S2", nonce, root, signals, proof)
        identity = await _control_child_stop(lane, proof, observe_only=True)
        proof["fixture_identity_before_event"] = identity
        assert identity["capture_known"] and identity["route_active"] and identity["root_pid"], identity
        proof["target_cli_pid"] = pid = identity["root_pid"]
        proof["pre_event"] = before = await _helper(lane, "snapshot", root)
        cli = next(row for row in before["processes"] if row.get("cli") and row["pid"] == pid)
        binding = _native_tool_session(lane.lane_dir / "home/.claude/projects/-workspace-runtime-git-obs-vault-active", root)
        proof["native_subject_session_binding"] = binding
        session = binding["session_id"]
        jsonl = JsonlTail(Path(binding["source"]), session, timing=lane.timing)
        jsonl.start()
        row_mark = jsonl.log.mark()
        matches = {name: asyncio.get_running_loop().create_future() for name in
                   ("Stream error, attempting reconnect:", "Soft reset after error (session_id preserved)", "dropping route state and sending bounce-backs")}
        def on_line(line, seen):
            for name, future in matches.items():
                if name in line and not future.done():
                    future.set_result({"line": line, "observed_epoch": seen})
        tail = FileTail(log, on_line)
        tail.offset = log.stat().st_size
        tail.start()
        proof["log_watch_mode"] = tail.mode
        proof["faults"] = []
        if kind == "route-drop":
            deleted = await lane.exec(PYTHON, "-m", "tests.e2e.harness.evc_fixture", "delete-topic", "--root", root,
                "--expected-bot", str(lane.info["bot_id"]), "--chat", str(start["chat_id"]), "--topic", str(start["topic_id"]),
                cwd=lane.info["harness_root"], env={"PYTHONPATH": lane.info["harness_root"] + ":" + lane.info["code_dir"] + "/src"})
            proof["delete_topic_process"] = {"rc": deleted["rc"], "stderr": deleted["stderr"][-500:]}
            assert deleted["rc"] == 0, proof["delete_topic_process"]
            proof["owned_test_topic_deleted"] = json.loads(deleted["stdout"])
            proof["faults"].append({"type": "OWN_TEST_TOPIC_DELETION", "topic_id": topic,
                                    "epoch": proof["owned_test_topic_deleted"]["completed_epoch"]})
            proof["post_deletion_foreground_release"] = await _helper(lane, "release", root)
            event = await asyncio.wait_for(matches["dropping route state and sending bounce-backs"], 90)
            proof["native_close_event"] = event
            connection = lane.state_db()
            try:
                count = connection.execute("SELECT COUNT(*) FROM route_state WHERE chat_id=? AND thread_id=?",
                                            (start["chat_id"], start["topic_id"])).fetchone()[0]
            finally:
                connection.close()
            proof["route_rows_after_drop"] = count
            assert count == 0, count
            proof["route_drop_phase_limit"] = "native route-drop log after owned topic deletion and helper release; SDK Result timing untapped, so not a claim that a model/tool was still active at SDK disconnect"
        else:
            fault_at = time.time()
            proof["faults"].append({"type": "CAPTURED_TEST_CLI_SIGKILL", "epoch_before": fault_at,
                "pid": pid, "start": cli["start"], "native_signal": await lane._ctl.call("POST", "/signal", {"pid": pid, "sig": "KILL"}),
                "epoch_after": time.time()})
            stream_error = await asyncio.wait_for(matches["Stream error, attempting reconnect:"], 45)
            proof["native_stream_error"] = stream_error
            resume = await lane.wait("session_build", agent=proof["fixture_child"]["agent_name"],
                                     match=lambda row: row.get("source") == "resume" and row.get("session_id") == session,
                                     after=fault_at, timeout=45)
            proof["native_reconnect_build"] = resume
            retry = await jsonl.log.wait("jsonl_append", match=_retry_user, after=row_mark, timeout=45)
            proof["native_retry_user_row"] = {key: retry.get(key) for key in ("session_id", "uuid", "ts", "type")}
            new_identity = await _control_child_stop(lane, proof, observe_only=True)
            proof["identity_after_reconnect"] = new_identity
            assert new_identity["capture_known"] and new_identity["root_pid"] != pid and new_identity["route_active"], new_identity
            if kind == "soft-reset":
                new_pid = new_identity["root_pid"]
                current = await _helper(lane, "snapshot", root, identities={new_pid: new_identity["owned"][str(new_pid)]})
                check = current["identity_checks"][0]
                assert check["outcome"] == "SAME_IDENTITY" and check["state"] not in {"Z", "X"}, check
                proof["faults"].append({"type": "ONE_RETRY_TEST_CLI_SIGKILL", "epoch_before": time.time(),
                    "pid": new_pid, "start": check["observed_start"],
                    "native_signal": await lane._ctl.call("POST", "/signal", {"pid": new_pid, "sig": "KILL"}), "epoch_after": time.time()})
                event = await asyncio.wait_for(matches["Soft reset after error (session_id preserved)"], 45)
            else:
                event = {"line": "native resume=session_id build plus real retry user row and distinct captured CLI", "observed_epoch": retry["ts"]}
            proof["native_close_event"] = event
        proof["shape_signal"] = {"kind": "native_named_close_path", "injection": kind, **proof["native_close_event"]}
        epoch = proof["native_close_event"]["observed_epoch"] + 10
        proof["required_observation_epoch"] = epoch
        await _at_epoch(epoch)
        proof["post_cleanup_proc_states"] = after = await _helper(lane, "snapshot", root,
            identities={int(key): value for key, value in identity["owned"].items()})
        proof["owned_original_survivors_at_observation"] = [row for row in after["identity_checks"]
            if row["outcome"] == "SAME_IDENTITY" and row["state"] not in {"Z", "X"}]
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2000:])
        raise
    finally:
        try:
            proof["owned_fixture_release"] = await _helper(lane, "release", root)
            if proof.get("fixture_child"):
                try:
                    proof["owned_final_task_stop"] = await _control_child_stop(lane, proof)
                except Exception as exc:
                    proof["owned_final_task_stop_unavailable"] = type(exc).__name__
        finally:
            if tail:
                tail.close()
            if jsonl:
                jsonl.close()
            signals.close()
            proof["finished_epoch"] = time.time()
            _proof_path(lane, proof["cell_id"]).write_text(json.dumps(proof, indent=2) + "\n")


@pytest.mark.parametrize("kind", ["reconnect", "soft-reset", "route-drop"])
async def test_native_midturn_close(lane, kind):
    await run_midturn(lane, kind)


async def test_native_MCP_latch_session_binding(lane):
    await run_latch(lane, "RC-alt", "S5m")
