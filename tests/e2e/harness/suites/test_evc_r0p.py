"""Named R0-prime reproduction, original pinned strict-xfail case unchanged."""
import pytest

from tests.e2e.harness.suites.test_stop_escalation import test_stop_escalation_reaps_owned_cli_obs_22_43
from tests.e2e.harness.suites.test_evc_round2 import (
    test_isolation_i,
    test_isolation_ii,
    test_isolation_iii,
)

pytestmark = [pytest.mark.suite("evc-r0p", "isolation")]
