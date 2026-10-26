"""Real AgentTaskStop / active-latch SIGTERM constructions, with explicit shape limits."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.harness.events import EventLog
from tests.e2e.harness.suites.test_evc_lifecycle import (
    PYTHON, _after_cleanup_observation_epoch, _control_child_stop, _helper,
    _proof_path, _recorded_background_call, _shape,
)
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-latch-samples", "isolation")]


def _user_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("type") == "user" and row.get("sessionId") == path.stem:
            content = (row.get("message") or {}).get("content")
            rows.append({"uuid": row.get("uuid"), "timestamp": row.get("timestamp"),
                         "tool_result_only": isinstance(content, list) and bool(content)
                         and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)})
    return rows


def _ancestry(processes: list[dict], child: int, cli: int) -> list[dict]:
    table = {row["pid"]: row for row in processes}
    result, visited = [], set()
    while child != cli:
        assert child in table and child not in visited, {"missing_or_cyclic_ancestor": child}
        visited.add(child)
        row = table[child]
        result.append({key: row[key] for key in ("pid", "ppid", "start", "state")})
        child = row["ppid"]
    assert cli in table
    result.append({key: table[cli][key] for key in ("pid", "ppid", "start", "state")})
    return result


def _live(checks: list[dict]) -> list[dict]:
    assert checks and all(row["outcome"] != "UNAVAILABLE" for row in checks), checks
    return [row for row in checks if row["outcome"] == "SAME_IDENTITY" and row["state"] not in {"Z", "X"}]


async def run_latch(lane, config: str, shape: str):
    nonce = uuid.uuid4().hex[:8]
    root = "/workspace/runtime/state/evc-latch-" + nonce
    local = lane.lane_dir / "state" / ("evc-latch-" + nonce)
    local.mkdir(parents=True, exist_ok=True)
    vault = lane.lane_dir / "vault"
    saved = {name: (vault / name).read_bytes() if (vault / name).exists() else None
             for name in (".claude/settings.json", ".mcp.json")}
    signals = EventLog(local / "signals.jsonl", timing=lane.timing)
    signals.start()
    proof = {"cell_id": f"{config}/{shape}/P1d/2.1.59/R13", "run_id": lane.manifest["run_id"], "lane": lane.name,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_utc_epoch": time.time(), "require_child": True,
             "trigger": "24cf5ca:telegram.py5896-5923 _latch_task_stop /13415-13443 real native AgentTaskStop; lane-local inotify watcher SIGTERM only verified captured CLI",
             "configuration_validation": "normal OBS child/recorded explicit Sol; added measurement heartbeat, no daemon/SDK changes; reference/void-rule judgment external",
             "added_hook": shape in {"S4", "S5h"}, "mcp_lane_copy_mechanism": shape == "S5m",
             "shape_binding_limit": "CURRENT cells.yaml labels retained: S2 foreground heartbeat is the original delta466 S4 ledger variant, NOT silently renamed; current S4 remains tool-not-started with blocking hook heartbeat; S3 has no sentinel heartbeat",
             "full_vm_obs_5_foreground_variant": shape == "S2"}
    watcher = None
    try:
        proof["configuration_before_turn"] = await _helper(lane, "setup", root, shape)
        if shape != "S3":
            (local / "heartbeat-enabled").touch()
        topic, sent, start, observed = await _shape(lane, shape, nonce, root, signals, proof)
        if shape != "S3":
            first = await signals.wait("heartbeat", match={"pid": observed["pid"]}, after=0, timeout=5)
            second = await signals.wait("heartbeat", match={"pid": observed["pid"]}, after=first, timeout=5)
            assert second["ts"] > first["ts"]
            proof["advancing_heartbeat_before_stop"] = [first, second]
        identity = await _control_child_stop(lane, proof, observe_only=True)
        proof["fixture_identity_before_event"] = identity
        assert identity["capture_known"] and identity["root_pid"] and identity["route_active"], identity
        proof["target_cli_pid"] = pid = identity["root_pid"]
        before = await _helper(lane, "snapshot", root)
        proof["pre_event"] = before
        cli = next(row for row in before["processes"] if row["pid"] == pid and row.get("cli"))
        if shape != "S3":
            assert str(observed["pid"]) in identity["owned"], identity
            proof["sentinel_ancestry_before_stop"] = _ancestry(before["processes"], observed["pid"], pid)
        if shape == "S4":
            assert before["tool_started"] is False
            proof["pre_event_tool_not_started"] = True
        if shape == "S1":
            proof["background_bash_call"] = _recorded_background_call(lane, root)
            assert cli["flags"].get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "0"
        connection = lane.state_db()
        try:
            row = connection.execute("SELECT session_id FROM route_state WHERE chat_id=? AND thread_id=?",
                                     (start["chat_id"], start["topic_id"])).fetchone()
        finally:
            connection.close()
        assert row and row[0], "active subject session id must be native, never guessed"
        proof["subject_session_id"] = session = row[0]
        session_file = lane.lane_dir / "home/.claude/projects/-workspace-runtime-git-obs-vault-active" / (session + ".jsonl")
        proof["user_rows_before_stop"] = user_before = _user_rows(session_file)
        log = lane.lane_dir / "logs/telegram-prod.log"
        offset = log.stat().st_size
        watcher = asyncio.create_task(lane.exec(PYTHON, "-m", "tests.e2e.harness.evc_latch_watcher", "--root", root,
            "--pid", str(pid), "--start", str(cli["start"]), "--chat", str(start["chat_id"]),
            "--topic", str(start["topic_id"]), "--offset", str(offset), timeout=50,
            cwd=lane.info["harness_root"], env={"PYTHONPATH": lane.info["harness_root"]}))
        proof["latch_watcher_ready"] = await signals.wait("latch_watcher_ready", after=0, timeout=10)
        receipt = await _control_child_stop(lane, proof)
        proof["native_task_stop_receipt"] = receipt
        assert receipt["request_timestamp_basis"] == "native_latch" and receipt["route_active"], receipt
        watched = await watcher
        proof["latch_watcher_process"] = {"rc": watched["rc"], "stderr": watched["stderr"][-1500:]}
        assert watched["rc"] == 0, proof["latch_watcher_process"]
        proof["active_latch_sigterm_receipt"] = signal_receipt = json.loads(watched["stdout"])
        assert signal_receipt["signal_syscall_returned"] and signal_receipt["signal_before_epoch"] >= receipt["stop_requested_at"], signal_receipt
        remaining = receipt["stop_requested_at"] + 45 - time.time()
        assert remaining > 0
        end, cleanup = await asyncio.gather(lane.wait("turn_end", agent=topic, after=start, timeout=remaining),
                                            lane.wait("stop_cleanup", agent=topic, after=start, timeout=remaining))
        proof.update(turn_end=end, stop_cleanup=cleanup)
        await _after_cleanup_observation_epoch(proof, cleanup)
        owned = {int(key): int(value) for key, value in receipt["owned"].items()}
        assert owned.get(pid) == cli["start"]
        proof["post_cleanup_proc_states"] = point = await _helper(lane, "snapshot", root, identities=owned)
        point_live = _live(point["identity_checks"])
        proof["live_owned_at_P1_point"] = point_live
        epoch = max(receipt["stop_requested_at"] + 33, proof["required_post_cleanup_observation_epoch"])
        proof["beyond_grace_observation_epoch"] = epoch
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        handle = loop.call_at(loop.time() + max(0, epoch - time.time()), future.set_result, None)
        try:
            await future
        finally:
            handle.cancel()
        proof["beyond_grace_proc_states"] = later = await _helper(lane, "snapshot", root, identities=owned)
        later_live = _live(later["identity_checks"])
        proof["live_owned_beyond_grace"] = later_live
        proof["native_task_stop_final_receipt"] = final = await _control_child_stop(lane, proof, observe_only=True)
        user_after = _user_rows(session_file)
        previous = {row["uuid"] for row in user_before}
        proof["new_user_rows_after_stop"] = [row for row in user_after if row["uuid"] not in previous]
        signal_at = signal_receipt["signal_after_epoch"]
        proof["new_turns_after_signal"] = lane.events.find("turn_start", agent=topic, after=signal_at)
        proof["session_builds_after_signal"] = lane.events.find("session_build", agent=proof["fixture_child"]["agent_name"], after=signal_at)
        proof["new_session_CLIs_beyond_grace"] = [row for row in later["processes"] if row.get("cli") and row["pid"] != pid
            and any((row.get("cli_launch") or {}).get(key) == session for key in ("--resume", "--session-id"))]
        proof["heartbeats_after_signal"] = signals.find("heartbeat", after=signal_receipt["signal_before_epoch"])
        proof["signal_before_native_turn_end"] = signal_receipt["signal_after_epoch"] < end["ts"]
        proof["SDK_result_timestamp"] = "NOT_DIRECTLY_TAPPED: native active latch/live blocked foreground lineage and later turn_end recorded; no fictitious SDK Result timestamp"
        proof["latch_log_excerpt"] = [line for line in log.read_text(errors="replace").splitlines()
            if "[task_stop]" in line and f"thread_id={topic})" in line]
        failures = []
        for key, failed in (("owned survivor at P1 observation", bool(point_live)), ("owned survivor beyond grace", bool(later_live)),
            ("task not stopped/inactive", final["native_status"] != "stopped" or final["route_active"]),
            ("new user row", bool(proof["new_user_rows_after_stop"])), ("new route turn", bool(proof["new_turns_after_signal"])),
            ("new session build", bool(proof["session_builds_after_signal"])), ("new CLI for session", bool(proof["new_session_CLIs_beyond_grace"])),
            ("heartbeat after signal", bool(proof["heartbeats_after_signal"])), ("signal not before native turn end", not proof["signal_before_native_turn_end"])):
            if failed:
                failures.append(key)
        proof["ledger_failures"] = failures
        assert not failures, failures
        proof["raw_status"] = "RAN-PASS"
    except Exception as exc:
        proof.update(raw_status="RAN-FAIL", error=type(exc).__name__ + ": " + str(exc)[-2000:])
        raise
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        try:
            proof["owned_fixture_release"] = await _helper(lane, "release", root)
        finally:
            signals.close()
            for name, content in saved.items():
                path = vault / name
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            proof["finished_epoch"] = time.time()
            _proof_path(lane, proof["cell_id"]).write_text(json.dumps(proof, indent=2) + "\n")


@pytest.mark.parametrize("config,shape", [("RC-prod", "S2"), ("RC-prod", "S3"), ("RC-alt", "S4")])
async def test_active_latch_sample(lane, config, shape):
    await run_latch(lane, config, shape)
