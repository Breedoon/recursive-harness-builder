"""Bounded five-construction validation after the first native raw failures."""
import pytest

from tests.e2e.harness.suites import test_isolation as isolation
from tests.e2e.harness.suites.test_evc_lifecycle import _run

pytestmark = [pytest.mark.suite("evc-round2", "isolation")]


@pytest.mark.needs(all_lanes=True)
async def test_isolation_i(lane):
    await isolation.test_i_lane_sees_only_its_own_processes(lane)


@pytest.mark.needs(all_lanes=True)
async def test_isolation_ii(lane):
    await isolation.test_ii_host_pid_is_unreachable(lane)


@pytest.mark.needs(all_lanes=True)
async def test_isolation_iii(lane):
    await isolation.test_iii_lane_env_is_the_allow_list(lane)


@pytest.mark.parametrize("config,shape,path", [
    ("RC-prod", "S2", "P1a"), ("RC-prod", "S3", "P1a"),
    ("RC-alt", "S4", "P1a"), ("RC-alt", "S5m", "P1a"),
    ("RC-prod", "S3", "P7"),
])
async def test_round2_construction(lane, config, shape, path):
    await _run(lane, config, shape, path)
