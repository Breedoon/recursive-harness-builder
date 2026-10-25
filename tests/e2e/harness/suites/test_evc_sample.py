"""EV-C construction sample using the existing C3 cases, Codex only.

Explicit collection file avoids the pinned collection catalogue being written
before pytest's -k deselection; that behavior is recorded, not altered here.
"""

import pytest

from tests.e2e.harness.suites.test_isolation import (
    test_i_lane_sees_only_its_own_processes,
    test_ii_host_pid_is_unreachable,
    test_iii_lane_env_is_the_allow_list,
)
from tests.e2e.harness.suites.test_smoke import (
    test_fork_inherits_context,
    test_send_reply_then_follow_up,
    test_stop_interrupts_running_turn,
)

pytestmark = [pytest.mark.suite("evc-sample", "isolation"), pytest.mark.needs(all_lanes=True)]
