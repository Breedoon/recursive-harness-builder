"""First three still-unattempted latch cells; no replay of the prior sample cohort."""
import pytest

from tests.e2e.harness.suites.test_evc_latch import run_latch
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-latch-remaining-sample", "isolation")]


@pytest.mark.parametrize("config,shape", [("RC-prod", "S4"), ("RC-prod", "S5h"), ("RC-alt", "S1")])
async def test_remaining_latch_sample(lane, config, shape):
    await run_latch(lane, config, shape)
