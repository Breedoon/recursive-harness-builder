# Evals Program Status

Date: 2026-02-26
Owner: Runtime/evals maintainers

This document is the detailed source of truth for the eval overhaul status.
`CLAUDE.md` is the policy entry point; this file tracks current behavior,
pending work, and open questions.

## Current State

- Eval lanes:
  - `deterministic`: explicit assertions, no LLM judge
  - `judge`: transcript-level behavioral evaluation
- Profiles:
  - `smoke`: fast default subset
  - `feature`: feature-loop subset
  - `full`: complete current suite
- CLI eval status:
  - CLI evals are now opt-in and disabled by default.
  - Enable via `OBS_EVAL_ENABLE_CLI=1`.
- Telegram eval status:
  - Telegram evals remain the default runtime eval path.
  - `(done)` handling is per turn; queued multi-turn interactions can legitimately
    produce multiple `(done)` markers (one for each completed turn).

## Tier System (Execution Policy)

Tier 0: Deterministic + fast sanity
- Goal: catch obvious regressions on every meaningful change.
- Default command:
  - `OBS_EVAL_PROFILE=smoke .venv/bin/pytest tests/evals/ -v -m eval --timeout=300`
- Typical contents: deterministic transport/plumbing checks + minimal judge coverage.

Tier 1: Feature evals
- Goal: validate behavior of the subsystem/feature being edited during iteration.
- Current command (manual selection while subsystem routing is pending):
  - `OBS_EVAL_PROFILE=feature .venv/bin/pytest tests/evals/ -v -m eval --timeout=300`
  - Optional Telegram subset: `OBS_TG_SCENARIOS=<comma_list> ...test_eval_telegram_all ...`
- Use when: adding/changing behavior, during implementation loop.

Tier 2: Master eval (PENDING, highest priority)
- Goal: one holistic, adversarial, end-to-end scenario that represents "real use"
  and acts as the primary pre-ship integration signal.
- Current temporary fallback:
  - `OBS_EVAL_PROFILE=full .venv/bin/pytest tests/evals/ -v -m eval --timeout=1800`
- Status: not yet implemented as a dedicated master scenario.

## Feature Evals vs Master Eval

Feature evals:
- Narrower, scoped to the feature/subsystem under active development.
- Used repeatedly during implementation to debug quickly.

Master eval:
- Broad, realistic multi-step workflow with context awareness, queueing,
  chronological integrity, and resilience expectations in one flow.
- Used as the final confidence check before shipping non-trivial changes.

Current gap:
- We have feature-style and stress scenarios, but we do not yet have a single
  canonical "master eval scenario" with a stable contract.

## Open Questions (Preserved)

Priority P0:
- Define and implement the dedicated Tier 2 master eval scenario:
  - exact user journey and acceptance criteria
  - expected runtime budget
  - required deterministic vs judge checks inside the master flow

Priority P1:
- Implement true feature targeting (`OBS_EVAL_SUBSYSTEM` or equivalent) so
  `feature` is materially different from `full`.
- Reduce runtime of long Telegram stress cases without losing failure detection
  quality (especially scenarios with 120-300s waits).

Priority P2:
- Finalize CLI deprecation path:
  - which CLI scenarios remain mandatory for regression history
  - when CLI evals can be fully retired vs kept as explicit opt-in checks

## Immediate Next Steps

1. Draft the Tier 2 master eval scenario contract (intent, steps, criteria).
2. Implement subsystem tags/filtering for Tier 1 (`feature`) runs.
3. Rebalance long-running stress budgets after master eval lands.
