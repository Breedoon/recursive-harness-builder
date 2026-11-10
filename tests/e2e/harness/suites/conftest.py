"""Planning-branch metadata for EV-C-owned files only; frozen C3 fixtures unchanged."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
async def evc_case_metadata(request, lane):
    if not Path(str(request.node.path)).name.startswith("test_evc_"):
        yield
        return
    root = "/workspace/runtime/state/evc-meta-" + uuid.uuid4().hex[:8]
    helper = str(Path(lane.info["code_dir"]) / "tests/e2e/harness/evc_fixture.py")
    proof = {"run_id": lane.manifest["run_id"], "lane": lane.name, "nodeid": request.node.nodeid,
             "profile": lane.info["profile"], "model": lane.info["model"], "bot_id": lane.info["bot_id"],
             "started_epoch": time.time()}
    before = await lane.exec("/workspace/obs/.venv/bin/python", helper, "snapshot", "--root", root)
    assert before["rc"] == 0, {"metadata_capture": "before", "rc": before["rc"]}
    proof["before"] = json.loads(before["stdout"])
    try:
        yield
    finally:
        after = await lane.exec("/workspace/obs/.venv/bin/python", helper, "snapshot", "--root", root)
        proof["after_rc"] = after["rc"]
        if after["rc"] == 0:
            proof["after"] = json.loads(after["stdout"])
        proof["finished_epoch"] = time.time()
        output = lane.run_dir / "tests" / lane.name / "evc-meta"
        output.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
        (output / (slug + ".json")).write_text(json.dumps(proof, indent=2) + "\n")
        assert after["rc"] == 0, {"metadata_capture": "after", "rc": after["rc"]}
