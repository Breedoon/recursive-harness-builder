# Testing Philosophy

> Authoritative rules live in `CLAUDE.md` → "Evaluations (Evals)".
> This file expands on the *why* and gives worked examples. If they disagree, follow `CLAUDE.md`.

## The Three Layers

| Layer | What it proves | What it can't prove |
|-------|---------------|-------------------|
| **Unit tests** (mocked) | Code is wired correctly: routing, data flow, pure functions | Whether the system actually works with a real SDK |
| **Integration tests** (real HTTP + SDK) | The HTTP + SDK pipeline produces real responses | Whether the user experience is correct |
| **Evals** (real CLI/Telegram + SDK judge) | The system works from a user's perspective | Nothing — this is the top |

Each layer has a job. None replaces the others.

## Writing Eval Scenarios

### The Intent Section Is the Judge's Primary Weapon

The judge is an intelligent LLM. It can evaluate nuance, spot suspicious behavior,
and exercise judgment — but only if you give it the context to do so. The Intent
section is not a formality. It is the single most important part of the scenario.

A good Intent section tells the judge:

1. **What this feature is** — in plain language, what does the system do here?
2. **Why it matters to the user** — what user problem does this solve?
3. **What a working interaction looks like** — from the user's perspective, not the code's
4. **What a broken system would produce** — 2-3 concrete failure modes
5. **What "suspicious but technically passing" looks like** — the subtle failures

#### Example: Bad Intent

```markdown
## Intent
- Verify that session recovery works after errors.
- Flag if the response contains error messages.
```

This tells the judge nothing. It will mechanically check criteria and miss subtle failures.

#### Example: Good Intent

```markdown
## Intent
This tests whether the agent's session survives internal errors without the user
noticing. The recovery mechanism reconnects to the same conversation — from the
user's perspective, nothing should break.

A working system: user sends a message, something crashes internally, the system
recovers silently, user sends a follow-up, and the agent responds with clear
memory of the previous conversation. The user never knows anything went wrong.

A broken system looks like:
- The follow-up gets a blank-slate response — agent introduces itself, asks how it
  can help, or gives a generic answer with no reference to the previous exchange
- The agent says "starting fresh" or "new session" or apologizes for losing context
- The follow-up gets no response at all (session is dead)

Suspicious: the agent responds to the follow-up but is vague or generic in a way
that could mean it lost context and is just being polite. If the agent doesn't
reference anything specific from the first exchange, that's a red flag even if
the response is superficially coherent.
```

This gives the judge enough context to be a real QA engineer, not a pattern matcher.

### Criteria Should Test What Users Would Notice

The judge is intelligent. Criteria should describe **behavioral outcomes** — what a
user would observe — not implementation details or string matching.

**Bad criteria** (tests implementation artifacts):
- "Response does not contain '(session reset)'" — the code could change that string
  while still nuking the session. This criterion would pass on a completely broken system.
- "Response is longer than 100 characters" — proves nothing about quality
- "Response contains the word 'vault'" — the agent could say "vault" in any context

**Good criteria** (tests what the user would notice):
- "The agent's second response references specific content from the first exchange,
  proving conversational memory survived the error"
- "The response reads like a continuation of an ongoing conversation, not like a
  fresh introduction or a cold start"
- "The agent produces a substantive response covering most of the requested topics,
  not a truncated fragment or a refusal"

The pattern: **write criteria as if you're explaining to a smart human tester what
to check, not as if you're writing a regex.**

A few specific structural checks are fine as supplements (e.g., "the response includes
at least one code block"), but the core criteria should be behavioral. Trust the judge
to evaluate whether the interaction felt like a working system.

### Falsifiability: The Pre-Flight Check

Before writing any criterion, ask: **"If this feature is completely broken, would
this criterion still pass?"**

If yes, the criterion is useless. Rewrite it.

Example: Testing session continuity. If the session silently resets:
- "Response is non-empty" → PASSES (broken system still responds)
- "Response does not contain 'error'" → PASSES (no error string, just amnesia)
- "Agent recalls the code word from the first message" → FAILS (this actually catches it)

### The Judge's Output Structure

Every judge verdict includes three sections (enforced by `judge.py`):

1. **CRITERIA CHECK** — per-criterion pass/fail with brief reasoning
2. **INTENT CHECK** — did the behavior match the scenario intent, even beyond criteria?
3. **NOTES** — suspicious behavior, quality concerns, or "none"

Read the NOTES. A PASS with concerning notes may indicate a fragile eval that needs
stronger criteria, or a system behavior that's technically correct but user-hostile.

## Where to Look

- Authoritative rules: `CLAUDE.md` → "Evaluations (Evals)" → The Eval Commandments
- Eval harness: `tests/evals/test_evals.py`
- Judge prompts/logic: `tests/evals/judge.py`
- Scenario parser: `tests/evals/scenario.py`
- Scenario definitions: `tests/evals/scenarios/*.md`
- Telegram eval platform: `tests/evals/platform_telegram.py`
- Eval guardian policy: `.claude/agents/eval-guardian.md`

## Historical Notes (Why This Exists)

This project repeatedly learned that mocked confidence is not reliability:

- Mock-heavy suites passed while real user flows still broke.
- `reconnect()` and `soft_reset()` were shipped with only mocked tests — never called
  against a real SDK. The mocked tests all passed. The real system crashed.
- Queue/interrupt/streaming edge cases appeared only in real interaction loops.
- Telegram behavior required real Telegram runs (not synthetic stubs).
- An eval criterion "response does not contain '(session reset)'" would pass even when
  the session was actually being nuked — because the error message string is not the
  same as the behavior.

The eval stack exists to prevent these regressions. The philosophy exists because
every shortcut we tried — mocking SDK calls, testing strings instead of behavior,
writing thin Intent sections — eventually let a bug through.
