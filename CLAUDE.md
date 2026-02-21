# OBS Agent

Personal AI assistant backed by an Obsidian vault. Uses the Claude Agent SDK for Python.

## Paths

| What | Path |
|------|------|
| **Runtime code** (this dir) | `/Users/breedoon/Documents/obs/` |
| **Obsidian vault** | `/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T` |
| **Vault shorthand** | `T/` in all vault references below |

## Documentation Entry Points

Primary context and skills still live in the vault, but this repo now contains a
maintained mirror/reference set under `docs/` for implementation work.

### Vault Docs (source context + skills)

| Document | Vault Path | What It Contains |
|----------|-----------|-----------------|
| **Implementation plan** | `T/.claude/system/implementation-plan.md` | Architecture, component specs, 12 TDD steps, team tracks, verification checklist |
| **Architecture** | `T/.claude/system/architecture.md` | Full system design |
| **Design decisions** | `T/.claude/system/decisions.md` | 25 decisions (D001-D025) with rationale |
| **File conventions** | `T/.claude/skills/file-conventions/SKILL.md` | Vault directory map, all 11 rules, templates |
| **Session offboard** | `T/.claude/skills/session-offboard/SKILL.md` | Memory extraction procedure |
| **Update context** | `T/.claude/skills/update-context/SKILL.md` | How to persist to CLAUDE.md |
| **All skills** | `T/.claude/skills.md` | Parent note listing all 11 skills |
| **OpenClaw research** | `T/.claude/system/research/openclaw.md` | Memory, prompts, session management patterns |
| **claude-mem research** | `T/.claude/system/research/claude-mem.md` | Observer pattern, knowledge extraction |
| **Claude SDK research** | `T/.claude/system/research/claude-sdk.md` | Hooks, sessions, forking, MCP tools API |
| **Context** | `T/CLAUDE.md` | Agent's current brain state |

### Repo Docs (implementation + reference)

| Document | Repo Path | What It Contains |
|----------|-----------|-----------------|
| **Architecture (current runtime)** | `docs/architecture.md` | Current runtime architecture (CLI + daemon + Telegram + eval stack) |
| **Telegram flow plan** | `docs/plans/2026-02-19-telegram-message-flow-design.md` | Chronological Telegram behavior + observability decisions |
| **Design intent (historical)** | `docs/design-intent.md` | Original intent capture from design phase |
| **Implementation plan (historical MVP)** | `docs/implementation-plan.md` | Initial MVP sequencing; not a live roadmap |
| **Testing philosophy (historical, superseded)** | `docs/testing-philosophy.md` | Archived lessons; live testing policy is in this `CLAUDE.md` |
| **Specs** | `docs/specs/` | Focused specs and investigations (e.g. agent self-awareness) |

Quick map: `docs/README.md`

## Implementation

**Start here**: Read `T/.claude/system/implementation-plan.md`. It has everything:
- Architecture diagram and data flows
- Component specifications with code sketches
- 12 implementation steps in TDD order (Step 0 is the project scaffold)
- Team execution tracks (A: core infra, B: agent behavior, C: server+client, D: polish)
- Verification checklist

**Key SDK reference** (`claude-agent-sdk` v0.1.35):
- `ClaudeSDKClient` for daemon (interactive multi-turn), `query()` for forks (unidirectional)
- Hooks: PreToolUse, PostToolUse, Stop, PreCompact
- Forking: `ClaudeAgentOptions(resume=session_id, fork_session=True)`
- MCP tools: `@tool` decorator + `create_sdk_mcp_server()`
- `self_fork` MCP tool: agent-controlled forking for subtasks (replaces classification pipeline). Supports `background=true` to run forks without blocking — results are delivered via the message queue.
- `setting_sources=["project"]`: native loading of `.claude/skills/` by SDK
- Subagents cannot spawn sub-subagents (hence fork-based approach)

**Project structure:**
```
/Users/breedoon/Documents/obs/
├── CLAUDE.md                    # This file
├── pyproject.toml               # Dependencies, entry points
├── src/obs_agent/               # Runtime Python code
│   ├── config.py                # Paths, constants
│   ├── prompt.py                # System prompt builder (reads CLAUDE.md)
│   ├── hooks.py                 # SDK hooks (immutable guard, interrupt, queue, compact)
│   ├── tools.py                 # MCP tools (self_fork)
│   ├── fork.py                  # Fork runner (generic fork + extract_memory)
│   ├── session.py               # Session lifecycle (ClaudeSDKClient manager)
│   ├── daemon.py                # FastAPI server (uses ClaudeSDKClient via session)
│   ├── cli.py                   # CLI client
│   ├── runner.py                # Shared conversation orchestration
│   ├── telegram.py              # Telegram adapter runtime
│   ├── telegram_format.py       # Telegram HTML conversion + splitting
│   └── telegram_main.py         # Telegram entrypoint (loads .env + starts bot)
├── docs/                        # Architecture/plans/research/specs mirror
└── tests/                       # Unit + eval + integration tests
```

**Architecture: Agent controls forking, not daemon.**
The old classification pipeline (daemon forks to classify skills per message) has been replaced.
Now the agent itself decides when to fork via the `self_fork` MCP tool. Skills are loaded natively
by the SDK via `setting_sources=["project"]` which reads `.claude/skills/` from the vault.

## Vault Rules (for any vault file operations)

- **Extend before creating** — add to existing files by default
- **Parent note pattern** — `folder-name.md` sibling to `folder-name/`
- **Contextual wiki links** — `[[path|why relevant]]`, never bare links
- **Immutable files** — never edit `Misc/Meeting Notes/` transcripts
- **Templates** — structural templates live in `Assets/Templates/`, reference don't inline
- Full reference: `T/.claude/skills/file-conventions/SKILL.md`

## Evaluations (Evals) — THE ONLY PROOF THAT THE SYSTEM WORKS

### What Are Evals?

Evals are markdown scenario files in `tests/evals/scenarios/`. Each describes a user
journey. An interactive Claude SDK judge agent (`ClaudeSDKClient` + MCP tools) executes
the scenario against the real CLI with a real vault clone, then judges PASS/FAIL.

### The Eval Commandments

1. **EVALS ARE NOT OPTIONAL.** Every feature change MUST pass all evals before being
   declared done. Running unit tests alone is theater. Running mocked "E2E" tests is
   a lie. Only evals prove the system works.

2. **IF AN EVAL CAN'T RUN, FIX THAT FIRST.** If the eval infrastructure is broken,
   if the vault clone is missing, if the daemon won't start — these are P0 blockers.
   Do not work around them. Do not skip them. Fix them.

3. **NEVER MOCK THE SDK IN AN EVAL.** Evals use the real SDK, real daemon, real CLI.
   If you patch, mock, or fake anything in the eval path, you are writing a unit test.

4. **NEVER USE `anthropic.Anthropic()` FOR JUDGING.** The SDK uses subscription auth.
   There is no API key. The judge uses `ClaudeSDKClient` with MCP tools. If you write
   `import anthropic` in eval code, you are doing it wrong.

5. **THE JUDGE IS AN SDK AGENT.** The judge agent uses `ClaudeSDKClient` with MCP tools
   (`send_message`, `read_output`) to interact with the CLI. It follows a scenario and
   returns VERDICT: PASS or VERDICT: FAIL. No heuristics, no `len > 5`, no regex.

6. **READ THE EVAL OUTPUT.** A passing eval is not a green checkmark to ignore. Read
   the judge's reasoning. If the judge passed for wrong reasons, the eval is broken.

7. **EVAL RESULTS ARE NON-DETERMINISTIC AND THAT'S OK.** LLM outputs vary. If an eval
   fails once but passes on retry, that's acceptable. Consistent failure = broken feature.

8. **EVERY CHANGE NEEDS AN EVAL.** Before declaring a feature or migration done,
   identify which eval would fail if the change broke. If no eval covers it, write
   one first. The vault_write eval caught that permission_mode was never set. The
   context_awareness eval catches system prompt loading. If you can't point to an
   eval that would catch a regression, you haven't tested it.

9. **EVAL SCENARIOS MUST TEST REAL BEHAVIOR.** Scenarios must exercise actual agent
   capabilities (vault reads, writes, context awareness, safety guardrails) — not just
   "can the agent respond to hello." If a scenario can pass with a dumb echo bot, it is
   not an eval.

10. **PASS WITH CONCERNS MUST BE SURFACED.** Judges must report suspicious or
   off-intent behavior even when criteria pass. Every scenario should include an
   `Intent` section, and judge output must include a `NOTES` section. `VERDICT: PASS`
   does not mean "looks good" unless `NOTES: none`.

11. **BRAINSTORM FALSIFIABILITY BEFORE WRITING CRITERIA.** Before drafting any eval,
   ask: "If this feature is completely broken, what would the output look like? Would
   my criteria still pass?" Write criteria that test **behavioral outcomes**, not string
   matching. Bad: "response does not contain '(session reset)'" — the code can change
   the string while still nuking the session. Good: "the agent recalls details from the
   first message in its second response, proving the conversation actually continued."
   Test what a user would notice, not what an implementation detail looks like.

12. **THE JUDGE IS INTELLIGENT. WRITE FOR INTELLIGENCE, NOT FOR A REGEX ENGINE.**
   The judge is a capable LLM — treat it like a smart QA engineer, not a checkbox
   machine. The Intent section is the judge's primary weapon. It should explain:
   - What this feature is and why it matters to the user
   - What a normal successful interaction looks like from the user's perspective
   - What a broken system would produce (give 2-3 concrete failure modes)
   - What "suspicious but technically passing" looks like

   **Criteria should be mostly behavioral and broad.** A few specific structural checks
   are fine, but the criteria should not over-constrain the judge. Trust it to evaluate
   whether the interaction felt like a working system, not just whether strings matched.
   The best criteria read like what a user would check, not what a `grep` would check.

   **Example of a rich Intent (session resilience eval):**
   ```
   ## Intent
   This tests whether the agent's session survives errors. The system recovers from
   SDK crashes by reconnecting to the same session — the user should never notice
   anything broke. A working system: user sends message, something crashes internally,
   user sends a follow-up, agent responds and remembers the previous conversation.
   A broken system: the follow-up gets a blank-slate response with no memory of what
   came before, or the agent says "starting fresh" or introduces itself again, or the
   follow-up gets no response at all. Suspicious: agent responds to the follow-up but
   is vague or generic in a way that could mean it lost context and is just being polite.
   ```

   **Example of good vs bad criteria:**
   - Bad: `Response does not contain "(session reset)" or "error"` (tests a string)
   - Bad: `Response is longer than 100 characters` (tests nothing meaningful)
   - Good: `The agent's second response references specific content from the first
     exchange, proving conversational memory survived` (tests real behavior)
   - Good: `The response reads like a continuation of an ongoing conversation, not
     like a fresh introduction or a cold start` (trusts the judge's intelligence)

### Running Tests

```bash
# Unit tests (fast, mocked):
.venv/bin/pytest tests/ -q -m "not eval and not integration"

# Live integration (real HTTP + SDK, no CLI):
.venv/bin/pytest tests/ -q -m integration --timeout=300

# Full evals (CLI + Telegram; requires Telegram creds for tg_*):
.venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Telegram evals only (single aggregate test):
.venv/bin/pytest tests/evals/test_evals.py::test_eval_telegram_all -v -s -m "eval and telegram" --timeout=300

# Optional: run only selected Telegram scenarios (comma-separated)
OBS_TG_SCENARIOS=tg_auth_guard,tg_tool_visibility .venv/bin/pytest tests/evals/test_evals.py::test_eval_telegram_all -v -s -m "eval and telegram" --timeout=300

# EVERYTHING (duration varies; much longer when Telegram evals run):
.venv/bin/pytest tests/ -v --timeout=300
```

### Test Layers

| Layer | Proof Value | Where |
|-------|-------------|-------|
| **Evals** | HIGHEST — real CLI + vault + SDK judge | `tests/evals/` |
| **Live Integration** | Medium — real HTTP + SDK | `tests/test_integration_live.py` |
| **Unit** | Low — mocked logic, routing | `tests/test_*.py` |

### Spike Before You Build

**Before implementing any feature that uses an SDK API you haven't used before, write a 10-line spike script that proves the API actually works.** Run it. If it crashes, the feature cannot be built as planned. Plans that use unverified SDK features are not plans — they are hopes.

### Eval Guardian Agent

The `.claude/agents/eval-guardian.md` agent has **veto power** over eval quality. It does not write evals — it reviews scenarios, runs them, reads judge output, and blocks until satisfied. It is deliberately adversarial. It catches theater testing, manipulated assertions, and contrived scenarios. If the Eval Guardian blocks, fix what it found.

In the first eval build, the guardian caught 5 real bugs (PATH inheritance, prompt collision, concurrent test design, `@tool()` args, `mcp_servers` format). In the second round (vault migration + new evals), the guardian caught 4 more issues (vault_write needed separate read-back step, context_awareness criteria too vague, skills_awareness had unjudgeable criterion, immutable_guard needed control write to prove hook works). It also caught that fixture_vault was stale after the vault migration.

### Eval Architecture: Platform + Mode Split

CLI sequential scenarios (all `Send:` steps):
- All steps are `Send:` — judge drives CLI via MCP tools (`send_message`, `read_output`)

CLI concurrent scenarios (`SendNowait`/`Sleep` present):
- Steps include `SendNowait:` and `Sleep:` for timing control
- pexpect harness drives CLI directly, captures transcript
- Judge evaluates transcript post-hoc (no MCP tools needed)

Telegram scenarios (`tg_*`):
- Run sequentially in one aggregate test (`test_eval_telegram_all`) to avoid chat pollution
- Harness starts real Telegram bot process with `OBS_VAULT_PATH=<eval_vault>`
- Telethon userbot drives real Telegram messages
- Scenario output includes per-scenario judge text, including `CRITERIA CHECK`, `INTENT CHECK`, and `NOTES`

This split exists because MCP tool calls are sequential — the judge can't send a message while waiting for a previous response.

### Eval Timing

- **20 scenarios total** (12 CLI + 8 Telegram)
- Telegram aggregate test is marked with a higher per-test timeout because it runs all Telegram scenarios serially
- Scenarios are intentionally sequential to preserve causality and avoid cross-test message contamination

### Current Eval Coverage (20 scenarios)

| Eval | Tests | Platform/Mode |
|------|-------|------|
| basic_chat | Agent responds coherently | CLI Sequential |
| tool_visibility | Agent lists `.claude/skills/` | CLI Sequential |
| vault_file_access | Agent reads `CLAUDE.md` | CLI Sequential |
| session_continuity | Two-turn recall | CLI Sequential |
| vault_write | Agent creates + reads back a file | CLI Sequential |
| context_awareness | Agent knows identity/threads from system prompt | CLI Sequential |
| skills_awareness | Agent lists skills from system prompt | CLI Sequential |
| immutable_guard | Control write succeeds, Meeting Notes write blocked | CLI Sequential |
| fork_tool | Fork inherits parent conversation history | CLI Sequential |
| background_fork | Background fork runs without blocking, results via queue | CLI Sequential |
| queue_message | Queued message during streaming | CLI Concurrent |
| interrupt | /stop during streaming | CLI Concurrent |
| tg_auth_guard | Authorized-user Telegram flow | Telegram Sequential |
| tg_background_auto_delivery | Background fork auto-delivery in Telegram | Telegram Sequential |
| tg_chronological_output | Chronological tool/text + done sentinel in Telegram | Telegram Sequential |
| tg_html_format | Telegram rendering correctness | Telegram Sequential |
| tg_message_split | Long split-message coherence | Telegram Sequential |
| tg_queue_while_busy | Telegram queue behavior under concurrent inputs | Telegram Sequential |
| tg_stress_chronology | Mixed workload chronology stress | Telegram Sequential |
| tg_tool_visibility | Inline tool observability in Telegram | Telegram Sequential |

### Known Gaps (work in progress)

- Missing: memory offboard, error handling, fork tool eval verification.

### Pexpect Gotchas

- **Prompt collision**: Default `"> "` matches `> blockquote` in agent output. Use `OBS_EVAL_PROMPT` env var with a unique prompt like `OBS_EVAL> `.
- **Initial prompt consumption**: After `expect("Type your message")`, the initial prompt sits in the buffer. Must `expect(prompt_pattern)` again in `__init__` or the first `wait_for_prompt()` returns CLI header text.
- **Concurrent multi-prompt capture**: After `SendNowait` + `Sleep` steps, call `wait_for_prompt()` up to 3 times to capture continuation responses (queued message replies).
- **Inherit PATH**: Use `os.environ.copy()` with overlaid test vars, not a fresh dict. The SDK spawns `claude` as a subprocess that must be on PATH.

## Planning Rules

**Research before planning. Spike before building. Test with real SDK before declaring done.**

- Any plan step that uses an SDK feature must cite a working spike or prior usage proving it works
- If a plan says "use `mcp_servers`" or "use `allowed_tools`" or any SDK param — that param must be verified with a real `query()` call before the plan is approved
- Delegated agents (team members) must run real tests, not just mocked ones. If an agent reports "all tests pass" from mocked tests only, that is not completion
- "Tests passing" with `-m "not e2e"` is not an acceptable verification. The full suite must pass
- **Every implementation plan MUST include a verbatim copy of the original user request.** This enables downstream agents (especially the Eval Guardian) to verify that the user's full intent was captured and implemented. Plans without the original request are incomplete.
- **The Eval Guardian patrols intent, not just eval quality.** The guardian reads the original user request in the plan and verifies every point was addressed by the implementation and evals. The guardian is empowered to criticize the plan itself, flag missing requirements, and add tasks. The guardian is the user's advocate — it ensures what was asked for is what was built.

## Git

Two repos, don't confuse them:
- **This dir** (`/Users/breedoon/Documents/obs/`) — runtime code, normal git workflow
- **Vault** (`T/`) — has its own git, Obsidian Git auto-commits every 15 min. Agent makes explicit descriptive commits after meaningful operations.
