"""Unit checks for the 2026-10-12 (mission vault-1r7n) proxy changes.

These are NOT evidence that caching works (unit tests never were): the live
proof is scripts/prefix_repro.py (ws_resume, volatile_flip, boundary_* and the
full suite) plus the request log. They only pin the pure-function contracts.
"""
import json

import cache_proxy

OE = ("# Output efficiency\n\nIMPORTANT: Go straight to the point.\n\n"
      "Keep your text output brief.\n\n")
HEAD = "\nYou are an interactive agent.\n\n# Tone\n\nBe nice. End with a period.\n\n"
TAIL = "# auto memory\n\nYou have a memory dir.\n\ngitStatus: normalized"


def _body(with_oe=True, cm=True, first="hello", extra=()):
    sys2 = HEAD + (OE if with_oe else "") + TAIL
    body = {
        "model": "claude-opus-5-5",
        "system": [{"type": "text", "text": "x-anthropic-billing-header: v"},
                   {"type": "text", "text": "You are a Claude agent."},
                   {"type": "text", "text": sys2, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": first}]}, *extra],
        "thinking": {"type": "enabled", "budget_tokens": 1000},
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }
    if cm:
        body["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    return body


def _store(tmp_path):
    return cache_proxy.EffortPinStore(str(tmp_path / "pins.json"))


def test_removal_of_section_matches_cli_absent_form():
    with_oe, without = _body(True), _body(False)
    found = cache_proxy._find_volatile_section(with_oe["system"], "# Output efficiency")
    i, s, e = found[0], found[1], found[2]
    t = with_oe["system"][i]["text"]
    assert t[:s] + t[e:] == without["system"][2]["text"]


def test_section_flip_out_is_restored(tmp_path):
    store = _store(tmp_path)
    first = _body(True)
    cache_proxy.pin_volatile_config(first, store)
    later = _body(False, extra=[{"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                                {"role": "user", "content": [{"type": "text", "text": "more"}]}])
    res = cache_proxy.pin_volatile_config(later, store)
    assert "restored:# Output efficiency" in res["applied"]
    assert later["system"] == _body(True)["system"]


def test_section_flip_in_is_removed(tmp_path):
    store = _store(tmp_path)
    cache_proxy.pin_volatile_config(_body(False, cm=False), store)
    later = _body(True, cm=True)
    res = cache_proxy.pin_volatile_config(later, store)
    assert later["system"] == _body(False)["system"]
    assert "context_management" not in later
    assert "removed:context_management" in res["applied"]


def test_context_management_restored_only_with_thinking(tmp_path):
    store = _store(tmp_path)
    cache_proxy.pin_volatile_config(_body(True, cm=True), store)
    later = _body(True, cm=False)
    res = cache_proxy.pin_volatile_config(later, store)
    assert later["context_management"] == _body(True)["context_management"]
    assert res["beta"] is True
    no_think = _body(True, cm=False)
    no_think.pop("thinking")
    cache_proxy.pin_volatile_config(no_think, store)
    assert "context_management" not in no_think


def test_pin_survives_restart_and_is_shared_by_forks(tmp_path):
    store = _store(tmp_path)
    cache_proxy.pin_volatile_config(_body(True), store)
    reloaded = _store(tmp_path)  # proxy restart
    fork = _body(False, extra=[{"role": "assistant", "content": [{"type": "text", "text": "a"}]},
                               {"role": "user", "content": [{"type": "text", "text": "fork turn"}]}])
    cache_proxy.pin_volatile_config(fork, reloaded)
    assert fork["system"] == _body(True)["system"]


def test_new_conversation_records_current_form(tmp_path):
    store = _store(tmp_path)
    cache_proxy.pin_volatile_config(_body(True, first="conv A"), store)
    b = _body(False, first="conv B")
    res = cache_proxy.pin_volatile_config(b, store)
    assert res["recorded"] and not res["applied"]
    assert b["system"] == _body(False)["system"]


def test_toolless_side_calls_are_not_pinned(tmp_path):
    store = _store(tmp_path)
    b = _body(True)
    b.pop("tools")
    assert cache_proxy.pin_volatile_config(b, store) == {"recorded": False, "applied": [], "beta": False}
    assert not store.variants


def test_both_breakpoints_split_system_is_left_untouched():
    body = {"system": [
        {"type": "text", "text": "x-anthropic-billing-header: v"},
        {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
        {"type": "text", "text": "\nYou are an interactive agent x", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "# auto memory\nm", "cache_control": {"type": "ephemeral"}},
    ]}
    before = json.loads(json.dumps(body))
    cache_proxy.normalize_dynamic_boundary(body)
    assert body == before
    assert all(b["text"] for b in body["system"])  # never an empty text block


def test_conversation_cache_key_per_conversation_shared_by_fork():
    def b(first, n=1):
        msgs = [{"role": "user", "content": [{"type": "text", "text": first}]}]
        msgs += [{"role": "user", "content": [{"type": "text", "text": f"t{i}"}]} for i in range(n)]
        return {"model": "gpt-6.1-sol", "messages": msgs,
                "metadata": {"user_id": "user_ab_account__session_0"}}
    a1, a2, other = b("A"), b("A", 3), b("B")
    k1 = cache_proxy.assign_conversation_cache_key(a1)
    k2 = cache_proxy.assign_conversation_cache_key(a2)
    k3 = cache_proxy.assign_conversation_cache_key(other)
    assert k1 == k2 != k3
    assert a1["metadata"]["user_id"] == "user_ab_account__session_" + k1
    untouched = {"model": "m", "messages": a1["messages"], "metadata": {"user_id": "user_x"}}
    assert cache_proxy.assign_conversation_cache_key(untouched) is None
