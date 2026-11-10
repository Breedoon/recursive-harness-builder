"""P6 official SDK string-query component; policy/entry/configuration boundaries explicit."""
import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.harness.suites.test_evc_lifecycle import _helper, _proof_path
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-string-query", "isolation")]


async def test_real_string_query_background_end(lane):
    nonce = uuid.uuid4().hex[:8]
    root = "/workspace/runtime/state/evc-string-" + nonce
    local = lane.lane_dir / "state" / ("evc-string-" + nonce)
    local.mkdir(parents=True, exist_ok=True)
    proof = {"cell_id": "RC-alt/S1/P6/2.1.59/R13", "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_utc_epoch": time.time(), "native_subject_type": "REAL_SDK_STRING_QUERY_COMPONENT_NOT_NORMAL_OBS_ROUTE",
             "trigger": "24cf5ca:src/obs_agent/fork.py56 definition is statically unreached; direct existing SDK0.1.44 internal/client.py133-134 str/end_input component entry",
             "configuration_validation": "DECLARED P6(b) SDK callbacks OFF; Sol/normal option factory, component CLI parent not daemon; NOT_ATTESTED_RC; author void-rule/entry consequences remain external"}
    try:
        await _helper(lane, "setup", root, "S1")
        result = await lane.exec("/workspace/obs/.venv/bin/python", "-m", "tests.e2e.harness.evc_string_query_driver",
                                 "--root", root, timeout=120, cwd=lane.info["harness_root"],
                                 env={"PYTHONPATH": lane.info["harness_root"] + ":" + lane.info["code_dir"] + "/src"})
        proof["native_component_process"] = {"rc": result["rc"], "stderr": result["stderr"][-3000:]}
        assert result["rc"] == 0, proof["native_component_process"]
        component = json.loads(result["stdout"])
        proof["string_query_component"] = component
        proof["configuration_before_turn"] = component["configuration_before_turn"]
        proof["target_cli_pid"] = component["target_cli_pid"]
        signals = [json.loads(line) for line in (local / "signals.jsonl").read_text().splitlines()]
        starts = [row for row in signals if row["kind"] == "tool_running"]
        assert len(starts) == 1, starts
        proof["shape_signal"] = {"kind": "actual_string_query_end", "ts": component["component_query_end_epoch"],
                                 "background_tool_started": starts[0], "input_type": component["input_type"],
                                 "SDK_hook_policy": component["declared_policy"], "normal_OBS_entry": component["normal_OBS_entry"]}
        assert component["input_type"] == "str" and component["sdk_hook_names"] == [], component
        assert starts[0]["ts"] <= component["component_query_end_epoch"], proof["shape_signal"]
        epoch = component["component_query_end_epoch"] + 10
        proof["required_observation_epoch"] = epoch
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        handle = loop.call_at(loop.time() + max(0, epoch - time.time()), future.set_result, None)
        try:
            await future
        finally:
            handle.cancel()
        identities = {starts[0]["pid"]: starts[0]["start"]}
        cli = next(p for p in component["configuration_before_turn"]["processes"] if p["pid"] == component["target_cli_pid"])
        identities[cli["pid"]] = cli["start"]
        proof["post_cleanup_proc_states"] = await _helper(lane, "snapshot", root, identities=identities)
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2500:])
        raise
    finally:
        try:
            proof["owned_background_release"] = await _helper(lane, "release", root)
        except Exception as exc:
            proof.update(raw_status="RAN-FAIL", cleanup_error=type(exc).__name__ + ": " + str(exc)[-1000:])
            raise
        finally:
            proof["finished_epoch"] = time.time()
            _proof_path(lane, proof["cell_id"]).write_text(json.dumps(proof, indent=2) + "\n")
