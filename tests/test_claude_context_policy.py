"""Reference-math and validation tests, independent of the installed SDK."""

from dataclasses import FrozenInstanceError
import math

import pytest

from obs_agent.claude_context import ContextBudgetError, build_claude_context_plan


def reference_cli_threshold(model: str, environment: dict[str, str], output_cap=32_000) -> int:
    """Independent transcription of the documented/observed reference contract.

    This is a unit-test oracle, NOT execution of the Claude binary. The separate
    binary test checks that the installed release still implements this contract.
    """
    capacity = 1_000_000 if "[1m]" in model.lower() else 200_000
    window = min(capacity, max(100_000, int(environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])))
    effective_window = window - min(output_cap, 20_000)
    default_threshold = effective_window - 13_000
    percentage = float(environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"])
    return min(default_threshold, math.floor(effective_window * (percentage / 100)))


@pytest.mark.parametrize("window", [34_000, 64_000, 99_000, 100_000, 128_000, 200_000,
                                    201_000, 333_000, 400_000, 750_000, 999_000, 1_000_000])
@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-5.6-sol", "gemini-2.5-pro"])
def test_policy_reaches_linear_target_in_reference_cli(model, window):
    plan = build_claude_context_plan(model=model, context_tokens=window)
    selector = "[1m]" if window > 200_000 else "[200k]"
    assert plan.cli_model == model + selector
    assert plan.context_tokens == window
    assert plan.threshold_tokens == window - 33_000
    assert plan.environment["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == str(window)
    assert reference_cli_threshold(plan.cli_model, plan.environment) == window - 33_000
    assert 1 <= float(plan.environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]) <= 100


def test_every_supported_k_suffix_has_exact_integer_target():
    for thousands in range(34, 1001):
        window = thousands * 1000
        plan = build_claude_context_plan(model="gpt-5.6-sol", context_tokens=window)
        assert reference_cli_threshold(plan.cli_model, plan.environment) == window - 33_000


@pytest.mark.parametrize("output_cap", [1, 8_000, 16_000, 19_999, 20_000, 32_000, 64_000, 128_000])
@pytest.mark.parametrize("window", [34_000, 100_000, 400_000, 1_000_000])
def test_smaller_explicit_output_allowance_does_not_move_target_later(output_cap, window):
    original_env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_cap)}
    plan = build_claude_context_plan(model="gpt-5.6-sol", context_tokens=window, environ=original_env)
    assert reference_cli_threshold(plan.cli_model, plan.environment, output_cap) == window - 33_000
    assert original_env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_cap)}
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in plan.environment


@pytest.mark.parametrize("cap, expected", [(0, 367_000), (34_000, 1_000), (100_000, 67_000),
                                           (150_000, 117_000), (400_000, 367_000), (800_000, 367_000)])
def test_operator_cap_can_only_advance_compaction(cap, expected):
    plan = build_claude_context_plan(
        model="gpt-5.6-sol", context_tokens=400_000, auto_compact_window_tokens=cap,
    )
    assert plan.context_tokens == 400_000
    assert plan.cli_model == "gpt-5.6-sol[1m]"
    assert plan.threshold_tokens == expected
    assert reference_cli_threshold(plan.cli_model, plan.environment) == expected


@pytest.mark.parametrize("window", [True, 400_000.5, "400000", 0, -1, 33_000, 1_001_000, 2_000_000])
def test_unrepresentable_budget_fails_instead_of_silently_falling_back(window):
    with pytest.raises(ContextBudgetError):
        build_claude_context_plan(model="gpt-5.6-sol", context_tokens=window)


@pytest.mark.parametrize("cap", [True, 1.5, -1, 1, 33_000, 1_000_001])
def test_invalid_operator_cap_is_not_ignored(cap):
    with pytest.raises(ContextBudgetError):
        build_claude_context_plan(model="gpt-5.6-sol", context_tokens=400_000, auto_compact_window_tokens=cap)


@pytest.mark.parametrize("key", ["DISABLE_AUTO_COMPACT", "DISABLE_COMPACT",
                                  "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
                                  "CLAUDE_CODE_DISABLE_1M_CONTEXT"])
@pytest.mark.parametrize("value", ["1", "true", " YES ", "on"])
def test_conflicting_disable_switch_is_reported_not_reversed(key, value):
    with pytest.raises(ContextBudgetError, match=key):
        build_claude_context_plan(model="gpt-5.6-sol", context_tokens=400_000, environ={key: value})


def test_disabled_extended_context_allows_a_small_budget():
    plan = build_claude_context_plan(
        model="claude-opus-4-6", context_tokens=100_000,
        environ={"CLAUDE_CODE_DISABLE_1M_CONTEXT": "true"},
    )
    assert plan.cli_model.endswith("[200k]")


@pytest.mark.parametrize("value", ["0", "-1", "no", "NaN", "Infinity", "12.5", "٣٢٠٠٠"])
def test_invalid_output_override_is_rejected(value):
    with pytest.raises(ContextBudgetError, match="CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
        build_claude_context_plan(model="gpt", context_tokens=400_000,
                                 environ={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": value})


def test_raw_context_override_cannot_silently_force_200k():
    with pytest.raises(ContextBudgetError, match="CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        build_claude_context_plan(model="gpt", context_tokens=400_000,
                                 environ={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000"})


def test_plan_is_immutable_and_environment_results_are_independent():
    plan = build_claude_context_plan(model="gpt", context_tokens=400_000)
    with pytest.raises(FrozenInstanceError):
        plan.context_tokens = 1_000_000
    environment = plan.environment
    environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "200000"
    assert plan.environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"


def test_raw_stale_controls_are_replaced_without_mutating_the_caller():
    inherited = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "10",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
    }
    plan = build_claude_context_plan(model="gpt", context_tokens=400_000, environ=inherited)
    assert reference_cli_threshold(plan.cli_model, {**inherited, **plan.environment}) == 367_000
    assert inherited["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "10"
