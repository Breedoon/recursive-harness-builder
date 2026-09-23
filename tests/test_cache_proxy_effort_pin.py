"""Mid-session effort pinning in the cache proxy (pin_effort).

Live cache evidence lives in obs-artifacts part3-B2.md; these tests pin the
wire contract: top-level effort stays at the first-seen level, changes become
per-message system effort messages that are reproduced byte-identically.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cache_proxy as cp  # noqa: E402

MODEL = "claude-opus-5-5"


def user(text, cc=False):
    block = {"type": "text", "text": text}
    if cc:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return {"role": "user", "content": [block]}


def asst(text):
    return {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": text}]}


def tool_result():
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}


def body(msgs, effort=None, model=MODEL):
    b = {"model": model, "messages": copy.deepcopy(msgs)}
    if effort:
        b["output_config"] = {"effort": effort}
    return b


def sys_msgs(b):
    return [(i, m["output_config"]["effort"]) for i, m in enumerate(b["messages"])
            if m["role"] == "system"]


def store(tmp_path):
    return cp.EffortPinStore(str(tmp_path / "pins.json"))


def test_unsupported_models_untouched(tmp_path):
    s = store(tmp_path)
    for model in ("claude-fable-5", "claude-haiku-4-5", "gpt-6-luna", "claude-opus-4-6"):
        b = body([user("a")], "low", model)
        before = copy.deepcopy(b)
        assert cp.pin_effort(b, s)["inserted"] == 0
        assert b == before


def test_first_request_sets_base_and_sends_as_is(tmp_path):
    s = store(tmp_path)
    b = body([user("a", cc=True)], "low")
    r = cp.pin_effort(b, s)
    assert r == {"inserted": 0, "changed": False, "beta": False}
    assert b["output_config"] == {"effort": "low"}


def test_change_pins_top_level_and_inserts_message(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a", cc=True)], "low"), s)
    b = body([user("a"), asst("x"), user("b", cc=True)], "max")
    r = cp.pin_effort(b, s)
    assert r["changed"] and r["beta"]
    assert b["output_config"] == {"effort": "low"}
    assert sys_msgs(b) == [(2, "max")]
    assert b["messages"][3]["content"][0]["text"] == "b"


def test_injected_messages_reproduced_when_cache_control_moves(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a", cc=True)], "low"), s)
    cp.pin_effort(body([user("a"), asst("x"), user("b", cc=True)], "max"), s)
    b = body([user("a"), asst("x"), user("b"), asst("y"), user("c", cc=True)], "max")
    r = cp.pin_effort(b, s)
    assert not r["changed"]
    assert sys_msgs(b) == [(2, "max")]
    assert b["output_config"] == {"effort": "low"}


def test_back_to_base_adds_explicit_message(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a")], "low"), s)
    cp.pin_effort(body([user("a"), asst("x"), user("b")], "max"), s)
    b = body([user("a"), asst("x"), user("b"), asst("y"), user("c")], "low")
    cp.pin_effort(b, s)
    assert sys_msgs(b) == [(2, "max"), (5, "low")]


def test_conversation_first_seen_midway_is_pinned_to_default(tmp_path):
    # Pre-deploy history was cached with no top-level effort.
    s = store(tmp_path)
    b = body([user("a"), asst("x"), user("b")], "low")
    cp.pin_effort(b, s)
    assert "output_config" not in b
    assert sys_msgs(b) == [(2, "low")]


def test_high_equals_default(tmp_path):
    s = store(tmp_path)
    b = body([user("a"), asst("x"), user("b")], "high")
    assert cp.pin_effort(b, s)["inserted"] == 0
    assert "output_config" not in b
    b = body([user("a"), asst("x"), user("b"), asst("y"), user("c")], "low")
    cp.pin_effort(b, s)
    b = body([user("a"), asst("x"), user("b"), asst("y"), user("c"), asst("z"),
              user("d")], None)
    cp.pin_effort(b, s)
    assert sys_msgs(b) == [(4, "low"), (7, "high")]


def test_change_mid_tool_loop_is_deferred(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a")], "low"), s)
    b = body([user("a"), asst("x"), tool_result()], "max")
    r = cp.pin_effort(b, s)
    assert r["inserted"] == 0 and not r["changed"]
    assert b["output_config"] == {"effort": "low"}
    b = body([user("a"), asst("x"), tool_result(), asst("y"), user("b")], "max")
    cp.pin_effort(b, s)
    assert sys_msgs(b) == [(4, "max")]


def test_fork_reproduces_parent_messages(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a")], "low"), s)
    cp.pin_effort(body([user("a"), asst("x"), user("b")], "max"), s)
    fork = body([user("a"), asst("x"), user("b"), asst("y"), user("fork")], "max")
    cp.pin_effort(fork, s)
    assert sys_msgs(fork) == [(2, "max")]
    assert fork["output_config"] == {"effort": "low"}


def test_store_survives_restart(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("a")], "low"), s)
    cp.pin_effort(body([user("a"), asst("x"), user("b")], "max"), s)
    s2 = store(tmp_path)
    b = body([user("a"), asst("x"), user("b"), asst("y"), user("c")], "max")
    cp.pin_effort(b, s2)
    assert sys_msgs(b) == [(2, "max")]
    assert b["output_config"] == {"effort": "low"}


def test_identical_first_message_with_other_effort_passes_through(tmp_path):
    s = store(tmp_path)
    cp.pin_effort(body([user("same")], "low"), s)
    b = body([user("same")], "max")
    cp.pin_effort(b, s)
    assert b["output_config"] == {"effort": "max"}
    assert sys_msgs(b) == []


def test_other_output_config_keys_preserved(tmp_path):
    s = store(tmp_path)
    b = {"model": MODEL, "messages": [user("a"), asst("x"), user("b")],
         "output_config": {"effort": "low", "format": {"type": "json"}}}
    cp.pin_effort(b, s)
    assert b["output_config"] == {"format": {"type": "json"}}


def test_add_beta():
    h = {"anthropic-beta": "a,b"}
    cp._add_beta(h, cp.MID_EFFORT_BETA)
    assert h["anthropic-beta"] == "a,b," + cp.MID_EFFORT_BETA
    cp._add_beta(h, cp.MID_EFFORT_BETA)
    assert h["anthropic-beta"].count(cp.MID_EFFORT_BETA) == 1
    h2 = {}
    cp._add_beta(h2, cp.MID_EFFORT_BETA)
    assert h2 == {"anthropic-beta": cp.MID_EFFORT_BETA}
