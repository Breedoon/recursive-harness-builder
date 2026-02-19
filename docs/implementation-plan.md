# OBS Agent MVP - Implementation Plan

## Context

We're building a personal AI assistant backed by this Obsidian vault. The design phase produced: [[Agent/system/architecture|architecture]], [[Agent/system/conventions|conventions]], [[Agent/system/decisions|25 design decisions]], and [[Agent/skills|11 skills]]. Research on [[Agent/system/research/openclaw|OpenClaw]], [[Agent/system/research/claude-mem|claude-mem]], and [[Agent/system/research/claude-sdk|Claude Agent SDK]] informed the approach.

**What the MVP delivers**: A daemon with HTTP API, CLI client, and an agent that maintains a knowledge graph in this vault — with session forking for search, skill classification, and memory extraction.

**What it does NOT build**: Telegram, people tracking, embeddings/vector search, Tier 1 summaries, external data pipelines.

---

## Architecture

```
┌────────────┐     HTTP/SSE      ┌──────────────────────────────────────────┐
│  CLI Client │◄─────────────────►│           Daemon (FastAPI)               │
│  (obs CLI)  │   localhost:7832  │                                          │
└────────────┘                    │  Session Manager                         │
                                  │  Fork Runner (generic)                   │
                                  │  System Prompt Builder                   │
                                  │  Hooks: PreCompact, Stop, PreToolUse     │
                                  │  Metrics Logger (tokens, cache)          │
                                  └──────────────────────────────────────────┘
                                              │
                                              │ Claude Agent SDK (subprocess)
                                              ▼
                                  ┌──────────────────────────────────────────┐
                                  │  Obsidian Vault (T/)                     │
                                  │  Agent/context.md   (always loaded)      │
                                  │  Agent/skills/      (loaded via forks)   │
                                  │  Agent/memory/      (daily memory logs)  │
                                  └──────────────────────────────────────────┘
```

### Data Flow: User Message

1. User sends message via CLI → daemon HTTP API
2. **UserPromptSubmit hook** fires:
   a. Fork to classify: "What kind of request is this? What skills are needed?"
   b. Fork reads skill manifest, returns skill names + scope assessment
   c. If skills needed: read SKILL.md files from disk, inject as system message
3. Main session processes message with skills injected
4. Agent uses built-in tools (Read/Write/Edit/Glob/Grep/Bash) on the vault
5. Agent may invoke vault search (triggers another fork for structured search)
6. Response streamed back via SSE

### Data Flow: Session End

1. **Stop hook** fires (or **PreCompact** if approaching context limit)
2. Fork for memory extraction:
   a. Review conversation, identify decisions/information/actions/threads
   b. Write daily memory log to `Agent/memory/YYYY-MM-DD.md`
   c. Distribute updates to context.md, topic files, journal
   d. Create session reference card
   e. Git commit via Bash
3. If PreCompact: prevent compaction, restart with fresh session
4. If Stop: session ends cleanly, daemon ready for next interaction

---

## Components

The daemon has these logical components. Design decisions [[Agent/system/decisions|D018-D025]] explain the rationale for each.

| Component | Responsibility | Key Decision |
|-----------|---------------|-------------|
| **Fork Runner** | Generic mechanism for forking sessions. All forks (skill classification, vault search, memory extraction) go through this. | D018: Forks reuse KV cache, more flexible than subagents |
| **System Prompt Builder** | Reads `Agent/context.md` + core skills from vault, assembles the system prompt. Inspired by [[Agent/system/research/openclaw\|OpenClaw's SOUL.md pattern]]. | D025: context.md is the orientation document |
| **Session Manager** | Tracks session_id, decides resume vs fresh start (~58 min window), builds SDK options. | D014: SDK cache for continuity |
| **Hooks** | PreToolUse guards immutable files. Stop/PreCompact trigger memory extraction forks. | D022: No compaction, flush and restart |
| **Daemon** | HTTP API (SSE streaming). Manages one SDK client, session lifecycle, fork execution. | |
| **CLI** | Simple REPL. Auto-starts daemon. Streams SSE responses. | |
| **Metrics Logger** | Token usage, cache hits/misses, fork stats. Python logs, not vault. | D023: Operational metrics outside vault |

---

## Implementation Steps (TDD)

Each step: write failing test → implement → refactor.

### Step 0: Project Scaffold
- `pyproject.toml` with dependencies (claude-agent-sdk, fastapi, uvicorn, httpx, sse-starlette, pytest, pytest-asyncio)
- `src/obs_agent/` package structure
- `tests/` with conftest
- `scripts/setup_fixture_vault.sh` (copies real vault for testing)
- Create `Agent/memory.md` parent note and `Agent/memory/` directory

### Step 1: Config
- Vault path resolution (real + fixture vault)
- Skill paths, daemon settings

### Step 2: System Prompt Builder
- Reads context.md + core skills from vault
- Assembles prompt with identity, context, behavior, skills, safety sections
- See [[Agent/system/research/openclaw|OpenClaw research]] for prompt structure inspiration

### Step 3: Fork Runner
- Generic `run()` that forks a session with a task prompt
- Preset methods for the three fork types (classify, search, extract)
- See [[Agent/system/research/claude-sdk|SDK research]] for `fork_session` API

### Step 4: Hooks
- PreToolUse guard: blocks immutable files, .env files
- Stop: triggers memory extraction fork
- PreCompact: same as Stop, then prevents compaction

### Step 5: Session Manager
- Tracks session_id from SDK init message
- Resume within ~58 min window, fresh after timeout
- Builds `ClaudeAgentOptions` integrating hooks + system prompt

### Step 6: Skill Classification Fork
- Fork classifies what skills a user message needs
- Returns skill names; Python reads SKILL.md files and injects

### Step 7: Vault Search Fork
- Fork searches vault, returns structured summary with excerpts, file links, temporal context
- Temporal weighting: recent files are more relevant

### Step 8: Memory Extraction Fork
- On stop/pre-compaction, fork extracts memories
- Writes daily log, distributes to context.md/topics/journal, creates session card, commits
- Follows [[Agent/skills/session-offboard/SKILL|session-offboard]] and [[Agent/skills/update-context/SKILL|update-context]]

### Step 9: Daemon Server
- HTTP API with SSE streaming
- Integrates session manager, fork runner, hooks

### Step 10: CLI Client
- REPL, auto-starts daemon, streams responses

### Step 11: Proactive Behavior Skill
- New skill at `Agent/skills/proactive-behavior/SKILL.md`
- Patterns for connecting dots, suggesting from past context, temporal awareness

### Step 12: E2E Tests
- Fixture vault (copy of real vault)
- Test: basic chat, vault write, session resume, immutable guard, skill classification, vault search, memory extraction

---

## Testing Strategy

**Unit tests** (fast, free): Config, prompt builder, hook logic, session manager. Mock ForkRunner for components that depend on it.

**Integration tests** (slow, paid): Real API calls against fixture vault. Mark with `@pytest.mark.e2e`. ~5-10 scenarios.

**Fixture vault**: Copy of real vault via `scripts/setup_fixture_vault.sh`. Fresh git init per test session. Gitignored.

---

## Team Execution

Split into tracks after Step 0:

- **Track A** (Steps 1-3): Config → Prompt Builder → Fork Runner
- **Track B** (Steps 4-8): Hooks → Session Manager → Skill Classification → Vault Search → Memory Extraction. Depends on Fork Runner interfaces from Track A.
- **Track C** (Steps 9-10): Daemon → CLI. Depends on Session Manager interfaces from Track B.
- **Track D** (Steps 11-12): Proactive skill → E2E tests. Depends on everything integrated.

Tracks A and B can overlap once Fork Runner interfaces are defined. C starts once Session Manager interface exists. D is last.

---

## Key References

| Concept | Vault File |
|---------|-----------|
| Full architecture | [[Agent/system/architecture]] |
| All decisions (D001-D025) | [[Agent/system/decisions]] |
| File conventions + vault map | [[Agent/skills/file-conventions/SKILL]] |
| Session offboard procedure | [[Agent/skills/session-offboard/SKILL]] |
| Update context procedure | [[Agent/skills/update-context/SKILL]] |
| Templates | `Assets/Templates/` (session-card, topic-file, reference-card, tier1-summary) |
| OpenClaw patterns | [[Agent/system/research/openclaw]] |
| claude-mem patterns | [[Agent/system/research/claude-mem]] |
| SDK API reference | [[Agent/system/research/claude-sdk]] |
| Project instructions | `/Users/breedoon/Documents/obs/CLAUDE.md` |

---

## Verification Checklist

1. **Daemon starts**: `/health` returns ok
2. **CLI connects**: REPL auto-starts daemon
3. **Basic chat**: Agent reads context.md, responds coherently
4. **Skill classification**: Fork identifies needed skills
5. **Vault search**: Fork returns structured summary with temporal context
6. **Vault write**: Agent updates context.md following conventions
7. **Immutable guard**: Agent blocked from editing meeting transcripts
8. **Session resume**: Second message within cache window resumes
9. **Memory extraction**: On stop, fork writes daily log + updates context.md
10. **Pre-compaction restart**: Approaching limit → flush → fresh session
11. **Metrics logging**: Token counts and cache hits tracked in Python logs
