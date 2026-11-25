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


def _session(tmp_path: Path, *, orphan_tool_use: bool = False) -> Path:
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
    u1 = _row("user", done["uuid"], "next", t(10)); rows.append(u1)
    parent = u1["uuid"]
    if orphan_tool_use:
        dangling = _row("assistant", parent, [{"type": "tool_use", "id": "tX", "name": "Bash", "input": {}}], t(11),
                        model="claude-sonnet-5-5", id="m3", stop_reason="tool_use"); rows.append(dangling)
        parent = dangling["uuid"]
    refused = _row("assistant", parent, [{"type": "text", "text": "API Error: ... Usage Policy"}], t(13),
                   isApiErrorMessage=True, model="<synthetic>", stop_reason="stop_sequence"); rows.append(refused)
    u2 = _row("user", refused["uuid"], "again", t(20)); rows.append(u2)
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
