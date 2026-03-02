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
  - Completion is detected via the final `context: used / window` summary.
  - Busy-time queued follow-ups are expected to collapse into one queued run,
    ending with a single final completion summary once the queue drains.

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
- Decide the long-term Telegram judge control model:
  - constrained harness tools backed by Telethon
  - vs broader/raw Telethon access plus stronger judge onboarding
  - Current gap: the existing Telegram eval platform cannot express reply-to
    actions or expose message IDs cleanly enough for Phase 0-C fork-via-reply
    validation.

Priority P2:
- Finalize CLI deprecation path:
  - which CLI scenarios remain mandatory for regression history
  - when CLI evals can be fully retired vs kept as explicit opt-in checks

## Immediate Next Steps

1. Draft the Tier 2 master eval scenario contract (intent, steps, criteria).
2. Implement subsystem tags/filtering for Tier 1 (`feature`) runs.
3. Rebalance long-running stress budgets after master eval lands.
4. Redesign Telegram judge capabilities so the judge can drive richer flows
   (reply-to, future topics/threads, structured message inspection) without
   flattening everything into transcript-only assertions.

## 2026-02-28 Telegram Eval Retrospective

Context:
- This retrospective is based on the Telegram notification/queueing work that:
  - fixed busy-time follow-up queue injection
  - replaced per-turn `(done)` notifications with a final queue-idle completion
    summary (`context: used / window`, optional `@username`)
  - surfaced actual thinking content instead of a hardcoded `thinking...`
  - tightened Telegram fragment reassembly to reduce false message merges
- The goal here is to preserve what was learned while iterating on the live
  Telegram evals, especially the difference between product failures and harness
  failures.

### What Was A Real Product Bug

- The queue bug was real.
- Incoming Telegram messages while the chat was busy were being serialized
  behind the per-chat run lock instead of entering the in-flight queue path.
- As a result, a user could send a follow-up while the agent was working and
  that message would often wait for the current turn to finish, which defeated
  the intended hook-based queue delivery design.
- The deterministic Telegram eval for `tg_queue_while_busy` was the right kind
  of test for this behavior once the harness itself was fixed.

### What Failed For Harness Reasons

- The initial live Telegram failures after the product fix were not caused by
  the bot failing to send a completion message.
- The bot did send the final completion summary, but Telethon surfaced the
  underlined Telegram HTML as formatted wire text with underscore markers.
- Example of what the collector actually saw:
  - `__context: 23k / 200k`
  - `____@breedoon__`
- The completion detector in `tests/evals/platform_telegram.py` was matching a
  plain `context: used / window` line and therefore missed a valid completion.
- That produced false timeouts and made the run look like a transport or queue
  failure even though the agent had already finished correctly.
- The correct fix was to normalize Telegram formatting markers before applying
  completion detection, and to allow the optional username line.

### Why The Telegram Evals Took Time

- Some of the runtime was expected:
  - live Telegram transport
  - long-polling and message propagation
  - scenario drain windows that intentionally wait for queue completion
  - multiple multi-minute scenarios in one aggregate invocation
- Some of the runtime was wasted:
  - false timeouts caused by the completion collector not recognizing formatted
    completion messages
  - judge-lane overhead even when the transport behavior under test was already
    deterministically decidable
  - one separate judge-path hang that did not look product-related

### What Was Useful

- The deterministic Telegram lane was useful once the completion detector was
  fixed.
- The targeted subset mechanism (`OBS_TG_SCENARIOS=...`) was useful and should
  remain a first-class workflow for feature iteration.
- Splitting the Telegram scenarios into smaller deterministic batches made it
  much easier to isolate whether failures were:
  - transport/runtime problems
  - harness parsing problems
  - scenario contract problems

### What Was Not A Good Confidence Signal

- The full judge-inclusive Telegram aggregate was not a clean ship signal in
  this iteration.
- It was slow, mixed product assertions with harness behavior, and also exposed
  a separate judge-side hang where the evaluator path did not conclude cleanly.
- That meant a failing or hanging aggregate could not be read as "the Telegram
  feature is broken" without extra investigation.
- The main risk is not just runtime cost. The bigger problem is diagnostic
  ambiguity: the slower and more layered the aggregate gets, the less obvious it
  is whether a red result points to product behavior, collector parsing, or the
  judge subprocess itself.

### Specific Cleanup Recommendations

1. Normalize Telegram formatting before applying any transcript contract checks.
   Completion detection, sentinel checks, and any transcript parsing should work
   on normalized text rather than raw formatted wire text.
2. Keep deterministic Telegram transport checks separate from judge scenarios.
   Transport invariants such as queue delivery, completion detection, HTML
   formatting, and split-message reconstruction should not depend on an LLM
   judge when explicit assertions are sufficient.
3. Preserve and expand scenario subsetting (`OBS_TG_SCENARIOS`) as the default
   feature-loop workflow. A developer working on one transport behavior should
   not have to wait for the whole Telegram matrix.
4. Investigate the judge-lane hang as a harness bug with its own owner and
   acceptance test. Do not treat it as a vague "Telegram evals are slow"
   problem. It is a separate failure mode.
5. Document completion detection as a wire-level contract, not just a rendered
   UI contract. Telegram formatting can transform what the collector sees.
6. Reduce drain and timeout budgets only after collector correctness is
   established. Shortening timeouts before fixing parsing just produces faster
   false negatives.
7. Prefer deterministic assertions for notification and queue semantics. Use
   judges where the behavior is genuinely qualitative, not where the system
   already exposes a concrete protocol signal.
8. When a live eval fails, first classify the failure:
   - Did the bot fail to send the expected signal?
   - Did the collector fail to recognize a valid signal?
   - Did the judge fail to terminate or grade sanely?
   This classification should be explicit in future debugging notes.

### Practical Guidance For Future Runs

- Start with local deterministic support tests before live Telegram runs:
  - `tests/evals/test_platform_telegram_timeouts.py`
  - `tests/evals/test_deterministic.py`
  - `tests/evals/test_falsifiability.py`
- For live Telegram work, prefer small deterministic batches via
  `OBS_TG_SCENARIOS` before attempting the full aggregate.
- If the product appears to work in Telegram but the eval times out, inspect the
  exact collected bot text first. Do not assume the queue or transport is broken
  until the collector path is ruled out.
- Treat judge-inclusive aggregate failures as ambiguous until the transcript and
  evaluator subprocess behavior are inspected.

### Bottom Line

- The deterministic Telegram evals were worth keeping and were helpful after the
  collector fix.
- The judge-inclusive aggregate currently needs cleanup before it can be treated
  as a high-confidence gating signal.
- The main lesson from this iteration is that harness correctness has to be
  established before timeout tuning or judge expansion is meaningful.
