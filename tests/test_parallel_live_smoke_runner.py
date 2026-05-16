from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_parallel_live_smoke as runner


def test_discover_bot_pairs_reads_primary_and_parallel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "OBS_TEST_TELEGRAM_BOT_USERNAME": "primarybot",
        "OBS_TEST_TELEGRAM_BOT_TOKEN": "primary-token",
        "OBS_TEST_TELEGRAM_BOT_USERNAMES": "secondbot,thirdbot",
        "OBS_TEST_TELEGRAM_BOT_TOKENS": "second-token,third-token",
    }

    pairs = runner.discover_bot_pairs(env)

    assert [(pair.username, pair.token) for pair in pairs] == [
        ("primarybot", "primary-token"),
        ("secondbot", "second-token"),
        ("thirdbot", "third-token"),
    ]


def test_build_worker_env_forces_safe_live_settings() -> None:
    env = runner.build_worker_env(
        {"OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS": "1", "OTHER": "ok"},
        runner.BotPair(username="bot", token="token"),
        3,
    )

    assert env["OBS_AGENT_MODEL"] == "haiku"
    assert env["OBS_TEST_TELEGRAM_ISOLATED_RESOURCES"] == "1"
    assert env["OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS"] == "0"
    assert env["OBS_CACHE_PROXY_ENABLED"] == "0"
    assert env["OBS_TEST_TELEGRAM_BOT_USERNAME"] == "bot"
    assert env["OBS_TEST_TELEGRAM_BOT_TOKEN"] == "token"
    assert env["OBS_TEST_TELEGRAM_BOT_TOKENS"] == "token"
    assert env["OBS_LIVE_PARALLEL_WORKER_INDEX"] == "3"


def test_dry_run_prints_two_default_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_USERNAME", "primarybot")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_TOKEN", "primary-token")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_USERNAMES", "secondbot")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_TOKENS", "second-token")

    rc = runner.main(["--dry-run", "--output-dir", str(tmp_path), "--max-workers", "2"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "test_live_multi_chat_concurrent_isolation" in out
    assert "test_live_idle_agent_wakes_on_direct_message_without_sleep" in out
    assert out.count(" -m pytest ") == 2 or out.count("-m pytest") == 2


def test_shared_bot_requires_explicit_diagnostic_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_USERNAME", "primarybot")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_TOKEN", "primary-token")
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_USERNAMES", raising=False)
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_TOKENS", raising=False)

    with pytest.raises(SystemExit, match="one bot username/token pair per worker"):
        runner.main(["--dry-run", "--output-dir", str(tmp_path), "--max-workers", "2"])


def test_parallel_runner_summary_records_overlap_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_USERNAME", "primarybot")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_TOKEN", "primary-token")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_USERNAMES", "secondbot")
    monkeypatch.setenv("OBS_TEST_TELEGRAM_BOT_TOKENS", "second-token")

    class FakePopen:
        calls = 0

        def __init__(self, command, cwd, env, stdout, stderr, text):
            self.command = command
            self.cwd = cwd
            self.env = env
            self.stdout = stdout
            self.stderr = stderr
            self.text = text
            self.returncode = 0
            self.started = FakePopen.calls
            FakePopen.calls += 1

        def wait(self, timeout=None):
            worker_root = Path(self.command[-1]).parent
            metadata_dir = worker_root / "pytest-tmp" / "case" / "obs-agent-temp"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            (metadata_dir / "live-forum-resources.json").write_text(
                json.dumps(
                    {
                        "isolated": True,
                        "run_id": f"live-{self.started}",
                        "chat_id": -1000 - self.started,
                        "vault_path": str(worker_root / "vault"),
                        "state_db_path": str(worker_root / "state.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    times = iter([100.0, 101.0, 102.0, 103.0, 200.0, 104.0, 201.0])
    monkeypatch.setattr(runner.time, "time", lambda: next(times))
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    rc = runner.main(["--output-dir", str(tmp_path), "--max-workers", "2"])

    summary = json.loads((tmp_path / "parallel-live-summary.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert summary["worker_count"] == 2
    assert summary["workers_overlapped"] is True
    assert summary["dedicated_bot_pairs"] is True
    assert summary["safe_env"] == runner.SAFE_LIVE_ENV
    assert len(summary["results"]) == 2
    assert all(result["resource_metadata"] for result in summary["results"])


def test_docs_register_live_suite_map() -> None:
    docs = Path(__file__).resolve().parents[1] / "docs" / "live-test-suite.md"
    text = docs.read_text(encoding="utf-8")

    assert "telegram_core_smoke" in text
    assert "telegram_focused" in text
    assert "telegram_special" in text
    assert "scripts/run_parallel_live_smoke.py" in text
    assert "relevant existing live tests" in text or "relevant existing live smoke" in text
    assert "bespoke reproducers" in text
