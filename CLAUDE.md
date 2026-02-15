# OBS Agent

Personal AI assistant backed by an Obsidian vault. Uses the Claude Agent SDK for Python.

## Paths

| What | Path |
|------|------|
| **Runtime code** (this dir) | `/Users/breedoon/Documents/obs/` |
| **Obsidian vault** | `/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T` |
| **Vault shorthand** | `T/` in all vault references below |

## Vault Documentation (READ THESE)

All design docs, skills, and the implementation plan live in the vault — not here.

| Document | Vault Path | What It Contains |
|----------|-----------|-----------------|
| **Implementation plan** | `T/Agent/system/implementation-plan.md` | Architecture, component specs, 12 TDD steps, team tracks, verification checklist |
| **Architecture** | `T/Agent/system/architecture.md` | Full system design |
| **Design decisions** | `T/Agent/system/decisions.md` | 25 decisions (D001-D025) with rationale |
| **File conventions** | `T/Agent/skills/file-conventions/SKILL.md` | Vault directory map, all 11 rules, templates |
| **Session offboard** | `T/Agent/skills/session-offboard/SKILL.md` | Memory extraction procedure |
| **Update context** | `T/Agent/skills/update-context/SKILL.md` | How to persist to context.md |
| **All skills** | `T/Agent/skills.md` | Parent note listing all 11 skills |
| **OpenClaw research** | `T/Agent/system/research/openclaw.md` | Memory, prompts, session management patterns |
| **claude-mem research** | `T/Agent/system/research/claude-mem.md` | Observer pattern, knowledge extraction |
| **Claude SDK research** | `T/Agent/system/research/claude-sdk.md` | Hooks, sessions, forking, MCP tools API |
| **Context** | `T/Agent/context.md` | Agent's current brain state |

## Implementation

**Start here**: Read `T/Agent/system/implementation-plan.md`. It has everything:
- Architecture diagram and data flows
- Component specifications with code sketches
- 12 implementation steps in TDD order (Step 0 is the project scaffold)
- Team execution tracks (A: core infra, B: agent behavior, C: server+client, D: polish)
- Verification checklist

**Key SDK reference** (`claude-agent-sdk` v0.1.35):
- `ClaudeSDKClient` for daemon (interactive multi-turn), `query()` for forks (unidirectional)
- Hooks: PreToolUse, PostToolUse, UserPromptSubmit, Stop, PreCompact
- Forking: `ClaudeAgentOptions(resume=session_id, fork_session=True)`
- MCP tools: `@tool` decorator + `create_sdk_mcp_server()`
- Subagents cannot spawn sub-subagents (hence fork-based approach)

**Project structure** (to be created in Step 0):
```
/Users/breedoon/Documents/obs/
├── CLAUDE.md                    # This file
├── pyproject.toml               # Dependencies, entry points
├── src/obs_agent/               # Runtime Python code
│   ├── config.py                # Paths, constants
│   ├── prompt.py                # System prompt builder (reads vault)
│   ├── hooks.py                 # SDK hooks (offboard, guard, compact)
│   ├── session.py               # Session lifecycle (ClaudeSDKClient manager)
│   ├── daemon.py                # FastAPI server (uses ClaudeSDKClient via session)
│   └── cli.py                   # CLI client
└── tests/                       # Unit + E2E tests
```

## Vault Rules (for any vault file operations)

- **Extend before creating** — add to existing files by default
- **Parent note pattern** — `folder-name.md` sibling to `folder-name/`
- **Contextual wiki links** — `[[path|why relevant]]`, never bare links
- **Immutable files** — never edit `Misc/Meeting Notes/` transcripts
- **Templates** — structural templates live in `Assets/Templates/`, reference don't inline
- Full reference: `T/Agent/skills/file-conventions/SKILL.md`

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

### Running Tests

```bash
# Unit tests (fast, mocked):
.venv/bin/pytest tests/ -q -m "not eval and not integration"

# Live integration (real HTTP + SDK, no CLI):
.venv/bin/pytest tests/ -q -m integration --timeout=300

# Evals (real CLI + vault + SDK judge):
.venv/bin/pytest tests/evals/ -v -m eval --timeout=600

# EVERYTHING:
.venv/bin/pytest tests/ -v --timeout=600
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

## Planning Rules

**Research before planning. Spike before building. Test with real SDK before declaring done.**

- Any plan step that uses an SDK feature must cite a working spike or prior usage proving it works
- If a plan says "use `mcp_servers`" or "use `allowed_tools`" or any SDK param — that param must be verified with a real `query()` call before the plan is approved
- Delegated agents (team members) must run real tests, not just mocked ones. If an agent reports "all tests pass" from mocked tests only, that is not completion
- "Tests passing" with `-m "not e2e"` is not an acceptable verification. The full suite must pass

## Git

Two repos, don't confuse them:
- **This dir** (`/Users/breedoon/Documents/obs/`) — runtime code, normal git workflow
- **Vault** (`T/`) — has its own git, Obsidian Git auto-commits every 15 min. Agent makes explicit descriptive commits after meaningful operations.
