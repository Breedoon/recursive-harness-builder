"""Scratch probe: which agent/team names does a root topic's session carry?"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = [pytest.mark.suite("inc12probe"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]


async def test_probe(lane):
    topic = await lane.new_topic("inc12 probe")
    n = uuid.uuid4().hex[:6].upper()
    first = await lane.turn(topic, f"Reply with exactly: R1-{n}")
    out = {"end1": {k: first.end.get(k) for k in ("agent_name", "team", "session_id")}}
    await lane.command(topic, "/clear")
    second = await lane.turn(topic, f"Reply with exactly: R2-{n}")
    out["end2"] = {k: second.end.get(k) for k in ("agent_name", "team", "session_id")}
    out["builds"] = [{k: e.get(k) for k in ("agent_name", "team", "source", "session_id")} for e in lane.find("session_build")]
    (lane.run_dir / "tests" / lane.name).mkdir(parents=True, exist_ok=True)
    (lane.run_dir / "tests" / lane.name / "probe.json").write_text(json.dumps(out, indent=2, default=str))
