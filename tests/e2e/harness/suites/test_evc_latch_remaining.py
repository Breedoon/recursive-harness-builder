"""Last four latch cells, after the separately recorded remaining-cell sample cohort."""
import pytest

from tests.e2e.harness.suites.test_evc_latch import run_latch
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-latch-remaining", "isolation")]


@pytest.mark.parametrize("config,shape", [("RC-alt", "S2"), ("RC-alt", "S3"), ("RC-alt", "S5h"), ("RC-alt", "S5m")])
async def test_remaining_latch_cells(lane, config, shape):
    await run_latch(lane, config, shape)
