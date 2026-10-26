"""Three primed native close paths; do not replay the completed MCP latch cell."""
import pytest

from tests.e2e.harness.suites.test_evc_midturn import test_native_midturn_close
from tests.e2e.harness.suites.test_evc_round2 import test_isolation_i, test_isolation_ii, test_isolation_iii

pytestmark = [pytest.mark.suite("evc-midturn-primed", "isolation")]
