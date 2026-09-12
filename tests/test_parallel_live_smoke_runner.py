"""Authored deprecation tests for the historical live-smoke launcher; unrun."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_parallel_live_smoke as runner


ROOT = Path(__file__).resolve().parents[1]


def test_public_parallel_live_entrypoint_always_fails_closed(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "not-created"
    assert "subprocess" not in runner.__dict__
    with pytest.raises(SystemExit, match="canonical host-preflight"):
        runner.main(
            [
                "--dry-run",
                "--output-dir",
                str(output_dir),
                "--allow-shared-bot",
            ]
        )
    assert not output_dir.exists()


def test_parallel_live_redirect_does_not_read_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "discover_bot_pairs",
        lambda *_args, **_kwargs: pytest.fail(
            "credential discovery must not run"
        ),
    )
    with pytest.raises(SystemExit, match="obs-live-test"):
        runner.main([])


def test_pure_nonlaunching_helpers_remain_library_compatible() -> None:
    pairs = runner.discover_bot_pairs(
        {
            "OBS_TEST_TELEGRAM_BOT_USERNAME": "unit-bot",
            "OBS_TEST_TELEGRAM_BOT_TOKEN": "unit-token",
        }
    )
    assert pairs == [runner.BotPair(username="unit-bot", token="unit-token")]
    assert runner._workers_overlapped(
        [
            {"started_at": 1.0, "finished_at": 3.0},
            {"started_at": 2.0, "finished_at": 4.0},
        ]
    ) is True


def test_docs_do_not_register_legacy_script_as_formal_runner() -> None:
    docs = ROOT / "docs" / "live-test-suite.md"
    text = docs.read_text(encoding="utf-8")
    assert "scripts/run_parallel_live_smoke.py" in text
    assert "disabled" in text or "deprecated" in text
    assert "scripts.isolated_test_runner" in text


def test_historical_direct_live_consumers_are_explicitly_deprecated() -> None:
    paths = (
        "tests/test_telegram_live_forking.py",
        "tests/test_telegram_live_media.py",
        "tests/test_telegram_live_forum_topics.py",
        "tests/test_telegram_live_smoke.py",
        "tests/test_telegram_live_schedule.py",
        "tests/test_telegram_live_naming_redesign.py",
        "tests/test_integration_audit.py",
        "tests/test_telegram_live_stress.py",
        "tests/test_bug_reproductions.py",
        "tests/test_telegram_live_agenttask_features.py",
        "tests/test_telegram_live_provisioning.py",
        "tests/test_telegram_live_schedule_soak.py",
    )
    for relative in paths:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.skip" in source
        assert "migrate through scripts.isolated_test_runner" in source

    mixed_source = (ROOT / "tests" / "test_reply_bugs.py").read_text(
        encoding="utf-8"
    )
    live_class = mixed_source.index("class TestLiveNeedsReplyBehavior")
    decorator_block = mixed_source[live_class - 300 : live_class]
    assert "@pytest.mark.skip" in decorator_block
    assert "migrate through scripts.isolated_test_runner" in decorator_block
