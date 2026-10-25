"""Three native P1c samples; prepared during launch hold, never auto-started."""
import pytest

from tests.e2e.harness.suites import test_isolation as isolation
from tests.e2e.harness.suites.test_evc_lifecycle import _run

pytestmark = [pytest.mark.suite("evc-child-stop", "isolation")]


@pytest.mark.needs(all_lanes=True)
async def test_isolation_i(lane):
    await isolation.test_i_lane_sees_only_its_own_processes(lane)


@pytest.mark.needs(all_lanes=True)
async def test_isolation_ii(lane):
    await isolation.test_ii_host_pid_is_unreachable(lane)


@pytest.mark.needs(all_lanes=True)
async def test_isolation_iii(lane):
    await isolation.test_iii_lane_env_is_the_allow_list(lane)


@pytest.mark.parametrize("config,shape", [("RC-prod", "S2"), ("RC-prod", "S3"), ("RC-alt", "S4")])
async def test_native_child_stop(lane, config, shape):
    await _run(lane, config, shape, "P1c")
