"""Lane self-test: a failed event sink cannot break replies, schedules or AgentTask."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest

from tests.e2e.harness.inotify import Inotify

pytestmark = [pytest.mark.suite("selftest-tap"), pytest.mark.needs(all_lanes=False, credential="dev")]


async def _until_changed(lane, check):
    notifier = Inotify()
    for directory in (lane.lane_dir / "logs", lane.lane_dir / "state"):
        notifier.add_watch(str(directory))
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_change():
        notifier.read()
        changed.set()

    loop.add_reader(notifier.fileno(), on_change)
    try:
        async with asyncio.timeout(120):
            while True:
                changed.clear()
                result = await check()
                if result:
                    return result
                await changed.wait()
    finally:
        loop.remove_reader(notifier.fileno())
        notifier.close()


def _rows(lane, query, parameters):
    connection = lane.state_db()
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


async def _messages(lane, topic, since, marker):
    messages = await lane.telegram.messages(topic, since=since, from_bot=True)
    return messages if any(marker in message.get("text", "") for message in messages) else None


async def test_tap_write_failure_keeps_reply_schedule_and_agenttask_alive(lane):
    nonce = uuid.uuid4().hex[:10].upper()
    topic = await lane.new_topic(f"tap fail-open {nonce}")
    pong = f"TAP-PONG-{nonce}"
    sent = await lane.send(topic, f"Reply with exactly: {pong}")
    replies = await _until_changed(lane, lambda: _messages(lane, topic, sent, pong))
    route = _rows(lane, "SELECT session_id FROM route_state WHERE chat_id=? AND thread_id=?",
                  (topic.chat_id, topic.topic_id))[0]
    identity_result = await lane.exec(
        "/workspace/obs/.venv/bin/python", "-c",
        "import json,sys;from obs_agent.lineage import find_latest_obs_bootstrap_for_session;"
        "b=find_latest_obs_bootstrap_for_session(session_id=sys.argv[1],cwd='/workspace/runtime/git/obs-vault-active');"
        "assert b is not None;print(json.dumps({'team':b.root_team_key,'agent':b.agent_name}))",
        route["session_id"], env={"PYTHONPATH": lane.info["code_dir"] + "/src"},
    )
    assert identity_result["rc"] == 0, identity_result
    identity = json.loads(identity_result["stdout"])
    assert identity["team"] and identity["agent"], identity

    description = f"E2E tap fail-open {nonce}"
    scheduled_marker = f"TAP-SCHEDULE-{nonce}"
    scheduled = await lane.send(
        topic,
        f"Call CronCreate once to make an interval schedule with interval_seconds='1', max_runs='1', "
        f"inherit='none', reset_session='false', description='{description}', "
        f"and prompt='Reply with exactly: {scheduled_marker}'. Leave unrelated optional fields empty. "
        f"Then reply with exactly: TAP-CRON-CREATED-{nonce}. Do not launch a child or use other tools.",
    )

    async def schedule_completed():
        records = _rows(lane, "SELECT schedule_id, run_count, max_runs, enabled, last_run_at, last_success_at, last_error "
                        "FROM topic_schedule WHERE chat_id=? AND thread_id=? AND description=?",
                        (topic.chat_id, topic.topic_id, description))
        if not records or records[0]["run_count"] != 1 or not records[0]["last_success_at"]:
            return None
        messages = await _messages(lane, topic, scheduled, scheduled_marker)
        return {"state": records[0], "messages": messages} if messages else None

    schedule_proof = await _until_changed(lane, schedule_completed)
    assert schedule_proof["state"]["max_runs"] == 1 and not schedule_proof["state"]["last_error"], schedule_proof

    child_marker = f"TAP-CHILD-{nonce}"
    launch = await lane.control(
        "--as", "obs-e2e-tap-selftest", "--json", "launch", "--parent-team", identity["team"], "--parent", identity["agent"],
        "--name", f"tap fail-open child {nonce}", "--prompt", f"Reply with exactly: {child_marker}", "--fresh",
    )
    assert launch["rc"] == 0, launch
    response = json.loads(launch["stdout"])
    assert response.get("ok") is True, response

    async def child_completed():
        records = _rows(lane, "SELECT task_id, child_chat_id, child_thread_id, child_session_id, launch_tool_name, status, completed_at "
                        "FROM task_handle_state WHERE parent_chat_id=? AND parent_thread_id=? ORDER BY created_at DESC LIMIT 1",
                        (topic.chat_id, topic.topic_id))
        if not records or records[0]["status"] != "completed" or not records[0]["completed_at"]:
            return None
        child = records[0]
        messages = await _messages(lane, child["child_thread_id"], None, child_marker)
        return {"state": child, "messages": messages} if messages else None

    child_proof = await _until_changed(lane, child_completed)
    assert child_proof["state"]["launch_tool_name"] == "AgentTask", child_proof
    assert child_proof["state"]["child_chat_id"] == topic.chat_id, child_proof
    log = (lane.lane_dir / "logs/telegram-prod.log").read_text(errors="replace")
    degraded = [line for line in log.splitlines() if "[event_tap] degraded" in line]
    assert len(degraded) == 1, degraded
    assert not lane.events.events, "The tap's unwritable sink unexpectedly supplied events"
    child_closed = await lane.close_topic(child_proof["state"]["child_thread_id"])
    root_closed = await lane.close_topic(topic)
    assert child_closed.get("closed") and root_closed.get("closed"), (child_closed, root_closed)
    proof = {"run_id": lane.manifest["run_id"], "lane": lane.name, "topic_id": topic.topic_id,
             "reply": replies, "schedule": schedule_proof, "agenttask": child_proof,
             "control_response": response, "degraded_lines": degraded, "event_count": len(lane.events.events),
             "child_closed": child_closed, "root_closed": root_closed}
    output = lane.run_dir / "tests" / lane.name / "tap-fail-open.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2) + "\n")
