"""obs-97: a mid-session synthetic API-error row must not freeze parallel siblings before it.

Live counterpart: scripts/prefix_repro.py refusal_parallel_resume.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from obs_agent import jsonl_replay

SID = "11111111-2222-3333-4444-555555555555"


def _row(kind, parent, content, ts, **extra):
    out = {"type": kind, "uuid": str(uuid.uuid4()), "parentUuid": parent, "sessionId": SID,
           "timestamp": ts, "message": {"role": kind, "content": content}}
    for key, value in extra.items():
        (out["message"] if key in ("model", "stop_reason", "id") else out)[key] = value
    return out


def _session(tmp_path: Path, *, orphan_tool_use: bool = False, refusal_thinking: bool = False,
             synthetic: bool = True, early_error: bool = False) -> Path:
    t = "2026-11-25T03:{:02d}:00Z".format
    rows = []
    u0 = _row("user", None, "start", t(0)); rows.append(u0)
    think = _row("assistant", u0["uuid"], [{"type": "thinking", "thinking": "", "signature": "s"}], t(1),
                 model="claude-sonnet-5-5", id="m1", stop_reason="tool_use"); rows.append(think)
    a = _row("assistant", think["uuid"], [{"type": "tool_use", "id": "tA", "name": "Read", "input": {}}], t(1),
             model="claude-sonnet-5-5", id="m1", stop_reason="tool_use"); rows.append(a)
    ra = _row("user", a["uuid"], [{"type": "tool_result", "tool_use_id": "tA", "content": "a"}], t(2)); rows.append(ra)
    b = _row("assistant", ra["uuid"], [{"type": "tool_use", "id": "tB", "name": "Read", "input": {}}], t(2),
             model="claude-sonnet-5-5", id="m1", stop_reason="tool_use"); rows.append(b)
    c = _row("assistant", b["uuid"], [{"type": "tool_use", "id": "tC", "name": "Read", "input": {}}], t(2),
             model="claude-sonnet-5-5", id="m1", stop_reason="tool_use"); rows.append(c)
    rb = _row("user", b["uuid"], [{"type": "tool_result", "tool_use_id": "tB", "content": "b"}], t(3)); rows.append(rb)
    rc = _row("user", c["uuid"], [{"type": "tool_result", "tool_use_id": "tC", "content": "c"}], t(3)); rows.append(rc)
    done = _row("assistant", rc["uuid"], [{"type": "text", "text": "read"}], t(4),
                model="claude-sonnet-5-5", id="m2", stop_reason="end_turn"); rows.append(done)
    parent = done["uuid"]
    if early_error:
        early = _row("assistant", parent, [{"type": "text", "text": "API Error: Request timed out"}], t(5),
                     isApiErrorMessage=True, model="<synthetic>", stop_reason="stop_sequence"); rows.append(early)
        parent = early["uuid"]
    u1 = _row("user", parent, "next", t(10)); rows.append(u1)
    parent = u1["uuid"]
    if orphan_tool_use:
        dangling = _row("assistant", parent, [{"type": "tool_use", "id": "tX", "name": "Bash", "input": {}}], t(11),
                        model="claude-sonnet-5-5", id="m3", stop_reason="tool_use"); rows.append(dangling)
        parent = dangling["uuid"]
    if refusal_thinking:
        stopped = _row("assistant", parent, [{"type": "thinking", "thinking": "", "signature": "r"}], t(12),
                       model="claude-sonnet-5-5", id="m3r", stop_reason="refusal"); rows.append(stopped)
        parent = stopped["uuid"]
    if synthetic:
        refused = _row("assistant", parent, [{"type": "text", "text": "API Error: ... Usage Policy"}], t(13),
                       isApiErrorMessage=True, model="<synthetic>", stop_reason="stop_sequence"); rows.append(refused)
        parent = refused["uuid"]
    u2 = _row("user", parent, "again", t(20)); rows.append(u2)
    again = _row("assistant", u2["uuid"], [{"type": "text", "text": "again"}], t(21),
                 model="claude-sonnet-5-5", id="m4", stop_reason="end_turn"); rows.append(again)
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _chain_tool_results(path: Path) -> set[str]:
    """What the CLI's chain-only reader (last UUID wins, walk parentUuid) replays."""
    effective = {}
    for line in path.read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            effective[entry["uuid"]] = entry
    leaf = max(effective.values(), key=lambda e: e["timestamp"])
    seen, cursor = set(), leaf["uuid"]
    while cursor in effective:
        entry = effective[cursor]
        for block in entry["message"]["content"] if isinstance(entry["message"]["content"], list) else []:
            if block.get("type") == "tool_result":
                seen.add(block["tool_use_id"])
        cursor = entry.get("parentUuid")
    return seen


@pytest.fixture(autouse=True)
def _provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_SESSION_REPLAY_PROVENANCE_DIR", str(tmp_path / "prov"))


def test_without_epoch_the_opaque_policy_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, raising=False)
    path = _session(tmp_path)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["legacy_prefix"]["policy"] == "opaque_original_ancestry_preserved_complete_suffix_only"
    assert result["refusal_repair"] is None
    assert "tB" not in _chain_tool_results(path)  # the reproduced defect


def test_error_rows_after_epoch_are_relinked_across(tmp_path, monkeypatch):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T00:00:00Z")
    path = _session(tmp_path)
    original = path.read_bytes()
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["legacy_prefix"] is None
    assert result["refusal_repair"]["policy"] == "synthetic_error_rows_after_epoch_relinked_whole_history"
    assert path.read_bytes().startswith(original)  # append-only
    assert _chain_tool_results(path) == {"tA", "tB", "tC"}
    # idempotent: a second preparation adds nothing
    again = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert again["overlay_count"] == 0


def test_error_rows_before_epoch_stay_opaque(tmp_path, monkeypatch):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T04:00:00Z")
    path = _session(tmp_path)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["refusal_repair"] is None
    assert "tB" not in _chain_tool_results(path)


def test_unanswered_tool_use_damage_stays_opaque(tmp_path, monkeypatch):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T00:00:00Z")
    path = _session(tmp_path, orphan_tool_use=True)
    try:
        result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    except ValueError:
        return  # refused outright, also not repaired
    assert result["refusal_repair"] is None


def test_refusal_thinking_row_plus_synthetic_row_is_relinked(tmp_path, monkeypatch):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T00:00:00Z")
    path = _session(tmp_path, refusal_thinking=True)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["refusal_repair"] is not None
    assert _chain_tool_results(path) == {"tA", "tB", "tC"}


def test_refusal_row_without_synthetic_row_is_not_legacy_damage(tmp_path, monkeypatch):
    monkeypatch.delenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, raising=False)
    path = _session(tmp_path, refusal_thinking=True, synthetic=False)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["legacy_prefix"] is None and result["refusal_repair"] is None
    assert _chain_tool_results(path) == {"tA", "tB", "tC"}  # the healthy path already relinks


def test_errors_on_both_sides_of_the_epoch_stay_opaque(tmp_path, monkeypatch):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T03:08:00Z")
    path = _session(tmp_path, early_error=True)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["refusal_repair"] is None
    assert "tB" not in _chain_tool_results(path)


@pytest.mark.parametrize("value", ["2026-11-25 00:00", "2026-11-25T00:00:00", "yesterday", ""])
def test_unparsable_or_naive_epoch_is_unset(tmp_path, monkeypatch, value):
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, value)
    path = _session(tmp_path)
    result = jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)
    assert result["refusal_repair"] is None


def test_offset_epoch_is_compared_as_a_time(tmp_path, monkeypatch):
    # 2026-11-24T23:30:00-05:00 is 04:30Z, after the 03:13Z error row: no repair.
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-24T23:30:00-05:00")
    path = _session(tmp_path)
    assert jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path)["refusal_repair"] is None
    # 2026-11-24T21:30:00-05:00 is 02:30Z, before it: repaired.
    other = tmp_path / "b"
    other.mkdir()
    path2 = _session(other)
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-24T21:30:00-05:00")
    assert jsonl_replay.prepare_session_replay(session_id=SID, cwd=tmp_path, source_path=path2)["refusal_repair"] is not None


def test_fork_of_a_post_epoch_refusal_session_keeps_the_siblings(tmp_path, monkeypatch):
    from obs_agent.jsonl_fork import fork_session_jsonl
    monkeypatch.setenv(jsonl_replay.REFUSAL_REPAIR_SINCE_ENV, "2026-11-25T00:00:00Z")
    path = _session(tmp_path)
    original = path.read_bytes()
    last = [json.loads(line) for line in path.read_text().splitlines()][-1]["uuid"]
    child = fork_session_jsonl(session_id=SID, target_uuid=last, cwd=tmp_path, source_path=path)
    assert path.read_bytes() == original  # the source is never touched
    assert _chain_tool_results(path.parent / f"{child}.jsonl") == {"tA", "tB", "tC"}
