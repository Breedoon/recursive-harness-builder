# Testing Philosophy (Historical + Current Pointers)

> This file is retained for context, but the live policy is in `CLAUDE.md`.
> If this file and `CLAUDE.md` disagree, follow `CLAUDE.md`.

## Current Truth

1. **Evals are the primary proof** of system correctness.
2. **Unit/integration are necessary but insufficient**.
3. **Judge output must be read, not just verdicts**.
4. **Pass-with-concerns must be surfaced** via scenario intent and judge notes.

## Where to Look

- Live testing rules: `CLAUDE.md` -> "Evaluations (Evals)"
- Eval harness: `tests/evals/test_evals.py`
- Judge prompts/logic: `tests/evals/judge.py`
- Scenario parser: `tests/evals/scenario.py`
- Scenario definitions: `tests/evals/scenarios/*.md`
- Telegram eval platform: `tests/evals/platform_telegram.py`
- Eval guardian policy: `.claude/agents/eval-guardian.md`

## Historical Notes (Why This Exists)

This project repeatedly learned that mocked confidence is not reliability:

- Mock-heavy suites passed while real user flows still broke.
- Queue/interrupt/streaming edge cases appeared only in real interaction loops.
- Telegram behavior required real Telegram runs (not synthetic stubs).

The modern eval stack exists to prevent those regressions.

## Practical Rule of Thumb

If a human operator would notice a failure in a live run, there should be an eval
that can expose it, and the judge output should call out suspicious behavior even
when criteria technically pass.
