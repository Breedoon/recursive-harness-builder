"""WP-01 real OBS command-hook parity, runner/lane containers only.

Recorder settings replace scripts at the production matchers; no live settings change.
SDK callbacks remain OBS's real state/Fractals pipelines, not mocks.
"""
from __future__ import annotations

import json
import gzip
from pathlib import Path
from types import SimpleNamespace
import datetime
import time
import uuid

import pytest

from tests.e2e.harness.suites.test_obs22_agent_shapes import _launch
from tests.e2e.harness.suites.test_inc11_stability import _jsonl_rows, _tool_results, _wait_tool_use

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
    # Supervisor-authorized lane-local workaround until --cli-path wiring is repaired.
    binary = lane.manifest.get("cli_path")
    if binary:
        code += "; pathlib.Path('/workspace/runtime/state/obs-cli-canary.json').write_text(" + repr(json.dumps({
            "cli_path":binary,"agents":[],"teams":[],"new_sessions_only":True,
            "percentage":100,"models":["claude-haiku-5-5"]})) + ")"
    result = await lane.exec("python3", "-c", code)
    assert result.get("returncode", result.get("rc", 0)) == 0, result


async def _tool_turn(lane, child, session_id, prompt, tool):
    sent = await lane.send(child["topic_id"], prompt)
    start = await lane.wait("turn_start", match={"trigger_message_id":sent.message_id})
    await _wait_tool_use(lane, session_id, start, tool)
    procs = await lane.procs()
    end = await lane.wait("turn_end", match={"trigger_message_id":sent.message_id}, after=start)
    messages = await lane.telegram.messages(child["topic_id"], since=sent, from_bot=True)
    binary = lane.manifest.get("cli_path")
    expected = "cli:" + Path(binary).name if binary else "cc-2.1.59"
    builds = [e for e in lane.find("session_build", agent=child["agent_name"])]
    native = [{"pid":p["pid"],"argv0":p["args"][0]} for p in procs if p.get("args") and
              (p["args"][0] == binary if binary else p["args"][0].endswith("/_bundled/claude"))]
    identity={"requested":binary,"native":native,"session_build":builds,"turn_end":end}
    _proof(lane, "identity-" + str(sent.message_id), identity)
    assert native and end.get("cli_runtime") == expected, identity
    assert builds, identity
    # Supervisor 2026-11-11 07:51:31Z, ruling 9 interim: rc2's tap is PRE-SELECTION.
    # Until the dedicated tap repair lands, argv[0]+turn_end are the two matching records.
    # Always retain session_build verbatim above; never present it as a matching selection.
    if not binary: assert builds[-1].get("runtime") == expected, identity
    return SimpleNamespace(end=end,text="\n".join(m.get("text") or "" for m in messages),identity=identity)


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


@pytest.mark.parametrize("mode,reason", [("WP01_EXIT2", "WP01_EXIT2_REASON"), ("WP01_JSON", "denied this tool")])
async def test_deny_blocks_and_returns_reason(lane, mode, reason):
    topic, parent, child, prior = await _child(lane, mode)
    command = "printf " + mode + "; touch /workspace/runtime/state/" + mode
    turn = await _tool_turn(lane, child, prior["session_id"], "Call Bash exactly once with command `" + command +
                           "`. Do not retry or use another tool if denied. Reply with the exact hook reason you received.", "Bash")
    rows = _jsonl_rows(lane, prior["session_id"])
    results = _tool_results(rows)
    record = _records(lane)
    denied = [r for r in results if r["tool"] == "Bash" and r["is_error"]]
    # JSON reason is the last completing hook's reason in 2.1.59, so a later no-op
    # can replace it with the generic denial; both observed parallel outcomes are valid.
    raw = json.dumps(rows)
    if mode == "WP01_JSON" and "WP01_JSON_REASON" in raw and "WP01_JSON_REASON" in turn.text:
        reason = "WP01_JSON_REASON"
    matching = [r for r in record if r["input"].get("tool_name") == "Bash" and r["input"].get("tool_input",{}).get("command") == command]
    _proof(lane, mode, {"records":record,"results":results,"model_received_reason":reason in raw,
                        "model_reply_reason":reason in turn.text,"turn_end":turn.end})
    assert matching, "project Bash command hook never ran"
    assert denied and reason in raw and reason in turn.text, "hook reason did not reach the model"
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
    turn = await _tool_turn(lane, child, prior["session_id"], request, "Bash")
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
    denied = await lane.turn(child["topic_id"], "Call Write exactly once for /workspace/runtime/git/obs-vault-active/Misc/Meeting Notes/wp01-forbidden.md "
                             "with content WP01. Do not retry. Then reply DONE.")
    assert not denied.end.get("failed"), denied.end
    assert not (lane.lane_dir / "vault/Misc/Meeting Notes/wp01-forbidden.md").exists(), "real OBS SDK callback did not deny immutable source"


def _wire(lane, rows):
    result=[]
    for row in rows:
        if row.get("_source") != "proxy" or row.get("path", "").split("?")[0] != "/v1/messages":
            continue
        paths=list((lane.lane_dir / "logs/cache-proxy/requests").glob("*/" + row["req_id"] + ".wire.json.gz"))
        if not paths: continue
        with gzip.open(paths[0]) as f: body=json.load(f)
        if not body.get("tools"): continue
        result.append({"req_id":row["req_id"],"system_cc":[b.get("cache_control") for b in body.get("system",[]) if b.get("cache_control")],
                       "usage":row.get("usage"),"http_status":row.get("http_status")})
    return result


async def test_restart_keeps_ttl_and_whole_prefix(lane):
    topic,parent,child,prior=await _child(lane,"restart-cache")
    sid=prior["session_id"]
    # Bind executed identity with a real tool call before warming the prefix.
    identity=await _tool_turn(lane,child,sid,"Call Bash exactly once with command `printf WP01_CACHE`. Then reply READY.","Bash")
    for _ in range(2):
        warm=await lane.turn(child["topic_id"],"Remember codeword WP01-CACHE. Reply exactly WP01-CACHE.")
        assert not warm.end.get("failed"), warm.end
    before=_wire(lane,lane.reqlog(agent=child["agent_name"]))
    assert before,before
    mark=time.time()
    await lane.restart_obs("maintenance")
    resumed=await lane.turn(child["topic_id"],"What codeword did I ask you to remember? Reply with the codeword only.")
    after=_wire(lane,lane.reqlog(agent=child["agent_name"],since=mark))
    tally=await lane.exec("/workspace/obs/.venv/bin/python",str(Path(__file__).resolve().parents[4]/"scripts/cache_tally.py"),
                          "--since",datetime.datetime.fromtimestamp(mark,datetime.timezone.utc).isoformat())
    _proof(lane,"restart-cache",{"identity":identity.identity,"before":before,"after":after,"resumed":resumed.end,"tally":tally})
    assert resumed.end.get("session_id")==sid and "WP01-CACHE" in resumed.text,resumed.end
    assert after and before[-1]["system_cc"]==after[0]["system_cc"] and before[-1]["system_cc"],(before,after)
    usage=after[0]["usage"] or {}
    read=usage.get("cache_read",0)
    created=usage.get("cache_creation",0)
    fresh=usage.get("input_tokens",0)
    assert read>0 and read/(read+created+fresh)>=.95 and created/(read+created+fresh)<.05,usage
    assert tally.get("returncode",tally.get("rc",0))==0,tally
