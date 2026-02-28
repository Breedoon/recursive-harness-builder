"""Meta-tests to keep evals meaningful and falsifiable."""

from __future__ import annotations

from pathlib import Path

from tests.evals.deterministic import _validate_scenario
from tests.evals.scenario import parse_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"
EXPECTED_JUDGE_SCENARIOS = {
    "background_fork",
    "context_usage_stress",
    "context_awareness",
    "tg_chronological_output",
    "tg_large_output_resilience",
    "tg_stress_chronology",
    "tg_transport_desync_on_send_error",
}


def _scenario_ids() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.md"))


def test_judge_suite_is_small_and_intentional() -> None:
    judge = {
        sid
        for sid in _scenario_ids()
        if (parse_scenario(SCENARIO_DIR / f"{sid}.md").lane or "").lower() == "judge"
    }
    assert judge == EXPECTED_JUDGE_SCENARIOS
    assert len(judge) <= 7


def test_scenario_quality_floor() -> None:
    for sid in _scenario_ids():
        path = SCENARIO_DIR / f"{sid}.md"
        raw = path.read_text()
        scenario = parse_scenario(path)

        assert scenario.steps, f"{sid}: scenario has no steps"
        assert len(scenario.criteria) >= 2, f"{sid}: scenario has too few criteria"
        assert all(len(c.strip()) >= 20 for c in scenario.criteria), (
            f"{sid}: criteria too short/trivial"
        )

        lane = (scenario.lane or "").lower()
        if lane == "judge":
            lowered = raw.lower()
            assert "## intent" in lowered, f"{sid}: judge scenario missing Intent section"
            assert "broken" in lowered, f"{sid}: judge scenario missing broken-behavior examples"
            assert (
                "suspicious" in lowered or "anomal" in lowered
            ), f"{sid}: judge scenario missing suspicious/anomaly guidance"


def test_deterministic_mutation_spot_checks() -> None:
    cases = [
        (
            "basic_chat",
            ["traceback: boom"],
            "",
            "error markers",
        ),
        (
            "session_continuity",
            ["ok", "I forgot the word"],
            "",
            "PINEAPPLE",
        ),
        (
            "tg_auth_guard",
            ["hello", "London"],
            "",
            "Paris",
        ),
        (
            "tg_transport_desync_forced_concurrency",
            ["FORCED_STRESS_DONE\ncontext: 30k / 200k", "Read: stale backlog then pong\ncontext: 31k / 200k"],
            "",
            "contaminated",
        ),
        (
            "tg_message_split",
            ["tiny answer\ncontext: 10k / 200k"],
            "",
            "too short",
        ),
    ]

    for sid, outputs, transcript, expected_detail in cases:
        passed, details = _validate_scenario(sid, outputs, transcript)
        assert not passed, f"{sid}: mutation unexpectedly passed"
        assert expected_detail.lower() in details.lower(), (
            f"{sid}: expected detail hint '{expected_detail}', got '{details}'"
        )
