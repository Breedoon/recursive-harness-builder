"""Two stream-recovery paths with exact resumed CLI identity; no route/MCP replay."""
import pytest

from tests.e2e.harness.suites.test_evc_midturn import run_midturn
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-stream-recovery", "isolation")]


@pytest.mark.parametrize("kind", ["reconnect", "soft-reset"])
async def test_native_stream_recovery(lane, kind):
    await run_midturn(lane, kind)
