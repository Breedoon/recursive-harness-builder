"""Declared protocol-stub harness exercise for each authored replacement recipe id.

All raw recipe statuses remain NEEDS-OBS-CLI even when these harness tests pass.
"""
import itertools
import json
from pathlib import Path

import pytest

from tests.e2e.harness.suites.test_evc_round2 import (
    test_isolation_i,
    test_isolation_ii,
    test_isolation_iii,
)

pytestmark = [pytest.mark.suite("evc-stub", "isolation")]
SHAPES = ("S1", "S2", "S3", "S4", "S5h", "S5m")
PATHS = ("P1a", "P1b", "P1c", "P1d", "P2", "P3", "P4", "P5", "P6", "P7", "P8")


async def stub_recipe(lane, shape, path):
    cli = Path(lane.manifest["cli_path"])
    assert cli.name == "evc_stream_stub.py", "Only the declared E3 stub belongs in this fixture."
    construction = f"{shape}-{path}-obs-cli"
    result = await lane.exec("/workspace/obs/.venv/bin/python", "-m", "tests.e2e.harness.evc_stub_driver",
        "--cli", str(cli), "--label", construction, cwd=lane.info["harness_root"],
        env={"PYTHONPATH": lane.info["harness_root"]})
    proof = json.loads(result["stdout"]) if result["rc"] == 0 else {
        "raw_status": "NEEDS-OBS-CLI", "driver_error": result["stderr"][-2000:]}
    proof.update(construction_id=construction, run_id=lane.manifest["run_id"], lane=lane.name,
                 shape=shape, path=path, driver_rc=result["rc"],
                 actual_venue="R13_HARNESS_STUB_NOT_REAL_RUNTIME")
    target = lane.run_dir / "tests" / lane.name / "evc-stub"
    target.mkdir(parents=True, exist_ok=True)
    (target / (construction + ".json")).write_text(json.dumps(proof, indent=2) + "\n")
    assert result["rc"] == 0, {"rc": result["rc"], "stderr": result["stderr"][-2000:]}
    assert proof["raw_status"] == "NEEDS-OBS-CLI"


@pytest.mark.parametrize("shape,path", list(itertools.product(SHAPES, PATHS)), ids=lambda value: value)
async def test_declared_stub_recipe(lane, shape, path):
    await stub_recipe(lane, shape, path)
