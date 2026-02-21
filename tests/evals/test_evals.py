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
import tempfile
import time
from pathlib import Path

import pytest

from tests.evals.judge import run_judge
from tests.evals.platform import CLIPlatform
from tests.evals.scenario import parse_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"

# Seconds to wait between Telegram scenarios to drain stale bot responses.
# Must be generous — large output scenarios can leave the bot busy for a while.
_INTER_SCENARIO_DRAIN_SECONDS = 10


def _all_scenario_ids() -> list[str]:
    """Discover all scenario files and return their stem names."""
    if not SCENARIO_DIR.is_dir():
        return []
    return [p.stem for p in sorted(SCENARIO_DIR.glob("*.md"))]


def cli_scenario_ids() -> list[str]:
    """Scenario IDs for CLI evals (exclude tg_* Telegram-specific ones)."""
    return [s for s in _all_scenario_ids() if not s.startswith("tg_")]


# Heavy-output scenarios that stress the bot/Telegram pipeline. These run LAST
# so they don't destabilize lighter scenarios that follow them.
_HEAVY_SCENARIOS = {"tg_large_output_resilience", "tg_message_split"}


def telegram_scenario_ids() -> list[str]:
    """Scenario IDs for Telegram evals (tg_* only).

    Heavy-output scenarios are moved to the end so their long generation
    times and many-chunk deliveries don't contaminate lighter scenarios.
    """
    scenarios = [s for s in _all_scenario_ids() if s.startswith("tg_")]
    raw_filter = os.environ.get("OBS_TG_SCENARIOS", "").strip()
    if raw_filter:
        wanted = {item.strip() for item in raw_filter.split(",") if item.strip()}
        scenarios = [s for s in scenarios if s in wanted]
    # Move heavy scenarios to the end, preserving relative order
    light = [s for s in scenarios if s not in _HEAVY_SCENARIOS]
    heavy = [s for s in scenarios if s in _HEAVY_SCENARIOS]
    return light + heavy


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


def _start_telegram_bot(vault_path: Path) -> tuple[subprocess.Popen, Path]:
    """Start the Telegram bot as a subprocess.

    Uses the test bot token and the given vault path.
    Returns (Popen handle, log file path) for cleanup.

    IMPORTANT: We redirect stderr to a temp file instead of using PIPE.
    With PIPE, the OS pipe buffer (~64KB on macOS) fills up during long eval
    runs. When the buffer is full, the bot subprocess blocks on every log
    write, causing a silent deadlock — no Telegram messages sent, no (done)
    sentinel, zero output from the bot's perspective. Writing to a file
    avoids this entirely.
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

    log_file = Path(tempfile.mktemp(prefix="obs_tg_bot_", suffix=".log"))
    log_fh = open(log_file, "w")

    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main"],
        env=env,
        stdout=log_fh,
        stderr=log_fh,
    )
    # Give the bot time to start polling and register handlers.
    # 3 seconds was insufficient — /new often timed out on the first attempt.
    time.sleep(5)
    if proc.poll() is not None:
        log_fh.close()
        log_text = log_file.read_text(errors="replace")
        raise RuntimeError(
            f"Telegram bot process exited immediately (rc={proc.returncode}).\n"
            f"log: {log_text[-2000:]}"
        )
    return proc, log_file


def _stop_telegram_bot(proc: subprocess.Popen, log_file: Path | None = None) -> None:
    """Gracefully stop the Telegram bot subprocess and print its logs."""
    if proc.poll() is not None:
        _print_bot_logs(log_file)
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    _print_bot_logs(log_file)


def _print_bot_logs(log_file: Path | None) -> None:
    """Print the bot's log file for debugging (last 5000 chars)."""
    if log_file is None or not log_file.exists():
        return
    try:
        text = log_file.read_text(errors="replace")
        if text:
            tail = text[-5000:] if len(text) > 5000 else text
            print(f"\n[telegram-eval] BOT LOG ({len(text)} chars, showing last {len(tail)}):")
            print(tail)
        # Clean up temp file
        log_file.unlink(missing_ok=True)
    except Exception:
        pass


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
    bot_proc, bot_log_file = _start_telegram_bot(eval_vault)
    try:
        failures: list[str] = []

        # Verify bot is responsive before running any scenarios.
        # Retry /new with exponential backoff until it responds.
        print("[telegram-eval] Waiting for bot to become responsive...")
        warmup_platform = TelegramPlatform()
        await warmup_platform.connect()
        bot_ready = False
        for attempt in range(5):
            reply = await warmup_platform.send_control("/new", timeout=10.0)
            if "session cleared" in reply.lower():
                bot_ready = True
                print(f"[telegram-eval] Bot ready after {attempt + 1} attempt(s)")
                break
            print(f"[telegram-eval] /new attempt {attempt + 1}: {reply!r}")
            await asyncio.sleep(3)
        await warmup_platform.close()
        if not bot_ready:
            print("[telegram-eval] WARNING: bot not responding to /new, proceeding anyway")

        for i, scenario_name in enumerate(scenarios):
            # Ensure bot process is still alive before each scenario
            if bot_proc.poll() is not None:
                log_text = bot_log_file.read_text(errors="replace") if bot_log_file.exists() else ""
                failures.append(
                    f"EVAL FAILED (telegram): {scenario_name}\n\n"
                    f"Bot process CRASHED before scenario started (rc={bot_proc.returncode}).\n"
                    f"log: {log_text[-500:]}"
                )
                print(f"[telegram-eval] FATAL: bot crashed before {scenario_name}")
                break

            print(f"[telegram-eval] start: {scenario_name}")
            scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
            scenario = parse_scenario(scenario_path)

            # Fresh platform per scenario to drain stale messages
            platform = TelegramPlatform()
            await platform.connect()
            try:
                # Reset the bot session before each scenario so runs are isolated.
                # /new now sets interrupt_flag=True before acquiring the chat lock,
                # so in-progress responses exit at the next tool boundary.
                # Retry up to 3 times to handle bot busy with previous scenario.
                reset_ok = False
                for _attempt in range(3):
                    reset_reply = await platform.send_control("/new", timeout=20.0)
                    if "session cleared" in reset_reply.lower():
                        reset_ok = True
                        break
                    print(f"[telegram-eval] /new retry for {scenario_name}: {reset_reply!r}")
                    await asyncio.sleep(5)
                print(f"[telegram-eval] /new {'OK' if reset_ok else 'TIMEOUT'} for {scenario_name}")

                # Rebaseline: update the platform's baseline message ID to the latest
                # message in the chat. This ensures any tutorial chunks still being
                # delivered from the previous scenario are ignored by the handler.
                await platform.rebaseline()

                # Extra drain: wait a moment for any late stale messages to arrive,
                # then flush the response queue so the scenario starts clean.
                await asyncio.sleep(2)
                drained = 0
                while not platform._response_queue.empty():
                    try:
                        platform._response_queue.get_nowait()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained:
                    print(f"[telegram-eval] drained {drained} stale message(s)")

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
        _stop_telegram_bot(bot_proc, bot_log_file)
