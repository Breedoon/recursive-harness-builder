"""V-RC2's identical double-fork witness; U2 is tracked, not a release blocker."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.e2e.harness.suites.test_obs22_agent_shapes import _launch
from tests.e2e.harness.suites.test_tap_fail_open import _messages, _until_changed

pytestmark = [pytest.mark.suite("vrc2-descendant"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]


class DetachedDescendantSurvived(AssertionError):
    pass


# Unlike a blanket xfail, this cannot hide a setup/identity/tool-launch failure.
@pytest.mark.xfail(strict=True, raises=DetachedDescendantSurvived, reason="known bug obs-22.138: detached descendant omitted from captured ownership")
async def test_stop_detached_grandchild_obs_22_138(lane):
    nonce = uuid.uuid4().hex[:8]
    tag = "vrc2detached" + nonce
    state = lane.lane_dir / "state"
    script = state / "vrc2-descendant.py"
    receipt = state / "vrc2-descendant.json"
    script.write_text(
        "import json,os,signal,time\n"
        "main=os.getpid()\n"
        "first=os.fork()\n"
        "if first == 0:\n"
        "    os.setsid()\n"
        "    intermediate=os.getpid()\n"
        "    second=os.fork()\n"
        "    if second != 0: os._exit(0)\n"
        "    born={'pid':os.getpid(),'ppid_at_birth':os.getppid(),'intermediate_pid':intermediate,'tool_python_pid':main}\n"
        "    with open('/workspace/runtime/state/vrc2-descendant.json','w') as f: json.dump(born,f)\n"
        "    for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP): signal.signal(s,signal.SIG_IGN)\n"
        "    null=os.open('/dev/null',os.O_RDWR)\n"
        "    for fd in (0,1,2): os.dup2(null,fd)\n"
        f"    os.execv('/bin/bash',['bash','-c','exec -a {tag} sleep 600'])\n"
        "os.waitpid(first,0)\n"
        "time.sleep(120)\n"
    )
    root = await lane.new_topic("vrc2 detached " + nonce)
    primed = await lane.turn(root, "Reply with exactly READY-" + nonce)
    child = await _launch(lane, root, primed, {
        "display_name": "vrc2 detached worker " + nonce,
        "fork": False,
        "effort": "low",
        "hooks": json.dumps({"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}),
        "prompt": "Reply with exactly CHILD-READY-" + nonce,
    }, nonce)
    ready = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not ready.get("failed"), ready
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, "CHILD-READY-" + nonce))
    topic = int(child["topic_id"])
    sent = await lane.send(topic,
        "Call Bash exactly once, in the foreground, with exactly this command: "
        "`python3 /workspace/runtime/state/vrc2-descendant.py`. "
        "After it returns reply DONE. Do not inspect, edit, or stop the script; use no other tool.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    async with asyncio.timeout(150):
        while True:
            rows = await lane.procs()
            tagged = [r for r in rows if (r.get("args") or [None])[0] == tag]
            if tagged and receipt.exists():
                break
            await asyncio.sleep(0.2)
    born = json.loads(receipt.read_text())
    before = rows
    birth = next(r["start"] for r in tagged if r["pid"] == born["pid"])
    stop = await lane.command(topic, "/stop")
    cleanup = await lane.wait("stop_cleanup", agent=child["agent_name"], after=start, timeout=180)
    after = await lane.procs()
    survivors = [r for r in after if r.get("pid") == born["pid"] and r.get("start") == birth
                 and r.get("state") != "Z" and (r.get("args") or [None])[0] == tag]
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start, timeout=120)
    proof = {
        "run_id": lane.manifest["run_id"], "lane": lane.name,
        "candidate_code": lane.info.get("code_dir"), "cli_path": lane.manifest.get("cli_path"),
        "born": born, "start_ticks": birth, "tag": tag, "before": before, "after": after,
        "surviving_detached_descendants": survivors, "stop_cleanup": cleanup,
        "turn_end": end, "stop_message_id": stop.message_id,
    }
    output = lane.run_dir / "tests" / lane.name / "vrc2-detached-descendant.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert cleanup.get("survivors") == 0, proof
        if survivors:
            raise DetachedDescendantSurvived("obs-22.138: native PID/start/tag remains live outside captured ownership")
    finally:
        proof["cleanup"] = []
        for row in survivors:
            result = await lane.exec("/bin/kill", "-KILL", str(int(row["pid"])), timeout=10)
            proof["cleanup"].append({"pid": row["pid"], "result": result})
        proof["after_explicit_cleanup"] = [r for r in await lane.procs() if r["pid"] == born["pid"] and r["start"] == birth]
        output.write_text(json.dumps(proof, indent=2, default=str) + "\n")
