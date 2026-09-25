"""Translate an OBS context budget into Claude Code's process-level controls.

The OBS suffix is metadata, not a Claude model capability declaration. Claude
Code recognizes a 200K/1M selector, so a 400K budget needs a 1M selector *and* an
explicit compaction percentage. Keeping those quantities separate is essential for
child inheritance and context reporting. See docs/context-compaction.md for the
reference thresholds, version assumptions, and the executable compatibility test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


STANDARD_CONTEXT_TOKENS = 200_000
EXTENDED_CONTEXT_TOKENS = 1_000_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 20_000
COMPACTION_BUFFER_TOKENS = 13_000
# A 35K budget leaves a 2K target: at least 1% of a 200K CLI window.
MIN_OBS_CONTEXT_TOKENS = 35_000


class ContextBudgetError(ValueError):
    """The requested budget cannot be represented safely by the CLI controls."""


def default_compaction_threshold(context_tokens: int) -> int:
    """Return the reference target T(C) = C - 33,000, floored at zero.

    The reference points are (200,000, 167,000) and (1,000,000, 967,000).
    This is linear in token counts, not in percentages. The corresponding
    headroom is 20K output reserve plus a 13K compaction buffer. This function
    describes OBS's target, not proof of a particular installed CLI's behavior.
    """
    return max(0, context_tokens - DEFAULT_OUTPUT_RESERVE_TOKENS - COMPACTION_BUFFER_TOKENS)


def _require_integer(value: int, *, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextBudgetError(f"{name} must be an integer token count")
    if not minimum <= value <= maximum:
        raise ContextBudgetError(f"{name} must be between {minimum:,} and {maximum:,} tokens")


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


COMPACTION_DISABLE_KEYS = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT")
# Keys the context plan writes. An explicit per-session value (AgentTask ``env``)
# for any of them wins over the plan value at both the process and settings layer.
CONTEXT_PLAN_ENV_KEYS = (
    "OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
)


def explicit_compaction_disabled(explicit_env: Mapping[str, str]) -> bool:
    """Return whether a per-session override explicitly disables auto-compaction."""
    return any(_enabled(explicit_env.get(name)) for name in COMPACTION_DISABLE_KEYS)


def validation_environment(
    process_env: Mapping[str, str],
    effective_env: Mapping[str, str],
    explicit_env: Mapping[str, str],
) -> dict[str, str]:
    """Environment used to validate a plan, excluding explicit per-session choices.

    ``_validate_environment`` exists to stop OBS from silently reversing a stray
    daemon-wide compaction kill switch. A disable switch set explicitly for one
    session (AgentTask ``env``) is a deliberate choice, not a stray value, and is
    honoured by ``apply_explicit_context_overrides`` instead of rejected. An
    explicit value (even "0") also shadows the daemon-wide value for that key.
    """
    merged = {**process_env, **effective_env}
    for name in COMPACTION_DISABLE_KEYS:
        if name in explicit_env:
            merged.pop(name, None)
    return merged


def apply_explicit_context_overrides(
    context_env: Mapping[str, str],
    explicit_env: Mapping[str, str],
    *,
    auto_compact_disabled: bool,
) -> dict[str, str]:
    """Return the context env with explicit per-session values taking precedence.

    The plan's selector/percentage stay in place (so the CLI's window and
    context percentage remain correct) unless the session explicitly overrides
    a key. When auto-compaction is disabled for the session, ``DISABLE_AUTO_COMPACT=1``
    is added so it is present at both the SDK env and the inline-settings layer.
    No hard token limit is introduced.
    """
    resolved = dict(context_env)
    for name in CONTEXT_PLAN_ENV_KEYS:
        value = explicit_env.get(name)
        if value is not None and str(value).strip():
            resolved[name] = str(value)
    if auto_compact_disabled:
        resolved["DISABLE_AUTO_COMPACT"] = "1"
    return resolved


def _validate_environment(environ: Mapping[str, str], cli_capacity_tokens: int) -> None:
    """Do not silently override an operator's explicit compaction kill switch."""
    for name in (
        *COMPACTION_DISABLE_KEYS,
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
    ):
        if _enabled(environ.get(name)):
            raise ContextBudgetError(
                f"{name} disables automatic compaction; unset it before using an OBS context budget"
            )
    if cli_capacity_tokens > STANDARD_CONTEXT_TOKENS and _enabled(
        environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")
    ):
        raise ContextBudgetError(
            "CLAUDE_CODE_DISABLE_1M_CONTEXT conflicts with a budget above 200K; "
            "unset it or select a context budget no greater than 200K"
        )
    declared_window = (environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or "").strip()
    if declared_window and declared_window != str(cli_capacity_tokens):
        raise ContextBudgetError(
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS conflicts with the CLI capacity selector; "
            "unset it and configure the model suffix instead"
        )


def _output_reserve(environ: Mapping[str, str]) -> int:
    """Account for an explicit smaller output cap without changing that cap.

    The reference CLI reserves min(max_output_tokens, 20K). All of OBS's default
    model families have at least a 20K output allowance. For unusual providers or
    changed CLI versions, run the binary compatibility test before deployment.
    """
    value = (environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or "").strip()
    if not value:
        return DEFAULT_OUTPUT_RESERVE_TOKENS
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ContextBudgetError("CLAUDE_CODE_MAX_OUTPUT_TOKENS must be a positive decimal integer")
    return min(int(value), DEFAULT_OUTPUT_RESERVE_TOKENS)


@dataclass(frozen=True)
class ClaudeContextPlan:
    """One session's immutable budget and its distinct CLI representation."""

    cli_model: str
    context_tokens: int
    compact_window_tokens: int
    cli_compact_window_tokens: int
    threshold_tokens: int
    output_reserve_tokens: int

    @property
    def environment(self) -> dict[str, str]:
        """Return explicit child-process values, including a percentage guard.

        A stale percentage inherited from the parent process cannot be removed
        by popping a key from ClaudeAgentOptions.env: the SDK merges os.environ
        back in. Always supply a fresh value. The percentage uses the CLI's
        *effective native capacity*, after its output reserve. Claude 2.1.59
        ignores AUTO_COMPACT_WINDOW, so using the requested smaller window as
        the denominator would silently delay compaction (400K -> about 946K).
        Set that variable to the native capacity too, so old releases that
        ignore it and newer releases that honor it use the same denominator.

        A half-token bias stays inside the target token's floor interval. It
        avoids accidentally compacting one token early due to decimal-to-binary
        floating point conversion in JavaScript. The CLI's own safety threshold
        remains an upper bound; this override never disables native compaction.
        """
        effective_window = self.cli_compact_window_tokens - self.output_reserve_tokens
        percentage = (
            (Decimal(self.threshold_tokens) + Decimal("0.5")) * 100 / effective_window
        ).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        return {
            "OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS": str(self.context_tokens),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.cli_compact_window_tokens),
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": format(percentage, "f"),
        }


def build_claude_context_plan(
    *,
    model: str,
    context_tokens: int,
    auto_compact_window_tokens: int = 0,
    environ: Mapping[str, str] | None = None,
) -> ClaudeContextPlan:
    """Build the CLI selector and controls without changing OBS model metadata.

    ``model`` must already be resolved and stripped of the OBS suffix. Requested
    windows from 35K through 1M are supported. The percentage supplies the
    requested earlier target within a recognized 200K/1M capacity. Smaller
    budgets cannot retain the 33K reserve and a >=1% override. An optional cap
    on a 1M selector must also retain that minimum percentage (about 43K).
    Unsupported values fail explicitly, never fall back.

    The optional OBS cap can only make compaction earlier. Raw Claude window and
    percentage overrides are superseded by this per-session policy. Disable
    switches are rejected instead of being silently reversed.
    """
    _require_integer(
        context_tokens, name="context window",
        minimum=MIN_OBS_CONTEXT_TOKENS, maximum=EXTENDED_CONTEXT_TOKENS,
    )
    _require_integer(
        auto_compact_window_tokens, name="auto-compact cap",
        minimum=0, maximum=EXTENDED_CONTEXT_TOKENS,
    )
    if not model.strip() or "[" in model or "]" in model:
        raise ContextBudgetError("CLI context planning requires a clean, nonempty model identity")
    cli_capacity = (
        EXTENDED_CONTEXT_TOKENS
        if context_tokens > STANDARD_CONTEXT_TOKENS
        else STANDARD_CONTEXT_TOKENS
    )
    environment = environ if environ is not None else {}
    _validate_environment(environment, cli_capacity)
    output_reserve = _output_reserve(environment)

    compact_window = context_tokens
    if auto_compact_window_tokens:
        _require_integer(
            auto_compact_window_tokens, name="auto-compact cap",
            minimum=MIN_OBS_CONTEXT_TOKENS, maximum=EXTENDED_CONTEXT_TOKENS,
        )
        compact_window = min(context_tokens, auto_compact_window_tokens)

    # Keep the percentage in the documented 1..100 range even when an operator
    # asks for an unusually small cap on a session using the 1M selector.
    threshold = default_compaction_threshold(compact_window)
    effective_capacity = cli_capacity - output_reserve
    if threshold * 100 < effective_capacity:
        minimum_cap = 33_000 + (effective_capacity + 99) // 100
        raise ContextBudgetError(
            "auto-compact cap must retain at least a 1% CLI threshold; "
            f"use {minimum_cap:,} tokens or more for this capacity selector"
        )

    # The selector establishes capacity, not the provider's actual limit.
    # The percentage alone carries the requested budget. Do not set the CLI
    # window to that smaller budget: version 2.1.59 ignores it, whereas newer
    # versions honor it, producing different percentage denominators.
    selector = "[1m]" if context_tokens > STANDARD_CONTEXT_TOKENS else "[200k]"
    return ClaudeContextPlan(
        cli_model=model.strip() + selector,
        context_tokens=context_tokens,
        compact_window_tokens=compact_window,
        cli_compact_window_tokens=cli_capacity,
        threshold_tokens=threshold,
        output_reserve_tokens=output_reserve,
    )
