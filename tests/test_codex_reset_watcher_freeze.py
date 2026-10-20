"""Freeze check in deploy/codex-reset-watcher.py (obs-15 V1 U1: restart callers without a check).

`act("stop"|"start")` runs `supervisorctl stop|start obs-telegram-prod`. It now asks the Agent
Operations freeze tool first and skips during an obs-restart/supervisor/all-shared freeze, fail-open.
The REAL vault freeze tool is used against a scratch state; subprocess.run for supervisorctl is
intercepted so nothing is executed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "codex-reset-watcher.py"
FREEZE_TOOL = Path(
    "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/"
    "Agent Operations/scripts/ops/freeze.py"
)


def load(tool: Path):
    with mock.patch.dict(os.environ, {"CODEX_RESET_FREEZE_TOOL": str(tool)}):
        spec = importlib.util.spec_from_file_location("codex_reset_watcher_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(FREEZE_TOOL.is_file(), "vault freeze tool unavailable")
class CodexResetWatcherFreeze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "freezes.json"
        self.real_run = subprocess.run
        self.executed = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, argv, *a, **k):
        if argv and argv[0] == "supervisorctl":
            self.executed.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        return self.real_run(argv, *a, **k)  # the freeze tool itself runs for real

    def act(self, action, freezes, env=None):
        self.state.write_text(json.dumps(freezes))
        mod = load(FREEZE_TOOL)
        with mock.patch.dict(os.environ, {"OPS_FREEZE_STATE": str(self.state), **(env or {})}), \
             mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run):
            return mod.act(action, "STOP")

    @staticmethod
    def fz(scope="obs-restart", id_="t1"):
        return {"id": id_, "scope": [scope], "declared_by": "test", "declared_at": "2026-10-20T00:00:00Z",
                "reason": "unit test", "until": None, "lifted_at": None}

    def test_stop_skipped_during_obs_restart_freeze(self):
        out = self.act("stop", [self.fz()])
        self.assertEqual(self.executed, [])
        self.assertIn("SKIPPED", out)
        self.assertIn("t1", out)

    def test_start_skipped_during_supervisor_freeze(self):
        self.act("start", [self.fz(scope="supervisor")])
        self.assertEqual(self.executed, [])

    def test_runs_without_freeze(self):
        out = self.act("stop", [])
        self.assertEqual(self.executed, [["supervisorctl", "stop", "obs-telegram-prod"]])
        self.assertIn("executed", out)

    def test_runs_with_ack(self):
        self.act("stop", [self.fz()], {"OPS_FREEZE_ACK": "t1"})
        self.assertEqual(len(self.executed), 1)

    def test_cliproxy_freeze_does_not_block_obs_stop(self):
        self.act("stop", [self.fz(scope="cliproxy")])
        self.assertEqual(len(self.executed), 1)

    def test_missing_tool_fails_open(self):
        mod = load(Path(self.tmp.name) / "missing.py")
        with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run):
            mod.act("stop", "STOP")
        self.assertEqual(len(self.executed), 1)

    def test_dry_run_never_consults_or_executes(self):
        out = self.act("dry-run", [self.fz()])
        self.assertIn("DRY-RUN", out)
        self.assertEqual(self.executed, [])


if __name__ == "__main__":
    unittest.main()
