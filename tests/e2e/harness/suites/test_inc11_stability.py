"""obs-22.83 increment 11: paired stability probes (2.1.59 vs obs-cli) in the real OBS lane.

Each test is run once per binary (``--cli-path`` selects the candidate) and writes its projected
evidence to ``tests/<lane>/inc11/<test>.json`` in the run directory; the pair is compared from
those files. Tests assert only what both binaries must satisfy; shape differences are evidence,
not assertions.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import pytest

from tests.e2e.harness.suites.test_obs22_agent_shapes import (
    _launch,
    test_maintenance_restart_mid_tool_recovers_by_fork as _shapes_restart_mid_tool,
    test_model_agenttask_fork_inherits_context as _shapes_fork,
    test_model_fresh_child_inbox_wake as _shapes_wake,
)
from tests.e2e.harness.suites.test_tap_fail_open import _messages, _until_changed

pytestmark = [pytest.mark.suite("inc11"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]

GUARD = {"PreToolUse": "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/executor_guard.py::check"}
ERROR_WORDS = re.compile(r"(?i)\b(error|failed|exception|traceback|crash)")

# Scenario 2 and 4 reuse the increment 1b probes under their scenario names.
test_s2_model_agenttask_fork = _shapes_fork
test_s2_model_fresh_child_inbox_wake = _shapes_wake
test_s4_maintenance_restart_mid_tool = _shapes_restart_mid_tool


def _nonce() -> str:
    return uuid.uuid4().hex[:8].upper()


def _flat(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if isinstance(block, dict):
            out.append(block.get("text") if block.get("type") == "text" else json.dumps(block.get("content", ""), default=str))
    return "\n".join(str(item) for item in out)


def _jsonl_rows(lane, session_id):
    path = lane.lane_dir / "home" / ".claude" / "projects" / "-workspace-runtime-git-obs-vault-active" / f"{session_id}.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def _tool_results(rows):
    """[(tool name, is_error, text head)] in order, projected from the transcript."""
    names = {}
    out = []
    for row in rows:
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[block.get("id")] = block.get("name")
            elif block.get("type") == "tool_result":
                out.append({"tool": names.get(block.get("tool_use_id")), "is_error": bool(block.get("is_error")),
                            "head": _flat(block.get("content"))[:120]})
    return out


def _interruption_rows(rows):
    found = []
    for row in rows:
        content = (row.get("message") or {}).get("content")
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        if "interrupted by user" in text.lower() or "user doesn't want to proceed" in text.lower():
            found.append({"type": row.get("type"), "head": text[:200]})
    return found


def _proof(lane, name, data):
    target = lane.run_dir / "tests" / lane.name / "inc11"
    target.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["session_builds"] = [{k: e.get(k) for k in ("runtime", "version", "cli_path", "source", "model", "agent_name")}
                              for e in lane.find("session_build")]
    (target / f"{name}.json").write_text(json.dumps(data, indent=2, default=str) + "\n")


async def _wait_tool_use(lane, session_id, after, tool, needle=""):
    cursor = float(after["ts"])
    async with asyncio.timeout(120):
        while True:
            row = await lane.wait("jsonl_append", match={"session_id": session_id, "type": "assistant"}, after=cursor)
            for block in (row["row"].get("message") or {}).get("content") or []:
                if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool
                        and needle in json.dumps(block.get("input") or {})):
                    return row
            cursor = row


async def _root(lane, label):
    nonce = _nonce()
    topic = await lane.new_topic(f"inc11 {label} {nonce}")
    parent = await lane.turn(topic, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in parent.text and parent.end.get("session_id"), parent.end
    return nonce, topic, parent


async def _bash_child(lane, topic, parent, nonce, seconds):
    """A fresh executor-guarded child that is running `sleep <seconds>` in a foreground Bash call."""
    child = await _launch(lane, topic, parent, {"display_name": f"inc11 child {nonce}", "fork": False, "effort": "low",
                          "hooks": json.dumps(GUARD), "prompt": f"Reply with exactly: CHILD-READY-{nonce}"}, nonce)
    child_end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not child_end.get("failed") and child_end.get("session_id"), child_end
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CHILD-READY-{nonce}"))
    # The parent is woken by the child's idle notice; let that turn end so later asks start their own turn.
    notice = await lane.wait("telegram_send", agent=parent.end["agent_name"], after=child_end,
                             match=lambda e: "went idle" in (e.get("text_head") or ""))
    await lane.wait("turn_end", agent=parent.end["agent_name"], after=notice)
    marker = lane.lane_dir / "state" / f"inc11-bash-{nonce}"
    sent = await lane.send(child["topic_id"], f"Call Bash exactly once in the foreground with command "
                           f"`touch /workspace/runtime/state/{marker.name}; sleep {seconds}; echo SLEPT-{nonce}`. "
                           f"After it returns reply exactly DONE-{nonce}, with no other tools.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    session_id = child_end["session_id"]
    await _wait_tool_use(lane, session_id, start, "Bash", marker.name)

    async def started():
        return marker.exists()
    await _until_changed(lane, started)
    return child, child_end, sent, start, session_id


async def _bot_text_since(lane, topic, sent):
    messages = await lane.telegram.messages(topic, since=sent, from_bot=True)
    return [m.get("text") or "" for m in messages]


def _user_visible_errors(texts):
    return [t[:200] for t in texts if ERROR_WORDS.search(t) and "interrupt sent" not in t.lower()]


# -- scenario 2: obs-agent MCP tools -------------------------------------------------------------

async def test_s2_cron_create_list_delete(lane):
    nonce, topic, parent = await _root(lane, "cron")
    payload = {"schedule_mode": "interval", "interval_seconds": "3600", "prompt": f"inc11 noop {nonce}",
               "description": f"inc11 probe {nonce}", "max_runs": "1", "reset_session": "false", "inherit": "none",
               "cron": "", "prompt_file": "", "from": "", "until": "", "run_mode": ""}
    sent = await lane.send(topic, "Call mcp__obs-agent__CronCreate exactly once with these exact arguments: "
                           + json.dumps(payload) + ". Then call mcp__obs-agent__CronList once. Then call "
                           "mcp__obs-agent__CronDelete once with the id of the schedule CronCreate returned. "
                           f"Finally reply exactly: CRON-{nonce}. Use no other tool.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start)
    results = _tool_results(_jsonl_rows(lane, end["session_id"]))
    _proof(lane, "s2_cron", {"end": end, "tool_results": results[-6:]})
    tools = [r["tool"] for r in results]
    assert not end.get("failed"), end
    for name in ("CronCreate", "CronList", "CronDelete"):
        assert any(t and t.endswith(name) for t in tools), tools


async def test_s2_context_info(lane):
    nonce, topic, parent = await _root(lane, "ctxinfo")
    sent = await lane.send(topic, f"Call mcp__obs-agent__context_info exactly once, then reply exactly: CTX-{nonce}. Use no other tool.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start)
    results = _tool_results(_jsonl_rows(lane, end["session_id"]))
    _proof(lane, "s2_context_info", {"end": end, "tool_results": results[-3:]})
    assert not end.get("failed"), end
    assert any((r["tool"] or "").endswith("context_info") for r in results), results


async def test_s2_inbox_read_and_send_between_agents(lane):
    nonce, topic, parent = await _root(lane, "inbox")
    me = parent.end["agent_name"]
    child = await _launch(lane, topic, parent, {"display_name": f"inc11 inbox child {nonce}", "fork": False, "effort": "low",
                          "prompt": f"Reply with exactly: CHILD-UP-{nonce}"}, nonce)
    child_end = await lane.wait("turn_end", agent=child["agent_name"], after=child)
    assert not child_end.get("failed"), child_end
    mark = lane.events.mark()
    request = await lane.send(topic, "Call `mcp__obs-agent__SendInboxMessage` exactly once to the agent you just launched, "
                              f"with content 'Call mcp__obs-agent__ReadInbox once, then call mcp__obs-agent__SendInboxMessage "
                              f"once to recipient {me} with content ACK-{nonce}, then reply exactly: CHILD-DONE-{nonce}'. "
                              f"Then reply exactly: SENT-{nonce}. Use no other tool.")
    await _until_changed(lane, lambda: _messages(lane, topic, request, f"SENT-{nonce}"))
    woke = await lane.wait("turn_end", agent=child["agent_name"], after=mark)
    await _until_changed(lane, lambda: _messages(lane, child["topic_id"], None, f"CHILD-DONE-{nonce}"))
    back = lane.find("inbox_delivered", agent=me, after=mark)
    results = _tool_results(_jsonl_rows(lane, woke["session_id"]))
    _proof(lane, "s2_inbox", {"child_end": woke, "back_events": back, "child_tool_results": results[-4:]})
    tools = [r["tool"] or "" for r in results]
    assert any(t.endswith("ReadInbox") for t in tools) and any(t.endswith("SendInboxMessage") for t in tools), tools
    assert not woke.get("failed") and woke.get("session_id") == child_end["session_id"], (child_end, woke)


async def test_s2_agenttaskoutput_on_running_child(lane):
    nonce, topic, parent = await _root(lane, "output")
    child, child_end, sent, start, session_id = await _bash_child(lane, topic, parent, nonce, 25)
    args = {"agent_name": child["agent_name"], "task_id": "", "team_name": parent.end["team"], "block": "false", "timeout": "1000"}
    ask = await lane.send(topic, "Call mcp__obs-agent__AgentTaskOutput exactly once with these exact arguments: "
                          + json.dumps(args) + f". Then reply exactly: OUT-{nonce}. Use no other tool.")
    await _until_changed(lane, lambda: _messages(lane, topic, ask, f"OUT-{nonce}"))
    end = await lane.wait("turn_end", agent=child["agent_name"], after=start)
    results = _tool_results(_jsonl_rows(lane, parent.end["session_id"]))
    _proof(lane, "s2_agenttaskoutput", {"parent_results": results[-3:], "child_end": end})
    assert any((r["tool"] or "").endswith("AgentTaskOutput") for r in results), results


# -- scenario 3: interrupts ---------------------------------------------------------------------

async def _assert_stop_clean(lane, topic_id, sent, start, stop_marker, session_id, proof_name, extra=None):
    """After a stop: the turn ended, nothing failed shows, the transcript carries the interruption."""
    texts = await _bot_text_since(lane, topic_id, sent)
    rows = _jsonl_rows(lane, session_id)
    proof = {"turn_end_failed": stop_marker["end"].get("failed"), "turn_end_subtype": stop_marker["end"].get("subtype"),
             "stop_cleanup": stop_marker.get("cleanup"), "user_visible_errors": _user_visible_errors(texts),
             "bot_texts": [t[:160] for t in texts], "interruption_rows": _interruption_rows(rows),
             "last_tool_results": _tool_results(rows)[-3:]}
    proof.update(extra or {})
    return proof


async def test_s3_stop_during_bash(lane):
    nonce, topic, parent = await _root(lane, "stopbash")
    child, child_end, sent, start, session_id = await _bash_child(lane, topic, parent, nonce, 90)
    t_stop = time.time()
    stop = await lane.command(child["topic_id"], "/stop")
    end, cleanup = await asyncio.gather(
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=child["agent_name"], after=start))
    proof = await _assert_stop_clean(lane, child["topic_id"], sent, start, {"end": end, "cleanup": cleanup}, session_id, "s3",
                                     {"stop_to_turn_end_s": round(float(end["ts"]) - t_stop, 1),
                                      "stop_to_cleanup_s": round(float(cleanup["ts"]) - t_stop, 1)})
    after = await lane.turn(child["topic_id"], f"Reply with exactly: AFTER-{nonce}")
    proof["follow_up"] = {"same_session": after.end.get("session_id") == session_id, "end_session": after.end.get("session_id"),
                          "replied": f"AFTER-{nonce}" in after.text}
    _proof(lane, "s3_stop_during_bash", proof)
    assert stop.message_id > sent.message_id
    assert proof["follow_up"]["replied"], after.messages
    assert not proof["turn_end_failed"] and not proof["user_visible_errors"], proof
    assert proof["follow_up"]["same_session"], proof


async def test_s3_stop_during_mcp_tool(lane):
    nonce, topic, parent = await _root(lane, "stopmcp")
    child, child_end, sent_c, start_c, child_session = await _bash_child(lane, topic, parent, nonce, 80)
    args = {"agent_name": child["agent_name"], "task_id": "", "team_name": parent.end["team"], "block": "true", "timeout": "120000"}
    ask = await lane.send(topic, "Call mcp__obs-agent__AgentTaskOutput exactly once with these exact arguments: "
                          + json.dumps(args) + f". Then reply exactly: OUT-{nonce}. Use no other tool.")
    start = await lane.wait("turn_start", match={"trigger_message_id": ask.message_id})
    await _wait_tool_use(lane, parent.end["session_id"], start, "mcp__obs-agent__AgentTaskOutput")
    t_stop = time.time()
    await lane.command(topic, "/stop")
    end, cleanup = await asyncio.gather(
        lane.wait("turn_end", match={"trigger_message_id": ask.message_id}, after=start),
        lane.wait("stop_cleanup", agent=topic, after=start))
    proof = await _assert_stop_clean(lane, topic.topic_id, ask, start, {"end": end, "cleanup": cleanup}, parent.end["session_id"], "s3",
                                     {"stop_to_turn_end_s": round(float(end["ts"]) - t_stop, 1),
                                      "stop_to_cleanup_s": round(float(cleanup["ts"]) - t_stop, 1)})
    after = await lane.turn(topic, f"Reply with exactly: AFTER-{nonce}")
    proof["follow_up"] = {"same_session": after.end.get("session_id") == parent.end["session_id"],
                          "end_session": after.end.get("session_id"), "replied": f"AFTER-{nonce}" in after.text}
    _proof(lane, "s3_stop_during_mcp_tool", proof)
    await lane.command(child["topic_id"], "/stop")
    assert proof["follow_up"]["replied"], after.messages
    assert not proof["turn_end_failed"] and not proof["user_visible_errors"], proof
    assert proof["follow_up"]["same_session"], proof


async def test_s3_agenttaskstop_child_mid_bash(lane):
    nonce, topic, parent = await _root(lane, "taskstop")
    child, child_end, sent, start, session_id = await _bash_child(lane, topic, parent, nonce, 90)
    args = {"agent_name": child["agent_name"], "task_id": "", "team_name": parent.end["team"], "shell_id": ""}
    t_stop = time.time()
    ask = await lane.send(topic, "Call mcp__obs-agent__AgentTaskStop exactly once with these exact arguments: "
                          + json.dumps(args) + f". Then reply exactly: STOPPED-{nonce}. Use no other tool.")
    await _until_changed(lane, lambda: _messages(lane, topic, ask, f"STOPPED-{nonce}"))
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start)
    proof = await _assert_stop_clean(lane, child["topic_id"], sent, start, {"end": end}, session_id, "s3",
                                     {"stop_to_turn_end_s": round(float(end["ts"]) - t_stop, 1),
                                      "parent_results": _tool_results(_jsonl_rows(lane, parent.end["session_id"]))[-2:]})
    after = await lane.turn(child["topic_id"], f"Reply with exactly: AFTER-{nonce}")
    proof["follow_up"] = {"same_session": after.end.get("session_id") == session_id, "end_session": after.end.get("session_id"),
                          "replied": f"AFTER-{nonce}" in after.text}
    _proof(lane, "s3_agenttaskstop_mid_bash", proof)
    assert proof["follow_up"]["replied"], after.messages
    assert not proof["turn_end_failed"] and not proof["user_visible_errors"], proof
    assert proof["follow_up"]["same_session"], proof


# -- scenario 4: restart and resume while idle ----------------------------------------------------

async def _idle_restart(lane, mode, label):
    nonce = _nonce()
    topic = await lane.new_topic(f"inc11 {label} {nonce}")
    code = "CW" + nonce
    first = await lane.turn(topic, f"Remember this codeword for later: {code}. Reply with exactly: NOTED-{nonce}")
    assert f"NOTED-{nonce}" in first.text, first.messages
    session_id = first.end["session_id"]
    mark = lane.events.mark()
    restarted = await lane.restart_obs(mode)
    assert restarted["ready"].get("obs") == "RUNNING", restarted
    second = await lane.turn(topic, "What was the codeword I asked you to remember? Reply with only the codeword.")
    recoveries = lane.find("recovery", agent=first.end["agent_name"], after=mark)
    builds = [{k: e.get(k) for k in ("runtime", "version", "cli_path", "source", "agent_name")}
              for e in lane.find("session_build", after=mark)]
    _proof(lane, f"s4_idle_{mode}", {"session_before": session_id, "session_after": second.end.get("session_id"),
                                     "recoveries": recoveries, "builds_after": builds, "reply_has_code": code in second.text,
                                     "failed": second.end.get("failed")})
    assert code in second.text, second.messages
    assert second.end.get("session_id") == session_id, (session_id, second.end, recoveries)
    assert not recoveries, recoveries


async def test_s4_maintenance_restart_while_idle(lane):
    await _idle_restart(lane, "maintenance", "idlemaint")


async def test_s4_kill_while_idle(lane):
    await _idle_restart(lane, "kill", "idlekill")


# -- scenario 6: thinking and effort on a real model (run with --profile inc11-opus) ---------------

def _wire_fields(lane, rows):
    """The projected request fields the proxy forwarded for main-path rows (no system text, no messages)."""
    import glob
    import gzip
    out = []
    for row in rows:
        if row.get("_source") != "proxy" or row.get("path") != "/v1/messages":
            continue
        hits = glob.glob(str(lane.lane_dir / "logs" / "cache-proxy" / "requests" / "*" / f"{row['req_id']}.wire.json.gz"))
        body = json.load(gzip.open(hits[0])) if hits else {}
        out.append({"model": row.get("model"), "http_status": row.get("http_status"), "stream": row.get("stream"),
                    "has_tools": bool(body.get("tools")), "max_tokens": body.get("max_tokens"),
                    "thinking": body.get("thinking"), "output_config": body.get("output_config"),
                    "temperature": body.get("temperature"), "context_management": body.get("context_management"),
                    "upstream_beta": row.get("upstream_beta"), "effort_pin": row.get("effort_pin"),
                    "usage_keys": sorted((row.get("usage") or {}).keys()) if isinstance(row.get("usage"), dict) else None})
    return out


async def test_s6_effort_high_and_low_on_opus(lane):
    nonce = _nonce()
    topic = await lane.new_topic(f"inc11 effort {nonce}")
    proof = {}
    for level in ("high", "low"):
        mark = lane.events.mark()
        cmd_ts = float(lane.find()[-1]["ts"]) if lane.find() else 0.0
        await lane.command(topic, f"/effort {level}")
        await lane.wait("telegram_send", agent=topic, match=lambda e, l=level: l in (e.get("text_head") or "").lower(), after=mark)
        turn = await lane.turn(topic, f"Reply with exactly: EFF-{level.upper()}-{nonce}")
        assert f"EFF-{level.upper()}-{nonce}" in turn.text, turn.messages
        rows = lane.reqlog(agent=topic, since=cmd_ts)
        proof[level] = {"end": {k: turn.end.get(k) for k in ("failed", "subtype", "turns", "cli_runtime", "cost_usd")},
                        "requests": _wire_fields(lane, rows)}
    _proof(lane, "s6_effort", proof)
    for level in ("high", "low"):
        main = [r for r in proof[level]["requests"] if r["has_tools"]]
        assert main and all(r["http_status"] == 200 for r in main), proof[level]


# -- scenario 5: three automatic compactions on Haiku (run with --profile inc11-compact) ------------

def _compaction_rows(rows):
    return [r for r in rows if r.get("type") == "system" and (r.get("subtype") == "compact_boundary" or "compact" in str(r.get("subtype", "")))
            or r.get("isCompactSummary")]


async def test_s5_three_compactions_then_recall(lane):
    nonce = _nonce()
    code = "CW" + nonce
    topic = await lane.new_topic(f"inc11 compact {nonce}")
    first = await lane.turn(topic, f"Remember this codeword for later: {code}. Reply with exactly: NOTED-{nonce}")
    assert f"NOTED-{nonce}" in first.text, first.messages
    session_id = first.end["session_id"]
    fixture = "/workspace/runtime/git/obs-vault-active/CLAUDE.md"
    turns = []
    for i in range(10):
        offset = 1 + 90 * i
        result = await lane.turn(topic, f"Call the Read tool once on {fixture} with offset {offset} and limit 90. "
                                 f"Do not summarize it. Reply with exactly: READ-{i}-{nonce}", timeout=240)
        session_id = result.end.get("session_id") or session_id
        compactions = _compaction_rows(_jsonl_rows(lane, session_id))
        turns.append({"i": i, "failed": result.end.get("failed"), "subtype": result.end.get("subtype"), "session": session_id,
                      "replied": f"READ-{i}-{nonce}" in result.text, "compact_rows": len(compactions)})
        assert not result.end.get("failed"), (i, result.end)
        if len(compactions) >= 3:
            break
    recall = await lane.turn(topic, "What was the codeword I asked you to remember at the start? Reply with only the codeword.", timeout=240)
    log = (lane.lane_dir / "logs" / "telegram-prod.log").read_text(encoding="utf-8", errors="replace").splitlines()
    summary_requests = [r for r in lane.reqlog(agent=topic) if r.get("_source") == "proxy" and r.get("path") == "/v1/messages"]
    rows = _jsonl_rows(lane, session_id)
    _proof(lane, "s5_compaction", {
        "turns": turns, "compaction_rows": [{"subtype": r.get("subtype"), "type": r.get("type"), "keys": sorted(r)[:12]} for r in _compaction_rows(rows)][:6],
        "recall_has_code": code in recall.text, "recall_end": {k: recall.end.get(k) for k in ("failed", "subtype", "turns", "session_id")},
        "first_session": first.end["session_id"], "final_session": recall.end.get("session_id"),
        "log_compact_lines": [line[:200] for line in log if "ompact" in line][:12],
        "n_proxy_messages_requests": len(summary_requests)})
    assert code in recall.text, recall.messages
    assert not recall.end.get("failed"), recall.end
    assert len(_compaction_rows(rows)) >= 3, turns
