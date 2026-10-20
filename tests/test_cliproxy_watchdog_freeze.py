"""Freeze branches of deploy/cliproxy-watchdog.sh (obs-15 V1 U2; bead ops-47.6).

The watchdog asks the Agent Operations freeze tool before each restart it performs itself:
- the 429-cooldown and catalog-miss restarts are SKIPPED while a cliproxy/supervisor/all-shared
  freeze is active, and run once the freeze is lifted or acknowledged;
- the liveness path still revives a DEAD proxy during a freeze, with an alert (accepted exception,
  freeze.md v1.1 "Limits").

Each test sources the real script in bash with CLIPROXY_WATCHDOG_SOURCE_ONLY=1, replaces the
network probes with shell stubs, points supervisorctl at a recording stub, and uses the REAL vault
freeze tool against a scratch state file (OPS_FREEZE_STATE). Nothing here touches the live proxy,
the live freeze state or supervisord.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "cliproxy-watchdog.sh"
FREEZE_TOOL = Path(
    os.environ.get(
        "OBS_FREEZE_TOOL",
        "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/"
        "Agent Operations/scripts/ops/freeze.py",
    )
)


def _freeze(id_="t1", scope="cliproxy", lifted=None):
    return {"id": id_, "scope": [scope], "declared_by": "test", "declared_at": "2026-10-20T00:00:00Z",
            "reason": "unit test", "until": None, "lifted_at": lifted}


@unittest.skipUnless(FREEZE_TOOL.is_file() and shutil.which("bash"), "freeze tool or bash unavailable")
class WatchdogFreezeBranches(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cpwd-freeze-"))
        self.calls = self.tmp / "supervisorctl.calls"
        stub = self.tmp / "supervisorctl"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        stub.chmod(0o755)
        self.stub = stub
        self.state = self.tmp / "freezes.json"
        self.state.write_text("[]")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fn(self, body: str, freezes=(), extra_env=None):
        self.state.write_text(json.dumps(list(freezes)))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "CLIPROXY_WATCHDOG_SOURCE_ONLY": "1",
            "CLIPROXY_WATCHDOG_STATE_DIR": str(self.tmp / "state"),
            "CLIPROXY_WATCHDOG_SUPERVISORCTL": str(self.stub),
            "CLIPROXY_WATCHDOG_FREEZE_TOOL": str(FREEZE_TOOL),
            "CLIPROXY_WATCHDOG_POST_RESTART_WAIT_SECS": "0",
            "CLIPROXY_WATCHDOG_MIN_RESTART_GAP_SECS": "0",
            "CLIPROXY_WATCHDOG_LIVENESS_MIN_GAP_SECS": "0",
            "OPS_FREEZE_STATE": str(self.state),
        }
        env.update(extra_env or {})
        script = f'source "{SCRIPT}"\n{body}\n'
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return proc, calls

    # --- 429 cooldown path -------------------------------------------------------------
    COOLDOWN = 'cooling_models() { echo "gpt-x "; }\nhandle_cooldown "gpt-x"'

    def test_cooldown_restart_skipped_during_freeze(self):
        proc, calls = self.run_fn(self.COOLDOWN, [_freeze()])
        self.assertEqual(calls, [], proc.stderr)
        self.assertIn("cooldown 429 restart SKIPPED: active freeze", proc.stderr)

    def test_cooldown_restart_runs_without_freeze(self):
        proc, calls = self.run_fn(self.COOLDOWN, [])
        self.assertEqual(calls, ["restart cliproxyapi"], proc.stderr)

    def test_cooldown_restart_runs_after_lift_and_with_ack(self):
        _, calls = self.run_fn(self.COOLDOWN, [_freeze(lifted="2026-10-20T01:00:00Z")])
        self.assertEqual(calls, ["restart cliproxyapi"])
        self.calls.unlink()
        _, calls = self.run_fn(self.COOLDOWN, [_freeze("t1")], {"OPS_FREEZE_ACK": "t1"})
        self.assertEqual(calls, ["restart cliproxyapi"])

    def test_supervisor_and_all_shared_scopes_also_skip(self):
        for scope in ("supervisor", "all-shared"):
            proc, calls = self.run_fn(self.COOLDOWN, [_freeze(scope=scope)])
            self.assertEqual(calls, [], (scope, proc.stderr))

    def test_unrelated_scope_does_not_skip(self):
        _, calls = self.run_fn(self.COOLDOWN, [_freeze(scope="obs-source")])
        self.assertEqual(calls, ["restart cliproxyapi"])

    def test_freeze_tool_missing_fails_open(self):
        _, calls = self.run_fn(self.COOLDOWN, [_freeze()],
                               {"CLIPROXY_WATCHDOG_FREEZE_TOOL": str(self.tmp / "nope.py")})
        self.assertEqual(calls, ["restart cliproxyapi"])

    def test_corrupt_freeze_state_fails_open(self):
        self.state.write_text("{not json")
        env = {"OPS_FREEZE_STATE": str(self.state)}
        proc = subprocess.run(["bash", "-c", f'source "{SCRIPT}"\n{self.COOLDOWN}\n'],
                              env={**os.environ, **env,
                                   "CLIPROXY_WATCHDOG_SOURCE_ONLY": "1",
                                   "CLIPROXY_WATCHDOG_STATE_DIR": str(self.tmp / "state"),
                                   "CLIPROXY_WATCHDOG_SUPERVISORCTL": str(self.stub),
                                   "CLIPROXY_WATCHDOG_FREEZE_TOOL": str(FREEZE_TOOL),
                                   "CLIPROXY_WATCHDOG_MIN_RESTART_GAP_SECS": "0"},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(self.calls.read_text().splitlines(), ["restart cliproxyapi"], proc.stderr)

    # --- catalog-miss path -------------------------------------------------------------
    CATALOG = ('missing_models_from() { cat >/dev/null; echo "m1"; }\n'
               'served_model_ids() { echo "m1"; }\n'
               'handle_model_catalog "a b"')

    def test_catalog_restart_skipped_during_freeze_and_not_counted(self):
        proc, calls = self.run_fn(self.CATALOG, [_freeze()])
        self.assertEqual(calls, [], proc.stderr)
        self.assertIn("restart SKIPPED, active freeze", proc.stderr)
        state = (self.tmp / "state" / "watchdog-model-state").read_text().split("|")
        self.assertEqual(state[1], "0")  # attempt not counted

    def test_catalog_restart_runs_without_freeze(self):
        _, calls = self.run_fn(self.CATALOG, [])
        self.assertEqual(calls, ["restart cliproxyapi"])

    # --- liveness path: dead-proxy revival is the accepted exception ---------------------
    LIVENESS = 'liveness_restart "proxy dead" restart'

    def test_dead_proxy_revived_during_freeze_with_alert(self):
        proc, calls = self.run_fn(self.LIVENESS, [_freeze()])
        self.assertEqual(calls, ["restart cliproxyapi"])
        self.assertIn("restarting a DEAD proxy despite an active freeze", proc.stderr)

    def test_dead_proxy_revived_without_freeze_no_freeze_alert(self):
        proc, calls = self.run_fn(self.LIVENESS, [])
        self.assertEqual(calls, ["restart cliproxyapi"])
        self.assertNotIn("despite an active freeze", proc.stderr)

    def test_source_only_does_not_enter_main_loop(self):
        proc, calls = self.run_fn("echo sourced-ok", [])
        self.assertIn("sourced-ok", proc.stdout)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
