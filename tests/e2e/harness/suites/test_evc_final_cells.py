"""Correct actual S1 env input and measure the remaining alt pre-tool race with the repaired watcher."""
import pytest

from tests.e2e.harness.suites.test_evc_latch import run_latch
from tests.e2e.harness.suites.test_evc_lifecycle import _run
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-final-cells", "isolation")]


async def test_background_stop_exact_env(lane):
    # Prior actual input misspelled CLAUDE as CLAAUDE; retain that failure, require the real key here.
    await _run(lane, "RC-alt", "S1", "P1a")


async def test_alt_pretool_corrected_signal_timing(lane):
    await run_latch(lane, "RC-alt", "S4")
