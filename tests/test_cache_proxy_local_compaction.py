"""Local-route compaction prefix sharing (obs-9).

The CLI's compaction request swaps the last system block for a summarizer
prompt, cuts tools to [Read] and disables thinking, so on a local model it
shares no prefix with the conversation (a 974k-token full prefill on
2026-10-18). The proxy rewrites it onto the session's last main-request shape.
"""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy
from cache_proxy import LocalCompactShapes, share_local_compaction_prefix

BILL = {"type": "text", "text": "x-anthropic-billing-header: cc_version=x; cc_entrypoint=sdk-py;"}
SDK = {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."}
MAIN_SYS = {"type": "text", "text": "\nYou are an interactive agent ... long prompt",
            "cache_control": {"type": "ephemeral"}}
TOOLS = [{"name": "Bash", "input_schema": {}}, {"name": "Read", "input_schema": {}}]
MSGS = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}]


def main_request():
    return {"model": "local-x", "system": [BILL, SDK, MAIN_SYS], "tools": copy.deepcopy(TOOLS),
            "thinking": {"type": "enabled", "budget_tokens": 31999}, "max_tokens": 32000,
            "messages": copy.deepcopy(MSGS), "stream": True}


def compaction_request():
    return {"model": "local-x",
            "system": [BILL, SDK, {"type": "text", "text": cache_proxy.LOCAL_COMPACT_SYSTEM_TEXT,
                                   "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Read", "input_schema": {}}], "max_tokens": 20000,
            "temperature": 1,
            "messages": copy.deepcopy(MSGS) + [
                {"role": "user", "content": [{"type": "text", "text": "Your task is to create a detailed summary"}]}],
            "stream": True}


def test_compaction_rewritten_to_main_shape(tmp_path):
    store = LocalCompactShapes(str(tmp_path / "s.json"))
    main = main_request()
    assert share_local_compaction_prefix(main, "sess", store) == "remembered"
    comp = compaction_request()
    assert share_local_compaction_prefix(comp, "sess", store) == "rewritten"
    m = main_request()
    for k in ("system", "tools", "thinking", "max_tokens"):
        assert comp[k] == m[k]
    assert "temperature" not in comp
    # messages are the CLI's, untouched, including the summary instruction
    assert comp["messages"][:2] == MSGS
    assert comp["messages"][-1]["content"][0]["text"].startswith("Your task")
    # the shared prefix is now byte-identical up to the messages
    pre = lambda b: json.dumps({k: b[k] for k in ("system", "tools", "thinking")}, sort_keys=True)
    assert pre(comp) == pre(m)


def test_shape_survives_restart(tmp_path):
    path = str(tmp_path / "s.json")
    share_local_compaction_prefix(main_request(), "sess", LocalCompactShapes(path))
    comp = compaction_request()
    assert share_local_compaction_prefix(comp, "sess", LocalCompactShapes(path)) == "rewritten"
    assert comp["tools"] == TOOLS


def test_unknown_session_or_no_session_left_alone(tmp_path):
    store = LocalCompactShapes(str(tmp_path / "s.json"))
    share_local_compaction_prefix(main_request(), "other", store)
    comp = compaction_request()
    before = copy.deepcopy(comp)
    assert share_local_compaction_prefix(comp, "sess", store) == "no-shape"
    assert comp == before
    assert share_local_compaction_prefix(comp, "", store) == ""
    assert comp == before


def test_toolless_helper_requests_not_remembered(tmp_path):
    store = LocalCompactShapes(str(tmp_path / "s.json"))
    share_local_compaction_prefix(main_request(), "sess", store)
    helper = {"model": "local-x", "system": [BILL, {"type": "text", "text": "title gen"}],
              "messages": copy.deepcopy(MSGS)}
    assert share_local_compaction_prefix(helper, "sess", store) == ""
    comp = compaction_request()
    share_local_compaction_prefix(comp, "sess", store)
    assert comp["tools"] == TOOLS  # still the main shape, not the helper's


def test_unchanged_shape_not_rewritten_to_disk(tmp_path):
    path = tmp_path / "s.json"
    store = LocalCompactShapes(str(path))
    share_local_compaction_prefix(main_request(), "sess", store)
    mtime = path.stat().st_mtime_ns
    path.write_text(path.read_text())  # same content, new mtime baseline
    mtime = path.stat().st_mtime_ns
    share_local_compaction_prefix(main_request(), "sess", store)
    assert path.stat().st_mtime_ns == mtime
