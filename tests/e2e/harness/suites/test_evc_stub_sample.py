"""Four-item protocol-stub validation before the full authored-id batch."""
import pytest

from tests.e2e.harness.suites.test_evc_stub import stub_recipe
from tests.e2e.harness.suites.test_evc_round2 import (
    test_isolation_i,
    test_isolation_ii,
    test_isolation_iii,
)

pytestmark = [pytest.mark.suite("evc-stub-sample", "isolation")]


@pytest.mark.parametrize("shape,path", [("S1", "P1a"), ("S2", "P1c"), ("S3", "P7"), ("S5m", "P6")])
async def test_declared_stub_sample(lane, shape, path):
    await stub_recipe(lane, shape, path)
