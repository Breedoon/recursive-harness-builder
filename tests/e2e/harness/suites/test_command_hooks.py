"""WP-01 real OBS command-hook parity, runner/lane containers only.

Recorder settings replace scripts at the production matchers; no live settings change.
SDK callbacks remain OBS's real state/Fractals pipelines, not mocks.
"""
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from tests.e2e.harness.suites.test_obs22_agent_shapes import _launch
from tests.e2e.harness.suites.test_inc11_stability import _jsonl_rows, _tool_results

pytestmark = [pytest.mark.suite("command-hooks"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]
GUARD = {"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}
RECORDER = '''import json,os,pathlib,sys,time
inp=json.load(sys.stdin); tag=sys.argv[1]
with open('/workspace/runtime/state/wp01-hooks.jsonl','a') as f:
 f.write(json.dumps({'tag':tag,'input':inp,'cwd':os.getcwd(),'time':time.monotonic()})+'\\n')
cmd=inp.get('tool_input',{}).get('command','')
if tag=='bash-guard' and 'WP01_EXIT2' in cmd:
 sys.stderr.write('WP01_EXIT2_REASON\\n'); sys.exit(2)
if tag=='bash-guard' and 'WP01_JSON' in cmd:
 print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':'WP01_JSON_REASON'}}))
'''


async def _setup(lane):
    source = Path(__file__).resolve().parents[4] / "deploy/obs-e2e/fixture-vault/.claude/settings.json"
    settings = json.loads(source.read_text())
    # Keep OBS's real root/executor SDK callbacks; replace project commands only.
    code = ("import json,pathlib; p=pathlib.Path('/workspace/runtime/git/obs-vault-active/.claude/settings.json'); "
            "s=json.loads(p.read_text()); s['hooks']=" + repr(settings["hooks"]) + "; p.write_text(json.dumps(s)); "
            "pathlib.Path('/workspace/runtime/state/wp01-hook.py').write_text(" + repr(RECORDER) + "); "
            "pathlib.Path('/workspace/runtime/state/wp01-hooks.jsonl').write_text('')")
    result = await lane.exec("python3", "-c", code)
    assert result.get("returncode", result.get("rc", 0)) == 0, result


def _records(lane):
    p = lane.lane_dir / "state/wp01-hooks.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _proof(lane, name, data):
    target = lane.run_dir / "tests" / lane.name / "command-hooks"
    target.mkdir(parents=True, exist_ok=True)
    (target / (name + ".json")).write_text(json.dumps(data, indent=2, default=str))


async def _child(lane, mode):
    await _setup(lane)
    nonce = uuid.uuid4().hex[:8]
    topic = await lane.new_topic("WP01 " + mode + " " + nonce)
    parent = await lane.turn(topic, "Reply exactly READY-" + nonce)
    assert not parent.end.get("failed"), parent.end
    child = await _launch(lane, topic, parent, {"display_name":"WP01 child " + nonce, "fork":False,
                          "effort":"low", "hooks":json.dumps(GUARD), "prompt":"Reply exactly CHILD-READY-" + nonce}, nonce)
    end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not end.get("failed") and end.get("session_id"), end
    # The child is ready before input; its parent's idle-notice turn is irrelevant.
    return topic, parent, child, end


@pytest.mark.parametrize("mode,reason", [("WP01_EXIT2", "WP01_EXIT2_REASON"), ("WP01_JSON", "WP01_JSON_REASON")])
async def test_deny_blocks_and_returns_reason(lane, mode, reason):
    topic, parent, child, prior = await _child(lane, mode)
    command = "printf " + mode + "; touch /workspace/runtime/state/" + mode
    turn = await lane.turn(child["topic_id"], "Call Bash exactly once with command `" + command +
                           "`. Do not retry or use another tool if denied. Reply with the exact hook reason you received.")
    rows = _jsonl_rows(lane, prior["session_id"])
    results = _tool_results(rows)
    record = _records(lane)
    denied = [r for r in results if r["tool"] == "Bash" and r["is_error"] and reason in r["head"]]
    # Full reason may follow the long hook command prefix; inspect content too.
    raw = json.dumps(rows)
    matching = [r for r in record if r["input"].get("tool_name") == "Bash" and r["input"].get("tool_input",{}).get("command") == command]
    _proof(lane, mode, {"records":record,"results":results,"model_received_reason":reason in raw,
                        "model_reply_reason":reason in turn.text,"turn_end":turn.end})
    assert matching, "project Bash command hook never ran"
    assert reason in raw and reason in turn.text, "hook reason did not reach the model"
    assert not (lane.lane_dir / "state" / mode).exists(), "denied Bash ran"
    assert not any(r["tag"] == "checkpoint-post" for r in matching), "PostToolUse ran for a denied tool"
    assert not any(r["tag"] in ("shared-edit","launch") for r in matching), "matcher fired on wrong tool"


async def test_production_shape_matching_stdin_and_post_count(lane):
    topic, parent, child, prior = await _child(lane, "shape")
    nonce = uuid.uuid4().hex[:8]
    filename = "/workspace/runtime/state/wp01-edit-" + nonce
    request = ("Use only these tools, once each, in this exact order: "
               "Bash with command `printf WP01_ALLOWED`; "
               "Write file " + filename + " with content OLD; "
               "Read the whole file " + filename + "; "
               "Edit that file replacing OLD with NEW. Then reply exactly SHAPE-DONE. No other tools.")
    turn = await lane.turn(child["topic_id"], request)
    rows = _jsonl_rows(lane, prior["session_id"])
    record = _records(lane)
    by_call = {}
    for r in record:
        inp = r["input"]
        by_call.setdefault(inp.get("tool_use_id"), []).append(r)
    selected = []
    expected = {
        "Bash":{"bash-guard","freeze","safe-sync","shared-bash","checkpoint-pre","regex"},
        "Write":{"shared-edit","checkpoint-pre"}, "Read":{"checkpoint-pre"},
        "Edit":{"shared-edit","checkpoint-pre"}, "mcp__obs-agent__AgentTask":{"launch","checkpoint-pre"},
    }
    for name in expected:
        calls = [rr for rr in by_call.values() if rr[0]["input"].get("tool_name") == name]
        assert len(calls) == 1, (name, len(calls), record)
        rr = calls[0]
        pre = [r for r in rr if r["input"]["hook_event_name"] == "PreToolUse"]
        post = [r for r in rr if r["input"]["hook_event_name"] == "PostToolUse"]
        assert {r["tag"] for r in pre} == expected[name], (name, pre)
        assert len(post) == 1 and post[0]["tag"] == "checkpoint-post", (name,post)
        assert all(r["input"].get("tool_input") and r["input"].get("session_id") and
                   r["input"].get("transcript_path") and r["cwd"] == "/workspace/runtime/git/obs-vault-active" for r in rr)
        assert max(r["time"] for r in pre) < post[0]["time"], rr
        selected.append({"tool":name,"pre_tags":sorted(r["tag"] for r in pre),"post_count":len(post)})
    sdk_events = [e for e in lane.find("tool_call") if e.get("agent_name") == child["agent_name"]]
    _proof(lane, "production-shape", {"records":record,"selected":selected,"sdk_events":sdk_events,
                                     "results":_tool_results(rows),"turn_end":turn.end})
    assert "SHAPE-DONE" in turn.text and not turn.end.get("failed"), turn.end
    assert (lane.lane_dir / "state" / ("wp01-edit-" + nonce)).read_text() == "NEW"
    # The real executor SDK guard ran too: its immutable-source denial is separate from project hooks.
    denied = await lane.turn(child["topic_id"], "Call Write exactly once for /workspace/runtime/git/obs-vault-active/Sources/wp01-forbidden.md "
                             "with content WP01. Do not retry. Then reply DONE.")
    assert not denied.end.get("failed"), denied.end
    assert not (lane.lane_dir / "vault/Sources/wp01-forbidden.md").exists(), "real OBS SDK callback did not deny immutable source"
