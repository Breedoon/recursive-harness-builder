"""Validate tool/MCP shapes with existing explicit per-agent executor privileges."""
import pytest

from tests.e2e.harness.suites.test_evc_lifecycle import _run
from tests.e2e.harness.suites.test_evc_round2 import (
    test_isolation_i,
    test_isolation_ii,
    test_isolation_iii,
)

pytestmark = [pytest.mark.suite("evc-executor-shapes", "isolation")]


@pytest.mark.parametrize("shape", ["S2", "S5m"])
async def test_executor_shape_native_stop(lane, shape):
    await _run(lane, "RC-alt", shape, "P1a")
