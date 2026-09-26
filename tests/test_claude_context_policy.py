"""Reference-math and validation tests, independent of the installed SDK."""

from dataclasses import FrozenInstanceError
import math

import pytest

from obs_agent.claude_context import ContextBudgetError, build_claude_context_plan

# The reference-curve tests below plan an OBS *budget* on a provider whose real
# window is 1M (hosted). Since vault-u3b.64 the target is also capped by that
# provider window minus the CLI's max_output and the 13K buffer.
HOSTED_WINDOW = 1_000_000


def expected_target(budget: int, *, provider: int = HOSTED_WINDOW, max_output: int = 32_000) -> int:
    return min(budget - 33_000, provider - max_output - 13_000)


def reference_cli_threshold(model: str, environment: dict[str, str], output_cap=32_000, *, honors_window=True) -> int:
    """Independent transcription of the documented/observed reference contract.

    This is a unit-test oracle, NOT execution of the Claude binary. The separate
    binary test checks that the installed release still implements this contract.
    """
    capacity = 1_000_000 if "[1m]" in model.lower() else 200_000
    # CI proved 2.1.59 ignores this variable; newer documented releases honor
    # it. The generated policy must give the same target under both contracts.
    window = capacity
    if honors_window:
        window = min(capacity, max(100_000, int(environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])))
    effective_window = window - min(output_cap, 20_000)
    default_threshold = effective_window - 13_000
    percentage = float(environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"])
    return min(default_threshold, math.floor(effective_window * (percentage / 100)))


@pytest.mark.parametrize("window", [35_000, 64_000, 99_000, 100_000, 128_000, 200_000,
                                    201_000, 333_000, 400_000, 750_000, 999_000, 1_000_000])
@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-5.6-sol", "gemini-2.5-pro"])
@pytest.mark.parametrize("honors_window", [False, True])
def test_policy_reaches_linear_target_in_reference_cli(model, window, honors_window):
    plan = build_claude_context_plan(model=model, context_tokens=window, provider_window_tokens=HOSTED_WINDOW)
    selector = "[1m]" if window > 200_000 else "[200k]"
    assert plan.cli_model == model + selector
    assert plan.context_tokens == window
    assert plan.threshold_tokens == expected_target(window)
    assert plan.environment["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == str(window)
    assert reference_cli_threshold(
        plan.cli_model, plan.environment, honors_window=honors_window
    ) == expected_target(window)
    assert 1 <= float(plan.environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]) <= 100


@pytest.mark.parametrize("honors_window", [False, True])
def test_every_supported_k_suffix_has_exact_integer_target(honors_window):
    for thousands in range(35, 1001):
        window = thousands * 1000
        plan = build_claude_context_plan(model="gpt-5.6-sol", context_tokens=window, provider_window_tokens=HOSTED_WINDOW)
        assert reference_cli_threshold(
            plan.cli_model, plan.environment, honors_window=honors_window
        ) == expected_target(window)


@pytest.mark.parametrize("output_cap", [1, 8_000, 16_000, 19_999, 20_000, 32_000, 64_000, 128_000])
@pytest.mark.parametrize("window", [35_000, 100_000, 400_000, 1_000_000])
@pytest.mark.parametrize("honors_window", [False, True])
def test_smaller_explicit_output_allowance_does_not_move_target_later(output_cap, window, honors_window):
    original_env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_cap)}
    plan = build_claude_context_plan(model="gpt-5.6-sol", context_tokens=window, environ=original_env,
                                      provider_window_tokens=HOSTED_WINDOW)
    assert reference_cli_threshold(plan.cli_model, plan.environment, output_cap, honors_window=honors_window) == expected_target(
        window, max_output=output_cap)
    assert expected_target(window, max_output=output_cap) <= window - 33_000
    assert original_env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_cap)}
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in plan.environment


@pytest.mark.parametrize("cap, expected", [(0, 367_000), (43_000, 10_000), (100_000, 67_000),
                                           (150_000, 117_000), (400_000, 367_000), (800_000, 367_000)])
def test_operator_cap_can_only_advance_compaction(cap, expected):
    plan = build_claude_context_plan(
        model="gpt-5.6-sol", context_tokens=400_000, auto_compact_window_tokens=cap,
        provider_window_tokens=HOSTED_WINDOW,
    )
    assert plan.context_tokens == 400_000
    assert plan.cli_model == "gpt-5.6-sol[1m]"
    assert plan.threshold_tokens == expected
    assert reference_cli_threshold(plan.cli_model, plan.environment) == expected


@pytest.mark.parametrize("window", [True, 400_000.5, "400000", 0, -1, 33_000, 34_000, 1_001_000, 2_000_000])
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
    assert plan.environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"


def test_raw_stale_controls_are_replaced_without_mutating_the_caller():
    inherited = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "10",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
    }
    plan = build_claude_context_plan(model="gpt", context_tokens=400_000, environ=inherited,
                                      provider_window_tokens=HOSTED_WINDOW)
    assert reference_cli_threshold(plan.cli_model, {**inherited, **plan.environment}) == 367_000
    assert inherited["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "10"


@pytest.mark.parametrize("cap", [35_000, 40_000, 42_799])
def test_small_cap_on_extended_selector_is_rejected_instead_of_ignored(cap):
    with pytest.raises(ContextBudgetError, match="1% CLI threshold"):
        build_claude_context_plan(
            model="gpt", context_tokens=400_000, auto_compact_window_tokens=cap,
        )


def test_matching_requested_max_context_still_conflicts_with_native_selector():
    with pytest.raises(ContextBudgetError, match="CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        build_claude_context_plan(model="gpt", context_tokens=400_000,
                                 environ={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "400000"})


def test_native_capacity_override_does_not_change_the_denominator():
    plan = build_claude_context_plan(model="gpt", context_tokens=400_000,
                                    environ={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"},
                                    provider_window_tokens=HOSTED_WINDOW)
    assert reference_cli_threshold(plan.cli_model, plan.environment, honors_window=False) == 367_000


# --- explicit per-session precedence (vault-u3b.13) ---


def test_validation_environment_drops_only_explicitly_chosen_disable_switches():
    from obs_agent.claude_context import validation_environment

    process = {"DISABLE_AUTO_COMPACT": "1", "DISABLE_COMPACT": "1", "OTHER": "x"}
    merged = validation_environment(process, {"A": "b"}, {"DISABLE_AUTO_COMPACT": "0"})
    assert "DISABLE_AUTO_COMPACT" not in merged
    assert merged["DISABLE_COMPACT"] == "1"  # not explicitly chosen -> still validated
    assert merged["OTHER"] == "x" and merged["A"] == "b"


def test_apply_explicit_context_overrides_precedence_and_disable():
    from obs_agent.claude_context import apply_explicit_context_overrides

    plan_env = {
        "OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS": "262000",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "23.4",
    }
    out = apply_explicit_context_overrides(
        plan_env, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "10", "UNRELATED": "1"}, auto_compact_disabled=False
    )
    assert out["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "10"
    assert out["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
    assert "UNRELATED" not in out and "DISABLE_AUTO_COMPACT" not in out
    disabled = apply_explicit_context_overrides(plan_env, {}, auto_compact_disabled=True)
    assert disabled["DISABLE_AUTO_COMPACT"] == "1"
    assert "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE" not in disabled


def test_explicit_compaction_disabled_accepts_both_switches():
    from obs_agent.claude_context import explicit_compaction_disabled

    assert explicit_compaction_disabled({"DISABLE_COMPACT": "true"})
    assert explicit_compaction_disabled({"DISABLE_AUTO_COMPACT": "1"})
    assert not explicit_compaction_disabled({"DISABLE_AUTO_COMPACT": "0"})
    assert not explicit_compaction_disabled({})


# --- provider input ceiling (vault-u3b.64) ---


@pytest.mark.parametrize("budget, provider, max_output, wall, expected", [
    # live defect: 262K local, CLI max_tokens 32K -> ceiling 230,000 (262,000-32,000)
    (262_000, 262_000, 32_000, False, 217_000),
    (262_000, 262_000, 32_000, True, 217_000),
    # a budget above the real window cannot move the target past the ceiling
    (500_000, 262_000, 32_000, False, 217_000),
    # a budget far below the provider window keeps the reference curve
    (120_000, 900_000, 32_000, False, 87_000),
    # ...and the handoff wall sits at the end of that budget
    (120_000, 900_000, 32_000, True, 107_000),
    (1_000_000, 1_000_000, 32_000, False, 955_000),
    (1_000_000, 1_000_000, 32_000, True, 955_000),
    # a smaller explicit max output never moves the target later than reference
    (262_000, 262_000, 8_000, False, 229_000),
])
def test_target_never_exceeds_provider_window_minus_max_output(budget, provider, max_output, wall, expected):
    from obs_agent.claude_context import COMPACTION_BUFFER_TOKENS, safe_compaction_threshold

    target = safe_compaction_threshold(
        budget, provider_window_tokens=provider, max_output_tokens=max_output, wall=wall,
    )
    assert target == expected
    assert target + max_output + COMPACTION_BUFFER_TOKENS <= provider


def test_plan_without_provider_window_treats_budget_as_the_real_window():
    plan = build_claude_context_plan(model="local-qwen3.8-27b", context_tokens=262_000)
    assert plan.threshold_tokens == 217_000
    assert plan.provider_window_tokens == 262_000
    assert plan.max_output_tokens == 32_000
    assert reference_cli_threshold(plan.cli_model, plan.environment) == 217_000


def test_wall_plan_is_clamped_to_the_cli_native_point():
    # 200K budget on a 900K provider: wall = 187K, but the CLI never compacts
    # past its own native 167K on the 200K selector.
    plan = build_claude_context_plan(
        model="gpt-6-luna", context_tokens=200_000, provider_window_tokens=900_000, wall=True,
    )
    assert plan.threshold_tokens == 167_000
    assert reference_cli_threshold(plan.cli_model, plan.environment) == 167_000
