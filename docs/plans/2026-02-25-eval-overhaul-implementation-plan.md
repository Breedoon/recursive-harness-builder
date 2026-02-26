# Eval Overhaul Implementation Plan

**Date:** 2026-02-25
**Status:** Proposed
**Scope:** Evals architecture, test taxonomy, runtime performance, fixture isolation, and validation quality.

## Original User Request (verbatim)

> So I want you to help me do an overhaul of evals. And first I want you to tell me how they work, how they are set up. And I know like what they are. I know that it's like a judge with trying to use the real thing through like through telethon to basically emulate as real as possible the real environment and then actually report suspicious behavior, you know, report things that were all about. That's the kind of setup so that it tests as closely as possible to the real scenario. The things that I want to do are first of all, fix the timing because, and I guess figure out the, like, what's a better way to organize it because right now and I can see the eval running and it's like, I can see the chat that's actually happening. And I can see that it takes like 20 seconds for the whole eval to go. So, uh, the judge send a message, the agent replies, and that happens within 20 seconds. And then for three minutes, nothing happens because there's some timeout or something like that's obviously doesn't make sense. That's one, like, that's one tactical aspect of the evals, like to improve the, uh, the architecture of evals so that the agents are more like interactive with it. So they maybe use that as a tool so they can like go idle, uh, and then like be woken up by a message or go idle for maybe no longer than like 30 seconds. So, uh, maybe it's like, maybe we will force them to not go idle or something. So they'll just like run sleep commands or something. Um, basically to speed that process up. That's one thing. Another thing is more about what we are evaluating. And a lot of these evals were kind of added as a test for individual steps. So individual features that we implemented, and we added an eval to test that feature. But I want something that's kind of a more holistic eval. And I want to also come up with a process for basically, like, how do we go about this, so we don't have to run every single eval for every single past feature? How do we maybe have one, like, holistic eval that tests, like, a complex task and says, like, sees that, you know, skills are loaded, the agent is not, like, stuck. That's, like, queuing in the same thing, just, like, a true integration tests as, like, one thing that would be, like, run at the end of a new feature request to make sure nothing broke. But it wouldn't maybe include all the detailed, like, race conditions or something. Maybe it would. Maybe it would. Maybe it would. But basically, a longer running eval to run on every, like, edit. And then have, I don't know, either optional evals or those that are testing for, like, for specific behaviors. And some of that, you know, might not even involve, like, an LLM judge, honestly. Maybe those would be, like, more unit tests. And I don't know if the unit tests are, like, end-to-end or integration tests, whatever they are, if they use telethon and they, like, check that already. Like, so that if we implement, like, a new feature, we can just test that feature and maybe run the main eval if there's, like, a bigger change. Versus if it's, like, a surgical edit that we probably don't even need to run the eval. I don't want to add the encouragement that evals might not have to be run so then modals skip them on the future coding agents, skip the evals when they are actually necessary. Like I want the default to be eval. And then if I like permit and I make the decision to not run them, like it's up to me, not up to the agents who are coding it up. And I wonder how much of these current evals have to be evals versus can be more like integration tests that are faster. Potentially they still run the actual Telegram API, the actual like SDK API, but they don't have this overhead of a judge because all they're doing now is just, you know, testing that concurrency. Concurrency doesn't like break that queuing works or like something that is more integration test versus what I want the eval to be. And then like an alarm judge is to actually go for this more complex task. And on that note, I want to know what is happening with the, like where this, um, this test, uh, bot that is test agent that we're developing, where it is run in which folder, because I have the main obsidian vault, which I don't want to touch for testing, obviously, like we should make a copy of it and like do something with that copy. And I have a suspicion there's something about like some data leaking from the main vault into test, which is really not good. And obviously, you know, they should have their working directory, like the working director of the test should be in that fixture test vault. Um, like, I just want to make sure that it actually is a separate vault that is functioning the same way as in production. And because I have a feeling there are some files that were leaking or some updates that I made to one file in the main vault and they somehow migrated to the fixture vault. I'm not sure if that's actually the case, but it's, um, worth, um, double, triple, triple checking. So what I want you to do now is to go look into all of these. Tell me if like what they are now, what each eval does roughly, how much that actually needs to be an eval that actually has an intelligent judge that can, you know, understand a bigger picture and flag actual things versus I feel like some of these evals are like give a very narrow context to the judge that it's basically could be an integration test because yeah. And then that you can also check, you know, the fixture and the other things that I mentioned. And so I want like an overview of those things, how they work, how they're set up. And I also want to get like suggestions on what's the best way to have this like system off maybe like a few full on integration evals that are actual evals that are doing complex tasks and not just, you know, checking if concurrency works or something. Versus how do we approach like surgical edits that don't need evals versus how do we approach like bigger edits that like adding a feature. So that each time if the eval, the main eval showed that the feature is not working then so that they don't have to, the agents don't have to rerun the whole main eval just to see if the feature isn't working. So they have something to work with so they can like have a temporary eval or something like those ones that we have currently that are more surgical that are more like focused on like that specific feature. Also, I would contemplate like literally having the coding agents themselves run the test and actually interact with the agents because they make the mistake of assuming that judges are stupid and the agent is stupid. And they give them like very narrow context, very narrow tasks, but in practice, they're all either the same model or models that are equally intelligent. So like, you know, it might be an extra step for the coding agents to delegate work to the judge agent who will work with the test agent versus the coding agent itself just running the thing and seeing if the feature works. I feel like it's worth keeping the judge for like adversarial check, because the judge won't have a bias to neglect something, but it's something also worth considering. So yeah, tell me what it's now, tell me how redundant some of these are, tell me why the architecture is such that they take unnecessary amount of time and suggest what would be a better way to organize this.

Additional constraints from follow-up (verbatim):

> Okay, so could you go ahead and write this implementation plan with reasoning and your exact steps of what you will do, including high level, what are we doing, high level, what are the gates, what are we keeping, how are we reorganizing things, all the discussions that we had. And also the phases and the detailed breakdown of phases and different scenarios you will put in each phase or different steps you will do in each phase, potentially including which files you will edit or what you will edit in specific files. Like I want like this source of truth document that you can reference against and especially how will you like enforce or how will you check that it's working now and it's doing the job and you actually succeeded. Like what are the success criteria that you will set for yourself? Because for once, I guess you can just run the evals now and they should pass and the new ones should also pass, but they also, you know, should be meaningful to be able not to pass. So you don't accidentally make an eval that always passes. So write it down in like a docs, some like as a spec maybe, and it should be like a detailed document that will guide you, but also could be used to pick up from here later on. ... let's not add that overhead.

## Executive Summary

We will restructure evals into **two lanes** and run them through **three lightweight run profiles** without adding PR/CI process overhead.

- Two lanes:
  - `deterministic`: strict assertions, no LLM judge.
  - `judge`: transcript-level behavioral evaluation for ambiguous, holistic behavior.
- Three run profiles (local workflow, no new governance burden):
  - `smoke`: always-run default, fast and high-signal.
  - `feature`: targeted checks for the subsystem being edited.
  - `full`: complete sweep before major merges/releases or when explicitly requested.

Core outcomes:

1. Remove avoidable idle timeouts and long dead periods in eval runtime.
2. Move surgical scenarios out of judge overhead where deterministic checks are stronger.
3. Keep a small number of holistic judge scenarios for adversarial behavior validation.
4. Eliminate fixture state carryover by running each eval session on an ephemeral copy of a template fixture vault.
5. Add falsifiability checks so tests are proven capable of failing.

## Non-Goals

1. No new mandatory PR workflow.
2. No heavyweight CI gate design in this phase.
3. No replacement of all judge evals with deterministic checks.
4. No redesign of product runtime architecture outside eval harness needs.

## Current State (Audit Findings)

### Eval architecture now

- Scenario source: `tests/evals/scenarios/*.md`
- Runner: `tests/evals/test_evals.py`
- Judge: `tests/evals/judge.py`
- CLI platform: `tests/evals/platform.py`
- Telegram platform: `tests/evals/platform_telegram.py`

### Timing bottlenecks now

1. Telegram collection waits until timeout budget if `(done)` is missing or delayed:
   - `tests/evals/platform_telegram.py`
2. Concurrent transcript mode adds up to 120s + 60s continuation waits:
   - `tests/evals/judge.py`
3. Telegram aggregate run has fixed inter-scenario drain sleeps:
   - `tests/evals/test_evals.py`
4. Several scenario waits/sleeps are very large by default.

### Fixture state now

- Eval vault is a persistent local clone created once and reused:
  - `tests/conftest.py` + `scripts/clone_vault.sh`
- This causes stale state carryover across runs (`fixture_vault` can become dirty).
- Path wiring is currently correct (evals point to fixture via `OBS_VAULT_PATH`), but freshness/isolation are weak.

## Target Model

## Two Lanes

1. Deterministic lane
- Assertion-first tests.
- Explicit expected outcomes and ordering checks.
- Preferred for transport/plumbing/guardrail behaviors.

2. Judge lane
- Judge reads transcript (or uses bounded interaction) and checks behavior vs intent.
- Reserved for semantic/holistic behaviors where strict output matching is brittle.

## Three Run Profiles (lightweight, local-first)

These are **run profiles**, not heavy policy gates.

1. `smoke` (default each meaningful edit)
- Fast deterministic core checks.
- 1-2 judge scenarios max.
- Objective: catch obvious regressions quickly.

2. `feature` (during implementation loop)
- Scenario subset tied to touched subsystem.
- Includes at most one relevant judge scenario when needed.

3. `full` (explicit pre-release/pre-major change)
- All deterministic + all judge + stress scenarios.

## Scenario Disposition Plan

### Keep as judge (holistic/adversarial)

1. `context_awareness.md`
2. `background_fork.md`
3. `tg_chronological_output.md`
4. `tg_stress_chronology.md`
5. `tg_large_output_resilience.md`
6. `tg_transport_desync_on_send_error.md`

### Convert to deterministic (or migrate to deterministic equivalents)

CLI-focused:

1. `basic_chat.md`
2. `tool_visibility.md`
3. `vault_file_access.md`
4. `vault_write.md`
5. `session_continuity.md`
6. `skills_awareness.md`
7. `fork_tool.md`
8. `immutable_guard.md`
9. `queue_message.md`
10. `interrupt.md`
11. `session_context_info.md`

Telegram-focused:

1. `tg_auth_guard.md`
2. `tg_background_auto_delivery.md`
3. `tg_tool_visibility.md`
4. `tg_html_format.md`
5. `tg_queue_while_busy.md`
6. `tg_message_split.md` (keep as deterministic transport/format stress)
7. `tg_transport_desync_forced_concurrency.md`

## Implementation Phases

## Phase 1: Runtime and Harness Foundations

**Goal:** remove dead-time overhead and make runs predictable.

### File changes

1. `tests/evals/platform_telegram.py`
- Add explicit done/idle state machine with bounded quiescence behavior.
- Introduce separate limits:
  - `first_message_timeout`
  - `done_timeout`
  - `idle_quiescence_timeout`
- Fail fast with structured timeout reason when `(done)` missing beyond allowed window.

2. `tests/evals/judge.py`
- Remove fixed 120/60 continuation waits; replace with shorter bounded continuation strategy.
- Use scenario-configurable continuation budget.

3. `tests/evals/test_evals.py`
- Reduce fixed drain sleeps where safe.
- Add optional profile filter plumbing (e.g., `OBS_EVAL_PROFILE`).

4. `tests/evals/scenario.py`
- Extend parser for optional scenario metadata block (lane/profile tags, timeout budget).

### Verification

1. No uncontrolled 3-minute idle in normal successful scenario runs.
2. Timeout failures are explicit and attributable (not silent hangs).
3. Existing passing scenarios continue passing or fail with clear actionable reasons.

## Phase 2: Taxonomy and Profile Wiring

**Goal:** split judge vs deterministic responsibilities cleanly.

### File changes

1. `tests/evals/test_evals.py`
- Add scenario selection by lane/profile tags.
- Keep Telegram aggregate sequencing where needed, but route subsets by profile.

2. `tests/evals/scenarios/*.md`
- Add metadata tags per scenario:
  - `lane: deterministic|judge`
  - `profiles: smoke|feature|full`
  - optional `subsystem` tags.

3. `tests/evals/README.md` (new)
- Source of truth for running profiles and authoring conventions.

### Verification

1. `smoke`, `feature`, `full` run distinct subsets correctly.
2. Lane separation visible and easy to audit.
3. No scenario becomes orphaned (every scenario assigned at least one profile).

## Phase 3: Deterministic Migration (Surgical and Transport Cases)

**Goal:** convert narrow behavior checks from judge-based to assertion-based.

### File changes

1. `tests/evals/test_deterministic_cli.py` (new)
- Migrate CLI deterministic scenarios (file access/write, continuity, immutable guard, queue, interrupt, tool visibility).

2. `tests/evals/test_deterministic_telegram.py` (new)
- Migrate Telegram deterministic scenarios (auth, done sentinel behavior, visibility, queue while busy, formatting assertions).

3. `tests/evals/platform.py` and `tests/evals/platform_telegram.py`
- Add helper APIs for deterministic assertions (event extraction, message boundaries, sentinel alignment).

### Verification

1. Converted scenarios no longer rely on judge output text.
2. Assertions are precise and explain failures clearly.
3. Runtime of deterministic lane drops materially versus current judge-heavy path.

## Phase 4: Holistic Judge Suite Consolidation

**Goal:** keep a small, high-value adversarial judge suite.

### File changes

1. `tests/evals/scenarios/` (judge set only)
- Refine retained judge scenarios for broader holistic intent coverage.
- Ensure each has strong `Intent` and non-trivial falsifiable criteria.

2. `tests/evals/judge.py`
- Enforce structured verdict sections (`CRITERIA CHECK`, `INTENT CHECK`, `NOTES`, `VERDICT`).
- Add stricter prompt templates for holistic evaluation quality.

### Verification

1. Judge suite count is intentionally small.
2. Each judge scenario evaluates behavior a deterministic check cannot robustly capture.
3. Judge output has useful failure diagnostics, not generic pass/fail.

## Phase 5: Fixture Isolation and Refresh Workflow

**Goal:** prevent stale carryover while preserving realistic data.

### File changes

1. `scripts/refresh_fixture_vault.sh` (new)
- Explicitly refresh template fixture from real vault snapshot (manual action).
- Record refresh timestamp and source commit hash if available.

2. `tests/conftest.py`
- Replace persistent mutable `fixture_vault` usage for evals with per-run ephemeral copy from template.
- Add hard guard: fail if eval path resolves to real vault path.
- Add optional guard to fail if template fixture has uncommitted changes.

3. `scripts/clone_vault.sh`
- Keep or repurpose as template setup, not per-run mutable test target.

### Verification

1. Every eval run starts from clean ephemeral vault copy.
2. No writes leak into real vault path.
3. Reproducibility improves (reruns do not depend on prior eval side effects).

## Phase 6: Meaningfulness and Falsifiability Hardening

**Goal:** ensure tests can fail for real regressions.

### File changes

1. `tests/evals/test_falsifiability.py` (new)
- Add sanity checks for scenario quality rules (e.g., no trivial criteria).

2. `tests/evals/README.md`
- Add authoring checklist requiring explicit broken-mode thought process.

3. Existing deterministic tests
- Add negative/control assertions per behavior where possible.

### Verification

1. Each deterministic test has at least one negative-path assertion.
2. Judge scenarios include explicit "broken behavior" examples in Intent.
3. Spot mutation checks demonstrate at least one failure when behavior is intentionally broken.

## What Will Be Kept As-Is

1. Real SDK interaction and real platform interaction remain the core principle.
2. Telegram aggregate sequencing remains for shared-state safety.
3. `(done)` sentinel remains the transport completion contract.
4. Existing strong eval philosophy in `CLAUDE.md` remains authoritative.

## What Will Be Explicitly Reduced

1. Judge usage for low-ambiguity checks.
2. Large static wait defaults where transport events can drive progression.
3. Persistent mutable fixture vault as default eval target.

## Success Criteria

## Functional success

1. `smoke`, `feature`, and `full` profiles run and select correct scenario subsets.
2. Deterministic lane catches transport/plumbing regressions without judge involvement.
3. Holistic judge lane still catches semantic/off-intent behavior.

## Runtime success

1. No unexplained multi-minute idle stalls in normal passing runs.
2. `smoke` runtime is substantially shorter than current full eval path.
3. Telegram timeouts fail fast with explicit cause.

## Isolation success

1. Eval writes appear only in ephemeral eval vault copies.
2. Real vault path is never used by eval processes.
3. Template refresh is explicit and auditable.

## Quality success (non-theater)

1. At least one deliberate negative control per deterministic area proves failability.
2. Judge scenarios include intent text that describes suspicious-but-passing behavior.
3. No scenario with criteria that a dumb echo bot could routinely satisfy.

## Rollout Order

1. Phase 1 (timing foundations)
2. Phase 5 (fixture isolation)
3. Phase 2 (taxonomy/profiles)
4. Phase 3 (deterministic migration)
5. Phase 4 (judge consolidation)
6. Phase 6 (falsifiability hardening)

Rationale for this order:
- Fixing timing and fixture correctness first stabilizes the environment.
- Then classification and migration happen on top of a reliable harness.

## Risks and Mitigations

1. Risk: over-conversion to deterministic removes valuable semantic checks.
- Mitigation: preserve explicit judge suite for ambiguous behaviors.

2. Risk: profile complexity becomes confusion.
- Mitigation: one short runbook and single command entry points per profile.

3. Risk: fixture refresh drifts from production shape.
- Mitigation: explicit manual refresh command and refresh notes in repo.

4. Risk: false confidence from brittle deterministic assertions.
- Mitigation: require negative controls and transcript-order assertions, not string trivia.

## Operational Commands (Target End-State)

Examples (final command names may vary by implementation details):

```bash
# Fast default
.venv/bin/pytest tests/evals/ -m eval -k profile_smoke -v

# Targeted feature loop
OBS_EVAL_PROFILE=feature OBS_EVAL_SUBSYSTEM=telegram .venv/bin/pytest tests/evals/ -m eval -v

# Full sweep
OBS_EVAL_PROFILE=full .venv/bin/pytest tests/evals/ -m eval -v --timeout=1800
```

## Deliverables

1. Refactored eval harness with bounded idle handling.
2. Scenario metadata + profile selection support.
3. Deterministic test modules for surgical/transport checks.
4. Consolidated holistic judge scenario set.
5. Ephemeral fixture-per-run workflow and refresh script.
6. Eval runbook and scenario authoring/falsifiability checklist.

