"""Native namespace-only Z-state control; no model, AgentTask or Telegram topic."""
import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.harness.events import EventLog
from tests.e2e.harness.suites.test_evc_lifecycle import PYTHON, HELPER_REL, _helper, _live_prior_tools, _proof_path
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-zombie", "isolation")]


async def test_native_zombie_is_dead_but_live_parent_is_not(lane):
    nonce = uuid.uuid4().hex[:8]
    root = "/workspace/runtime/state/evc-zombie-" + nonce
    local = lane.lane_dir / "state" / ("evc-zombie-" + nonce)
    local.mkdir(parents=True, exist_ok=True)
    signal = EventLog(local / "signals.jsonl", timing=lane.timing)
    signal.start()
    proof = {"cell_id": "NAMED/ZOMBIE", "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_utc_epoch": time.time(), "native_subject_type": "LINUX_/PROC_RULE_CONTROL; NO_MODEL_QUERY",
             "configuration_validation": "native OS control only; not a 2.1.59 or RC lifecycle result"}
    job = None
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, "S3")
        job = asyncio.create_task(lane.exec(PYTHON, str(Path(lane.info["code_dir"]) / HELPER_REL),
                                           "zombie", "--root", root, timeout=120))
        observed = await signal.wait("zombie_control", timeout=20, after=0)
        proof["shape_signal"] = observed
        assert observed["child_state"] == "Z", observed
        child_ids = {observed["child_pid"]: observed["child_start"]}
        before = await _helper(lane, "snapshot", root, identities=child_ids)
        parent = next(value for value in before["processes"] if value["pid"] == observed["pid"])
        identities = {**child_ids, parent["pid"]: parent["start"]}
        after = await _helper(lane, "snapshot", root, identities=identities)
        proof.update(pre_event=before, post_cleanup_proc_states=after)
        dead = _live_prior_tools(before, after, {"pid": observed["child_pid"]})
        live = _live_prior_tools(before, after, {"pid": observed["pid"]})
        proof.update(zombie_counted_live=dead, live_parent_positive_control=live)
        assert not dead and len(live) == 1, {"zombie": dead, "parent": live}
        child_check = next(value for value in after["identity_checks"] if value["pid"] == observed["child_pid"])
        assert child_check["outcome"] == "SAME_IDENTITY" and child_check["state"] == "Z", child_check
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2000:])
        raise
    finally:
        try:
            proof["fixture_release"] = await _helper(lane, "release", root)
            if job is not None:
                proof["fixture_process_result"] = await asyncio.wait_for(job, 15)
                assert proof["fixture_process_result"]["rc"] == 0, proof["fixture_process_result"]
        except Exception as exc:
            proof.update(raw_status="RAN-FAIL", cleanup_error=type(exc).__name__ + ": " + str(exc)[-1000:])
            raise
        finally:
            proof["finished_epoch"] = time.time()
            _proof_path(lane, "NAMED/ZOMBIE").write_text(json.dumps(proof, indent=2) + "\n")
            signal.close()
