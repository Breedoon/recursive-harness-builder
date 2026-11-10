"""Only the two genuine no-tool stream latch cells after native bootstrap metadata binding."""
import pytest

from tests.e2e.harness.suites.test_evc_latch import run_latch
from tests.e2e.harness.suites.test_evc_latch_targeted import REASONED_STREAM
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-latch-streams", "isolation")]


@pytest.mark.parametrize("config", ["RC-prod", "RC-alt"], ids=["rp-stream", "ra-stream"])
async def test_latch_stream_session_binding(lane, config):
    await run_latch(lane, config, "S3", stream_prompt=REASONED_STREAM)
