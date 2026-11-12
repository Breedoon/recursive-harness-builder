"""Harness acceptance self-check: real session identity, then an intentionally wrong request."""
from __future__ import annotations

import json
import uuid

import pytest

from tests.e2e.harness.identity import BUNDLED_PATH, lane_identity
from tests.e2e.harness.results import write_summary

pytestmark = [pytest.mark.suite("cli-identity"), pytest.mark.needs(all_lanes=True)]


async def test_requested_cli_identity_mismatch_is_setup_invalid(lane):
    nonce = uuid.uuid4().hex[:8]
    topic = await lane.new_topic("identity " + nonce)
    first = await lane.turn(topic, "Reply with exactly IDENTITY-" + nonce)
    assert "IDENTITY-" + nonce in first.text and not first.end.get("failed"), first.end
    second = await lane.turn(topic, "Reply with exactly FOLLOWUP-" + nonce)
    assert "FOLLOWUP-" + nonce in second.text and not second.end.get("failed"), second.end
    good = lane_identity(lane.lane_dir, lane.manifest)
    assert good["status"] == "MATCH", good
    wrong = dict(lane.manifest, cli_path=(BUNDLED_PATH if lane.manifest.get("cli_path") else "/deliberately-wrong/obs-cli"))
    bad = lane_identity(lane.lane_dir, wrong)
    assert bad["status"] == "SETUP-INVALID", bad
    assert any("differs" in e["reason"] for e in bad["errors"]), bad
    # Run the real summary writer against copied native records inside the runner.
    fixture = lane.run_dir / "tests" / lane.name / "identity-mismatch"
    target = fixture / "lanes" / lane.name
    target.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "cli-processes.jsonl"):
        (target / name).write_bytes((lane.lane_dir / name).read_bytes())
    wrong["lanes"] = [lane.info]
    (fixture / "manifest.json").write_text(json.dumps(wrong))
    summary = write_summary(fixture, exit_code=0)
    assert summary["exit_code"] == 2 and summary["cli_identity_status"] == "SETUP-INVALID", summary
    (fixture / "positive-and-negative.json").write_text(json.dumps({"positive": good, "negative": bad}, indent=2) + "\n")
