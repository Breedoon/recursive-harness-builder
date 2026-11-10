"""Native S1 recipes; each run retains the normal current pool preflight."""
import pytest

from tests.e2e.harness.suites.test_evc_lifecycle import _run
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-background", "isolation")]


@pytest.mark.parametrize("path", ["P1a", "P1c", "P2", "P7"])
async def test_native_background_shape(lane, path):
    await _run(lane, "RC-alt", "S1", path)
