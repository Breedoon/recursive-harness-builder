"""Remaining ready P1a/P1c/P2/P7 recipes after the three native P1c samples.

Previously attempted P1a/P7 cells are intentionally rechecked for the corrected
RC-alt override, exact identity reads and recorded cleanup+2s epoch; not replayed
merely because the launch hold was released.
"""
import itertools

import pytest

from tests.e2e.harness.suites.test_evc_lifecycle import _run
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-matrix", "isolation")]
PAIRS = [(config, shape) for config, shapes in [
    ("RC-prod", ("S2", "S3", "S4", "S5h")),
    ("RC-alt", ("S2", "S3", "S4", "S5h", "S5m"))] for shape in shapes]
SAMPLED = {("RC-prod", "S2", "P1c"), ("RC-prod", "S3", "P1c"), ("RC-alt", "S4", "P1c")}
CASES = [(config, shape, path) for (config, shape), path in itertools.product(PAIRS, ("P1a", "P1c", "P2", "P7"))
         if (config, shape, path) not in SAMPLED]
assert len(CASES) == 33


@pytest.mark.parametrize("config,shape,path", CASES)
async def test_ready_native_recipe(lane, config, shape, path):
    await _run(lane, config, shape, path)
