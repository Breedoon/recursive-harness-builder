"""Parametrized eval tests.

Each .md file in tests/evals/scenarios/ is a scenario. The judge agent
drives the real CLI via pexpect and evaluates responses against criteria.

CLI evals: run all scenarios EXCEPT tg_* prefixed (Telegram-specific).
Telegram evals: run tg_* scenarios sequentially in a single test function.

Run with:
    .venv/bin/pytest tests/evals/ -m eval -v                    # CLI only
    .venv/bin/pytest tests/evals/ -m "eval and telegram" -v     # Telegram only
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.evals.judge import run_judge
from tests.evals.platform import CLIPlatform
from tests.evals.scenario import parse_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"

# Seconds to wait between Telegram scenarios to drain stale bot responses
_INTER_SCENARIO_DRAIN_SECONDS = 5


def _all_scenario_ids() -> list[str]:
    """Discover all scenario files and return their stem names."""
    if not SCENARIO_DIR.is_dir():
        return []
    return [p.stem for p in sorted(SCENARIO_DIR.glob("*.md"))]


def cli_scenario_ids() -> list[str]:
    """Scenario IDs for CLI evals (exclude tg_* Telegram-specific ones)."""
    return [s for s in _all_scenario_ids() if not s.startswith("tg_")]


def telegram_scenario_ids() -> list[str]:
    """Scenario IDs for Telegram evals (tg_* only)."""
    scenarios = [s for s in _all_scenario_ids() if s.startswith("tg_")]
    raw_filter = os.environ.get("OBS_TG_SCENARIOS", "").strip()
    if not raw_filter:
        return scenarios
    wanted = {item.strip() for item in raw_filter.split(",") if item.strip()}
    return [s for s in scenarios if s in wanted]


# ---------------------------------------------------------------------------
# CLI evals
# ---------------------------------------------------------------------------

@pytest.mark.eval
@pytest.mark.parametrize("scenario_name", cli_scenario_ids() or ["_no_scenarios_found"])
async def test_eval(scenario_name: str, eval_vault: Path, eval_config) -> None:
    """Run a single eval scenario through the judge agent (CLI platform)."""
    if scenario_name == "_no_scenarios_found":
        pytest.skip("No scenario files found in tests/evals/scenarios/")

    scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
    scenario = parse_scenario(scenario_path)

    platform = CLIPlatform(
        vault_path=eval_vault,
        daemon_port=eval_config.daemon_port,
    )
    try:
        result = await run_judge(scenario, platform)
    finally:
        await platform.close()

    assert result.passed, f"EVAL FAILED: {scenario_name}\n\n{result.judgment}"


# ---------------------------------------------------------------------------
# Telegram evals
# ---------------------------------------------------------------------------

def _has_telegram_credentials() -> bool:
    """Check if all required Telegram env vars are set."""
    required = [
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_SESSION",
        "TELEGRAM_TEST_BOT_USERNAME",
        "OBS_TELEGRAM_TEST_BOT_TOKEN",
    ]
    return all(os.environ.get(k) for k in required)


_tg_creds_available = _has_telegram_credentials()


def _start_telegram_bot(vault_path: Path) -> subprocess.Popen:
    """Start the Telegram bot as a subprocess.

    Uses the test bot token and the given vault path.
    Returns the Popen handle for cleanup.
    """
    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    # Map the test bot token to what config.py reads
    env["OBS_TELEGRAM_BOT_TOKEN"] = os.environ["OBS_TELEGRAM_TEST_BOT_TOKEN"]
    # Set the allowed user to the Telethon test account so auth doesn't block evals.
    # TELEGRAM_TEST_USER_ID is the user ID of the secondary Telethon account that
    # sends messages during evals — NOT the primary account (OBS_TELEGRAM_AUTHORIZED_USER_ID).
    test_user_id = os.environ.get("TELEGRAM_TEST_USER_ID", "5129431382")
    env["OBS_TELEGRAM_ALLOWED_USERS"] = test_user_id

    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Give the bot time to start polling
    time.sleep(3)
    if proc.poll() is not None:
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"Telegram bot process exited immediately (rc={proc.returncode}).\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
    return proc


def _stop_telegram_bot(proc: subprocess.Popen) -> None:
    """Gracefully stop the Telegram bot subprocess."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.eval
@pytest.mark.telegram
@pytest.mark.timeout(1800)
@pytest.mark.skipif(
    not _tg_creds_available,
    reason="Telegram credentials not configured (need TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION, TELEGRAM_TEST_BOT_USERNAME, OBS_TELEGRAM_TEST_BOT_TOKEN)",
)
async def test_eval_telegram_all(eval_vault: Path) -> None:
    """Run ALL Telegram eval scenarios sequentially in a single test.

    Unlike CLI evals (parametrized), Telegram evals MUST be sequential because
    they share a single bot process. pytest-asyncio can run parametrized async
    tests concurrently, which causes scenarios to send messages simultaneously
    and pollute each other's responses. A single test function with an explicit
    loop guarantees one scenario completes before the next starts.

    Between scenarios, we drain stale bot messages and sleep briefly to ensure
    no late responses from the previous scenario bleed into the next one.
    """
    scenarios = telegram_scenario_ids()
    if not scenarios:
        pytest.skip("No tg_* scenario files found in tests/evals/scenarios/")

    from tests.evals.platform_telegram import TelegramPlatform

    # Start bot process for the duration of all scenarios
    bot_proc = _start_telegram_bot(eval_vault)
    try:
        failures: list[str] = []

        for i, scenario_name in enumerate(scenarios):
            print(f"[telegram-eval] start: {scenario_name}")
            scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
            scenario = parse_scenario(scenario_path)

            # Fresh platform per scenario to drain stale messages
            platform = TelegramPlatform()
            await platform.connect()
            try:
                # Reset the bot session before each scenario so runs are isolated.
                await platform.send_control("/new")
                result = await run_judge(scenario, platform)
            finally:
                await platform.close()

            print(f"[telegram-eval] judgment: {scenario_name}\n{result.judgment}\n")
            notes_line = next(
                (
                    line.strip()
                    for line in result.judgment.splitlines()
                    if line.strip().upper().startswith("NOTES:")
                ),
                "",
            )
            if notes_line and notes_line.lower() != "notes: none":
                print(f"[telegram-eval] caution: {scenario_name} -> {notes_line}")
            if not result.passed:
                failures.append(
                    f"EVAL FAILED (telegram): {scenario_name}\n\n{result.judgment}"
                )
                print(f"[telegram-eval] fail: {scenario_name}")
            else:
                print(f"[telegram-eval] pass: {scenario_name}")

            # Drain period between scenarios: sleep to let any late bot
            # responses arrive, then the next platform.connect() starts fresh
            if i < len(scenarios) - 1:
                await asyncio.sleep(_INTER_SCENARIO_DRAIN_SECONDS)

        if failures:
            combined = "\n\n" + ("\n" + "=" * 60 + "\n").join(failures)
            pytest.fail(combined)

    finally:
        _stop_telegram_bot(bot_proc)
