"""Native C3 half-selection proof, not replacement-runtime evidence."""
import json

import pytest

from tests.e2e.harness.suites import test_isolation as isolation

pytestmark = [pytest.mark.suite("evc-half", "isolation")]


@pytest.mark.both_runtimes
async def test_half_runtime(lane, runtime):
    assert runtime == "cc-2.1.59", "This named proof selects the pinned half, never a replacement stub."
    await isolation.test_i_lane_sees_only_its_own_processes(lane)
    await isolation.test_ii_host_pid_is_unreachable(lane)
    await isolation.test_iii_lane_env_is_the_allow_list(lane)
    script = (
        "import hashlib,json,subprocess,claude_agent_sdk;from pathlib import Path;"
        "p=Path(claude_agent_sdk.__file__).parent/'_bundled'/'claude';"
        "print(json.dumps({'binary':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
        "'version':subprocess.check_output([str(p),'--version'],text=True).strip()}))"
    )
    result = await lane.exec("/workspace/obs/.venv/bin/python", "-c", script)
    assert result["rc"] == 0, result
    data = json.loads(result["stdout"])
    assert data["sha256"].startswith("7a4a6539"), data
    proof = {"run_id": lane.manifest["run_id"], "runtime_fixture": runtime,
             "binary": data, "selectors": {"tests": lane.manifest["tests"], "keyword": lane.manifest.get("keyword")}}
    root = lane.run_dir / "tests" / lane.name
    root.mkdir(parents=True, exist_ok=True)
    (root / "evc-half.json").write_text(json.dumps(proof, indent=2) + "\n")
