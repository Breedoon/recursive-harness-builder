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

## Testing Philosophy

**Low confidence culture: assume code is broken until proven otherwise.**

### The Hard Rule

**NEVER declare a feature working based on mocked tests alone.** The verification command is:

```bash
# This is the ONLY command that proves the system works:
.venv/bin/pytest tests/ -q --tb=short  # ALL tests, including E2E — no -m filter
```

If you run `-m "not e2e"` you are running theater. 295 mocked tests passing while the real daemon crashes is not "tests passing" — it's lying. Do not report mocked test results as verification. Do not skip E2E tests to save time or credits. The user has explicitly authorized spending credits on real SDK tests.

### Spike Before You Build

**Before implementing any feature that uses an SDK API you haven't used before, write a 10-line spike script that proves the API actually works.** Run it. If it crashes, the feature cannot be built as planned.

Example of what should have happened before Step 5 (MCP tools):
```python
# spike_mcp.py — run this BEFORE writing tools.py
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions, query
# ... 10 lines proving mcp_servers actually works with query() ...
```

This would have caught the `ProcessTransport is not ready for writing` crash in 30 seconds instead of after 3 agents spent 20 minutes implementing and "testing" a broken feature. **Plans that use unverified SDK features are not plans — they are hopes.** Research comes before planning, not after shipping.

### Test Layers (all required for new features)

1. **Unit tests** — fast, mocked, test individual functions. Useful for refactoring safety and structure. **Not proof that anything works.**
2. **Integration tests** — TestClient or real uvicorn, mocked SDK. Tests HTTP wiring. **Still not proof.**
3. **Real E2E tests** — real uvicorn + real SDK + real HTTP, LLM-as-judge verification. **This is the proof.** Every new feature MUST have at least one.
4. **Terminal E2E with pexpect** — the highest-confidence layer. Spawns the real CLI, types messages, tests queuing/interrupt timing. Catches bugs all other layers miss (stdin threading, queue drain timing, race conditions).

### What Mocked Tests Are Good For

- Verifying argument parsing, config resolution, pure functions
- Refactoring safety (does the interface still match?)
- Fast CI feedback on structural regressions

### What Mocked Tests Cannot Do

- Prove the SDK accepts the options you're passing
- Prove the daemon doesn't crash on real requests
- Prove SSE streaming works end-to-end
- Prove anything about real system behavior

A test that mocks `query()` and asserts the mock returned what you told it to return is not a test. It's a tautology.

### Specific Rules

- **No API key gating** — SDK uses subscription auth. E2E tests should not check for ANTHROPIC_API_KEY.
- **LLM-as-judge** — For E2E tests with real SDK responses, use Haiku to evaluate response quality instead of brittle string matching.
- **Mocked tests create false confidence** — 295 passing mocked tests shipped a daemon crash on the very first real request. If a human would test it by typing in the terminal, write a pexpect test.
- Mock objects MUST match real SDK types — `mock_msg.content = [TextBlock(text="...")]` not `"string"`
- Full reference: `docs/testing-philosophy.md`

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
