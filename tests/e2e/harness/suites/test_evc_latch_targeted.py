"""Targeted corrected RP foreground race and genuine reasoned S3 streams; no bar changes."""
import pytest

from tests.e2e.harness.suites.test_evc_latch import run_latch
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-latch-targeted", "isolation")]
REASONED_STREAM = (
    "Without using any tools, compute the number of derangements of 23 distinct objects using "
    "D(0)=1, D(1)=0 and D(n)=(n-1)*(D(n-1)+D(n-2)). Give only the numerical answer, "
    "then write every integer 1 through 3000, one per line. No code, explanation or ellipsis."
)


@pytest.mark.parametrize("config,shape,stream_prompt", [
    ("RC-prod", "S2", None),
    ("RC-prod", "S3", REASONED_STREAM),
    ("RC-alt", "S3", REASONED_STREAM),
])
async def test_corrected_latch_targets(lane, config, shape, stream_prompt):
    await run_latch(lane, config, shape, stream_prompt=stream_prompt)
