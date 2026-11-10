"""Named R0 reproduction, original pinned case and known-bug marker unchanged."""
import pytest

from tests.e2e.harness.suites.test_stop_preflight import test_preflight_stop_obs_22_24
from tests.e2e.harness.suites.test_evc_round2 import (
    test_isolation_i,
    test_isolation_ii,
    test_isolation_iii,
)

pytestmark = [pytest.mark.suite("evc-r0", "isolation")]
