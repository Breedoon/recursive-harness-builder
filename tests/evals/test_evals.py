"""Parametrized eval tests.

Each .md file in tests/evals/scenarios/ is a scenario.

CLI scenarios run in one of two lanes:
- deterministic lane: explicit assertions, no judge
- judge lane: SDK judge evaluates behavior against criteria

Telegram scenarios also route by lane:
- deterministic lane: explicit transcript assertions
- judge lane: SDK judge behavior checks

CLI evals: run all scenarios EXCEPT tg_* prefixed (Telegram-specific).
Telegram evals: run tg_* scenarios sequentially in a single test function.

Run with:
    .venv/bin/pytest tests/evals/ -m eval -v                    # CLI only
    .venv/bin/pytest tests/evals/ -m "eval and telegram" -v     # Telegram only
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.evals.deterministic import run_deterministic
from tests.evals.judge import run_judge
from tests.evals.platform import CLIPlatform
from tests.evals.scenario import parse_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"

# Seconds to wait between Telegram scenarios to drain stale bot responses.
# Kept configurable for flaky environments.
_INTER_SCENARIO_DRAIN_SECONDS = float(
    os.environ.get("OBS_TG_INTER_SCENARIO_DRAIN_SECONDS", "5")
)
_PROFILE_FILTER = os.environ.get("OBS_EVAL_PROFILE", "").strip().lower()
_LANE_FILTER = os.environ.get("OBS_EVAL_LANE", "").strip().lower()
_CLI_EVALS_ENABLED = os.environ.get("OBS_EVAL_ENABLE_CLI", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _redact_log_text(text: str) -> str:
    return re.sub(r"(https://api\.telegram\.org/bot)[^/\s\"]+", r"\1<redacted>", text)


def _all_scenario_ids() -> list[str]:
    """Discover all scenario files and return their stem names."""
    if not SCENARIO_DIR.is_dir():
        return []
    return [p.stem for p in sorted(SCENARIO_DIR.glob("*.md"))]


@lru_cache(maxsize=None)
def _load_scenario(scenario_id: str):
    """Parse and cache a scenario by ID."""
    return parse_scenario(SCENARIO_DIR / f"{scenario_id}.md")


def _matches_profile(scenario_id: str) -> bool:
    """Filter by OBS_EVAL_PROFILE if scenario metadata defines profiles."""
    if not _PROFILE_FILTER:
        return True
    scenario = _load_scenario(scenario_id)
    if not scenario.profiles:
        # When a profile is explicitly requested, untagged scenarios are excluded.
        return False
    return _PROFILE_FILTER in {p.lower() for p in scenario.profiles}


def _matches_lane(scenario_id: str) -> bool:
    """Filter by OBS_EVAL_LANE if set (`judge` or `deterministic`)."""
    if not _LANE_FILTER:
        return True
    scenario = _load_scenario(scenario_id)
    if not scenario.lane:
        return False
    return scenario.lane.lower() == _LANE_FILTER


def cli_scenario_ids() -> list[str]:
    """Scenario IDs for CLI evals (exclude tg_* Telegram-specific ones)."""
    if not _CLI_EVALS_ENABLED:
        return []
    return [
        s for s in _all_scenario_ids()
        if not s.startswith("tg_") and _matches_profile(s) and _matches_lane(s)
    ]


# Heavy-output scenarios that stress the bot/Telegram pipeline. These run LAST
# so they don't destabilize lighter scenarios that follow them.
_HEAVY_SCENARIOS = {"tg_large_output_resilience", "tg_message_split"}


def telegram_scenario_ids() -> list[str]:
    """Scenario IDs for Telegram evals (tg_* only).

    Heavy-output scenarios are moved to the end so their long generation
    times and many-chunk deliveries don't contaminate lighter scenarios.
    """
    scenarios = [
        s for s in _all_scenario_ids()
        if s.startswith("tg_") and _matches_profile(s) and _matches_lane(s)
    ]
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
    """Run a single CLI eval scenario via its configured lane."""
    if scenario_name == "_no_scenarios_found":
        if not _CLI_EVALS_ENABLED:
            pytest.skip("CLI evals disabled. Set OBS_EVAL_ENABLE_CLI=1 to run.")
        pytest.skip("No CLI scenario files found in tests/evals/scenarios/")

    scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
    scenario = parse_scenario(scenario_path)

    platform = CLIPlatform(
        vault_path=eval_vault,
        daemon_port=eval_config.daemon_port,
    )
    try:
        lane = (scenario.lane or "judge").lower()
        if lane == "deterministic":
            result = await run_deterministic(scenario_name, scenario, platform)
            assert result.passed, (
                f"EVAL FAILED: {scenario_name}\n\n"
                f"Deterministic details: {result.details}\n\n"
                f"Transcript:\n{result.transcript}"
            )
            return
        if lane == "judge":
            result = await run_judge(scenario, platform)
            assert result.passed, f"EVAL FAILED: {scenario_name}\n\n{result.judgment}"
            return
        pytest.fail(f"Unknown lane '{lane}' in {scenario_name}.md")
    finally:
        await platform.close()


# ---------------------------------------------------------------------------
# Telegram evals
# ---------------------------------------------------------------------------

def _has_telegram_credentials() -> bool:
    """Check if all required Telegram env vars are set."""
    required = [
        "OBS_TEST_TELEGRAM_API_ID",
        "OBS_TEST_TELEGRAM_API_HASH",
        "OBS_TEST_TELEGRAM_SESSION",
        "OBS_TEST_TELEGRAM_BOT_USERNAME",
        "OBS_TEST_TELEGRAM_BOT_TOKEN",
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
    write, causing a silent deadlock — no Telegram messages sent, no final completion summary
    sentinel, zero output from the bot's perspective. Writing to a file
    avoids this entirely.
    """
    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    runtime_root = vault_path / ".obs-eval-runtime"
    env["OBS_TELEGRAM_TEMP_ROOT"] = str(runtime_root / "telegram-temp")
    env["OBS_TELEGRAM_STATE_DB_PATH"] = str(runtime_root / "telegram-state.sqlite3")
    # Map the test bot token to what config.py reads
    env["OBS_TELEGRAM_BOT_TOKEN"] = os.environ["OBS_TEST_TELEGRAM_BOT_TOKEN"]
    env["OBS_TELEGRAM_BOT_TOKENS"] = os.environ["OBS_TEST_TELEGRAM_BOT_TOKEN"]
    # Set the allowed user to the Telethon test account so auth doesn't block evals.
    # OBS_TEST_TELEGRAM_ALLOWED_USERS should be the Telethon account used during evals.
    test_user_id = os.environ.get("OBS_TEST_TELEGRAM_ALLOWED_USERS", "5129431382")
    env["OBS_TELEGRAM_ALLOWED_USERS"] = test_user_id
    env["OBS_TELEGRAM_DROP_PENDING_UPDATES"] = "1"

    log_file = Path(tempfile.mktemp(prefix="obs_tg_bot_", suffix=".log"))
    log_fh = open(log_file, "w")

    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main", "--test"],
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
            f"log: {_redact_log_text(log_text[-2000:])}"
        )
    return proc, log_file


def _telegram_platform_kwargs(scenario) -> dict:
    """Build TelegramPlatform kwargs from optional scenario metadata."""
    kwargs: dict = {}
    if scenario.response_timeout is not None:
        kwargs["timeout"] = int(scenario.response_timeout)
    if scenario.first_message_timeout is not None:
        kwargs["first_message_timeout"] = scenario.first_message_timeout
    if scenario.done_timeout is not None:
        kwargs["done_timeout"] = scenario.done_timeout
    if scenario.idle_quiescence_timeout is not None:
        kwargs["idle_quiescence_timeout"] = scenario.idle_quiescence_timeout
    return kwargs


def _is_new_session_confirmation(text: str) -> bool:
    lowered = text.lower()
    return "session cleared" in lowered or "new trunk session created" in lowered


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
            tail = _redact_log_text(tail)
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
    reason="Telegram credentials not configured (need OBS_TEST_TELEGRAM_API_ID, OBS_TEST_TELEGRAM_API_HASH, OBS_TEST_TELEGRAM_SESSION, OBS_TEST_TELEGRAM_BOT_USERNAME, OBS_TEST_TELEGRAM_BOT_TOKEN)",
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
            if _is_new_session_confirmation(reply):
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
                    f"log: {_redact_log_text(log_text[-500:])}"
                )
                print(f"[telegram-eval] FATAL: bot crashed before {scenario_name}")
                break

            print(f"[telegram-eval] start: {scenario_name}")
            scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
            scenario = parse_scenario(scenario_path)

            # Fresh platform per scenario to drain stale messages
            platform = TelegramPlatform(**_telegram_platform_kwargs(scenario))
            await platform.connect()
            try:
                # Reset the bot session before each scenario so runs are isolated.
                # /new now sets interrupt_flag=True before acquiring the chat lock,
                # so in-progress responses exit at the next tool boundary.
                # Retry up to 3 times to handle bot busy with previous scenario.
                reset_ok = False
                for _attempt in range(3):
                    reset_reply = await platform.send_control("/new", timeout=20.0)
                    if _is_new_session_confirmation(reset_reply):
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

                lane = (scenario.lane or "judge").lower()
                if lane == "deterministic":
                    det = await run_deterministic(scenario_name, scenario, platform)
                    judgment_text = (
                        f"VERDICT: {'PASS' if det.passed else 'FAIL'}\n"
                        f"DETAILS: {det.details}"
                    )
                    passed = det.passed
                    transcript = det.transcript
                elif lane == "judge":
                    result = await run_judge(scenario, platform)
                    judgment_text = result.judgment
                    passed = result.passed
                    transcript = ""
                else:
                    raise RuntimeError(
                        f"Unknown lane '{lane}' in Telegram scenario {scenario_name}.md"
                    )
            finally:
                await platform.close()

            print(f"[telegram-eval] judgment: {scenario_name}\n{judgment_text}\n")
            notes_line = next(
                (
                    line.strip()
                    for line in judgment_text.splitlines()
                    if line.strip().upper().startswith("NOTES:")
                ),
                "",
            )
            if notes_line and notes_line.lower() != "notes: none":
                print(f"[telegram-eval] caution: {scenario_name} -> {notes_line}")
            if not passed:
                failure_details = f"EVAL FAILED (telegram): {scenario_name}\n\n{judgment_text}"
                if transcript:
                    failure_details += f"\n\nTranscript:\n{transcript}"
                failures.append(failure_details)
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
