"""Remaining S1 SDK-interrupt cell with all required per-lane isolation probes."""
import pytest

from tests.e2e.harness.suites.test_evc_lifecycle import _run
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-background-p2", "isolation")]


async def test_remaining_native_background_interrupt(lane):
    await _run(lane, "RC-alt", "S1", "P2")
