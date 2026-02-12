# Design Intent Reference

Extracted from the original design session transcript (`9dd1f79f-d961-4c3a-9eea-e003569eccbe.jsonl`). This document captures user preferences, implicit expectations, changes in thinking, and nuances that may not be fully captured in the vault design docs.

---

## 1. User Preferences and Style Expectations

### Communication Style
The user wants the agent to be a **thinking partner, not a search engine**. From the system prompt structure discussed:

> "Be genuinely helpful, not performatively helpful. No filler."
> "Have opinions. Be a thinking partner, not a search engine."
> "Be resourceful before asking. Try to find it in the vault first."
> "Do not narrate routine tool calls. Narrate only when it helps."

These come from OpenClaw's SOUL.md pattern but were explicitly adopted and endorsed. The agent should:
- Have opinions and share them proactively
- Search the vault before asking the user for information
- Proactively connect dots when the user mentions people, projects, or decisions
- Keep narration brief and value-dense

### Decision-Making Involvement
The user pushed back multiple times when the assistant made decisions silently. Key quotes from feedback on the first plan draft:

User rejected the first implementation plan because:
- "Too many assumptions made silently"
- "Wanted to see research findings first"
- "Plan should be in the vault, not .claude/plans/"

The user wants to be consulted on design choices, not surprised by them. The assistant should propose options with recommendations rather than deciding unilaterally.

### Simplicity Over Cleverness
The user repeatedly pulled conversations back to simplicity:
- Rejected the `_index.md` vs parent note duality in favor of just parent notes everywhere
- Combined reference cards and indexes into one universal document lifecycle
- Dropped calendar-year archive naming for simpler sequential numbering
- Said "I feel like I'm thinking way, way ahead" about template versioning and explicitly deferred it

The user values pragmatic "good enough" solutions that can evolve, not comprehensive upfront systems.

### Opinionated But Iterative
The user wants strong defaults that can be changed later:
> "I will want to make a choice later on that we need multiple decision files or one decision file. And then I will want to migrate everything to that format, which is why I want Git."

Git is the safety net that makes bold decisions reversible. This philosophy should inform implementation: make decisive choices, document them, and trust Git to enable future changes.

---

## 2. Specific Implementation Details

### SDK Usage Patterns

**Fork-based architecture (D018)**: The core primitive is `fork_session=True` from the Claude Agent SDK, not native subagents. Key reasons:
- Forks reuse the KV cache (near-zero cost)
- Subagents cannot spawn sub-subagents (SDK limitation)
- Python manages fork lifecycle, giving full control
- System prompt must match exactly for cache reuse; forks inherit it

**Three fork use cases**:
1. **Skill classification** (UserPromptSubmit hook): Fork receives the user message + skill manifest, returns needed skill names. Python reads SKILL.md files, injects as system message.
2. **Vault search**: Fork searches the vault, returns structured summary with excerpts, file links, relevance explanations, and temporal context.
3. **Memory extraction** (Stop/PreCompact hook): Fork extracts memories, distributes to context.md/topics/journal, writes daily log, creates session card, commits.

**ForkRunner is generic**: All forks go through a single `ForkRunner` that takes a task prompt, allowed tools, and a result handler. The three fork types above are preset configurations, not separate implementations.

**Fork classification flow** (from assistant MSG 77):
```
User message arrives
    -> UserPromptSubmit hook fires
    -> Fork to classify: "What skills does this message need?"
    -> Fork reads skill manifest, returns skill names + scope
    -> If skills needed: read SKILL.md files, inject as system message
    -> Main session processes message with skills injected
```

**Session resumption**: The SDK's prompt cache lasts ~1 hour. The Session Manager tracks session_id and resumes within a ~58-minute window. After timeout, starts a fresh session loading updated context.md and skills.

**No compaction (D022)**: When approaching context limits:
1. PreCompact hook fires
2. Fork to extract memories (synchronous, blocks)
3. Fork writes to vault, completes
4. PreCompact returns `continue_: False` -- agent stops
5. Daemon detects stop reason, starts new session with fresh context.md

### System Prompt Structure

The system prompt is assembled by Python from vault files at session start:

```
[Identity: "You are [name TBD], a personal AI assistant. Your memory
and knowledge live in this Obsidian vault. Each session, you start
fresh. Agent/context.md is your memory. Read it. Update it. It's
how you persist."]

[Contents of Agent/context.md]

[Core Behavior rules]

[Core Skills: file-conventions, update-context, manage-summaries, create-reference]

[Operational Skills manifest: names + triggers only, loaded on demand via fork]

[Safety section]
```

Key: The context.md contents are embedded IN the system prompt, not just referenced. Core skills are embedded too. Operational skills are listed by name/trigger only.

### Daemon + CLI Architecture

- FastAPI daemon running on localhost (port mentioned as 7832 in plan)
- SSE streaming for responses
- CLI is a simple REPL that auto-starts the daemon
- HTTP chosen over other IPC because "you can curl it"
- One SDK client per daemon (not per-request)

### Metrics and Monitoring

- Token usage (input/output, cached/non-cached) tracked in Python log files
- Cache hit/miss detection via JSONL transcript analysis
- Alarm on unexpected cache miss (e.g., fork that should reuse cache didn't)
- Metrics stay in Python logs, NOT in the vault (D023)
- Dollar cost tracking is irrelevant (subscription model)

---

## 3. Changes in Thinking (Design Evolution)

### Memory Model Evolution
**Early**: User mentioned wanting "embeddings" and "map-reduce paradigm" for managing a large corpus.
**Final**: Explicitly deferred embeddings. Vault-native markdown files with lazy indexing via parent notes. The "map-reduce" concept evolved into the tiered summary system (Tier 0 one-liners in parent notes, Tier 1 detailed in `_summaries.md`, Tier 2 full originals).

### Indexing Evolution
**Early**: The assistant proposed `Agent/indexes/` as a separate folder.
**Mid**: User pushed for co-located indexes inside each directory.
**Final**: Dropped `_index.md` entirely. Parent notes (sibling to their folder) serve as curated hubs, not exhaustive indexes. Filesystem search handles exhaustive queries.

### Reference Cards vs Indexes
**Early**: Assistant proposed reference cards as a distinct concept from indexes.
**Mid**: User pointed out the artificial distinction -- a reference card that grows eventually becomes an index.
**Final**: Universal document lifecycle -- everything starts small, grows, splits. Reference cards and indexes are just different lifecycle stages of the same thing.

### Skill Loading
**Early**: "Judgment-based routing via system prompt encouragement" (D015 original)
**Later**: User pushed for MORE deterministic loading. Fork-based classification is the result (D019). OpenClaw's approach of "scan skills, if one clearly applies, read it" was explicitly considered but deemed insufficient because "skills are the backbone of vault consistency -- they must run when needed."

### Subagents vs Forks
**Early plan**: Used SDK's native subagent system.
**User correction**: Explicitly said NOT to use native subagents. Use `fork_session=True` instead. Reasons: forks reuse cache, Python controls lifecycle, no sub-subagent limitation.

### Plan Location
**First attempt**: Plan written to `.claude/plans/structured-meandering-robin.md`
**User rejection**: "Plan should be in the vault, not .claude/plans/"
**Final**: `T/Agent/system/implementation-plan.md` -- in the vault under the Agent system folder.

### Code Sketches in Plan
**First plan**: Had detailed Component Specifications with class signatures, method parameters, FastAPI port numbers.
**User feedback**: "I didn't really review the skeleton per se. A lot of it is semi-arbitrary decisions that I haven't approved."
**Final**: Stripped code sketches, kept only component responsibilities + key decisions. Implementation team makes their own code design choices.

### Compaction Strategy
**Early**: Assumed SDK compaction would be used.
**User quote**: "I think we will not do compacting. Or if we do, we're going to have our own prompt for compacting to integrate with our memory system."
**Final**: No compaction at all. Flush memories via fork, restart fresh (D022).

---

## 4. Skill Behavior Expectations

### Core Skills (Always Loaded)
These four are embedded in the system prompt every session:
1. **file-conventions** -- "The law." All vault operations defer to this.
2. **update-context** -- Post-interaction persistence to context.md + topics.
3. **manage-summaries** -- Lazy-append one-liners to parent notes.
4. **create-reference** -- Reference cards for external content.

### Operational Skills (Loaded on Demand via Fork)
These seven are loaded when the classification fork determines they're needed:
5. **split-document** -- When files grow past thresholds.
6. **session-offboard** -- On session end, captures everything.
7. **vault-search** -- How to find things (search chain: context.md -> parent notes -> glob -> grep -> backlinks).
8. **git-commit** -- Descriptive commits after meaningful operations.
9. **process-meeting** -- Meeting transcript handling (summarize, extract, link). NEVER modifies transcripts.
10. **ingest-content** -- Process any external content.
11. **daily-planning** -- Planning with journal template hierarchy (D/W/M/S/Y).

### Key Skill Rule: Immutability
Meeting transcripts in `Misc/Meeting Notes/` are IMMUTABLE. The agent must NEVER edit them. Indexing happens externally (parent notes, summaries). The PreToolUse hook enforces this.

### Skill Metadata Format (from agentskills.io spec)
Skills use YAML frontmatter with:
- `name`: matches directory name (kebab-case)
- `description`: trigger keywords for routing
- `metadata.priority`: "core" or "operational"
- `metadata.version`: for tracking changes
- `metadata.triggers`: human-readable activation description
- `metadata.dependencies`: space-delimited list of referenced skills

### Template Convention (Rule 11)
All structural templates live in `Assets/Templates/`, not inlined in skills. Skills reference templates via wiki links. Templates created: `session-card.md`, `topic-file.md`, `reference-card.md`, `tier1-summary.md`.

---

## 5. Edge Cases Discussed

### File Bloat
The user is deeply concerned about agent-created file bloat. Mitigation rules:
- Default action is to EXTEND an existing file, not create new
- New files must have a "home" (a parent note that links to them)
- Temporary artifacts go in `Agent/drafts/YYYY-MM/`, periodically cleaned
- Agent should never create new top-level directories without user approval

### iCloud Storage Constraint
The vault is on iCloud with limited storage. Implications:
- No heavy binary files in the vault
- Git delta compression is fine for text (5-7MB/year growth)
- Reference cards point TO external data, don't copy it in

### Cache Miss on Fork
If a fork doesn't hit the KV cache as expected (detected by unexpectedly high input token counts), the system should:
- Log a warning
- Inject alert into next main session turn
- Log in session reference card
- Eventually: send Telegram notification

### Pre-Compaction Race
The PreCompact hook must complete the memory extraction fork SYNCHRONOUSLY before returning. The fork blocks, writes to vault, completes, THEN PreCompact returns `continue_: False`. Otherwise, memories could be lost.

### Session Restart User Experience
When restarting after context flush:
- User experiences a brief interruption
- New session loads freshly updated context.md
- Should feel like "one long-running session" despite the restart

### What Happens with Very Old Files
Files that were never touched by the agent stay un-indexed forever. The vault search skill can still find them via glob/grep. If the user asks about them, the agent reads them, answers, AND indexes them (lazy-append to parent note).

---

## 6. Testing Expectations

### TDD is Non-Negotiable
The user was emphatic about test-driven development:
> "I will want to be heavily test driven so that we will first have an agent run and write tests and also maintain those tests and make sure those tests are meaningful."

### Test Quality Over Coverage
> "I want to have really, really solid testing... I don't want just test coverage or code coverage, but also like an end to end test."

### Meaningful E2E Tests
The user wants E2E tests that verify actual agent behavior, not just unit tests of Python glue code. But:
> "I can't have too many of them because it's going to defeat the purpose. It's going to be too expensive."

So: ~5-10 E2E scenarios against a fixture vault, using a cheaper model (Haiku suggested).

### Fixture Vault
A copy of the real vault used for testing. Fresh git init per test session. Gitignored from the project repo. Created via `scripts/setup_fixture_vault.sh`.

### What to Test E2E
From the verification checklist:
1. Daemon starts, health check passes
2. CLI auto-starts daemon and connects
3. Basic chat reads context.md, responds coherently
4. Skill classification fork identifies needed skills
5. Vault search fork returns structured summary
6. Vault write follows conventions (update context.md properly)
7. Immutable file guard blocks meeting transcript edits
8. Session resume within cache window
9. Memory extraction on stop writes daily log + updates context
10. Pre-compaction triggers flush and restart
11. Token counts and cache hits tracked in logs

---

## 7. Personality/Tone of the Agent Being Built

The agent's personality is inspired by OpenClaw's SOUL.md but adapted:

### Core Identity
- "You are [name TBD], a personal AI assistant"
- "Your memory and knowledge live in this Obsidian vault"
- "Each session, you start fresh. Agent/context.md is your memory. Read it. Update it. It's how you persist."

### Behavioral Directives
From the system prompt structure:
- **Genuinely helpful**: No performative helpfulness or filler
- **Opinionated**: Have and share opinions, be a thinking partner
- **Resourceful**: Try to find answers in the vault before asking
- **Proactive**: When people/projects/decisions are mentioned, search for context and share connections
- **Quiet by default**: Don't narrate routine tool calls
- **Bold internally, careful externally**: Be aggressive with vault operations, cautious with anything visible outside

### Safety Boundaries
- No independent goals beyond helping the user
- If instructions conflict, pause and ask
- Never modify immutable files (meeting transcripts)

### What the Agent is NOT
- Not a chatbot -- it's an agent with tools
- Not a search engine -- it's a knowledge manager
- Not a passive assistant -- it proactively connects dots and maintains the vault

---

## 8. Vault Conventions Summary

These are the "laws" the agent must follow (from file-conventions skill):

1. **Parent note pattern**: `Meeting Notes.md` sits next to `Meeting Notes/` folder
2. **Underscore prefix** for system-managed files (`_archive-001.md`, `_summaries.md`)
3. **Universal lifecycle**: file -> grows -> splits into folder -> parent becomes hub -> archives overflow
4. **Extend before create**: Default action is to add to existing files
5. **New files need a home**: Must have a parent note that links to them
6. **Contextual wiki links**: Always `[[path|why relevant]]`, never bare links
7. **Immutable files**: Never edit `Misc/Meeting Notes/` transcripts
8. **Drafts partitioned by month**: `Agent/drafts/YYYY-MM/`, no indexing
9. **No new top-level directories** without user approval
10. **YAML frontmatter**: `YYYY-MM-DD HH:MM:SS` format, kebab-case keys, no T separator
11. **Templates in `Assets/Templates/`**: Skills reference, don't inline

### Two Git Repos (Important!)
- **Project dir** (`/Users/breedoon/Documents/obs/`) -- runtime code, normal git
- **Vault** (`T/`) -- its own git, Obsidian Git auto-commits every 15 min, agent makes explicit descriptive commits after meaningful operations

---

## 9. Architecture Decisions Not Fully Captured Elsewhere

### Why HTTP over WebSocket/Unix Socket
The assistant recommended HTTP on localhost because "it's easier to debug (you can curl it) and Python has great async HTTP server support." Since Telegram (future) polls rather than maintains a persistent connection, the transport between CLI and daemon is irrelevant for that extension.

### Why Not OpenClaw's memory_search Tool Approach
OpenClaw does NOT inject memory into the system prompt. Instead, the agent has `memory_search` and `memory_get` tools, and the system prompt says "Before answering anything about prior work, decisions, dates, people, preferences, or todos: run memory_search." Our approach (D025) preloads context.md to avoid cold-start latency.

### Daily Memory Log as Ledger
`Agent/memory/YYYY-MM-DD.md` is not a brain dump. It's a ledger: what was learned, with links to where information was actually placed in the vault. Useful for debugging ("why did the agent update this topic file?") and for review.

### Proactive Behavior: System Prompt + Skill
Core proactive instructions live in the system prompt (always active). Detailed patterns (temporal awareness, conflict detection, connection-making) live in a dedicated `proactive-behavior` skill loaded for complex interactions. This follows D024.

### The Classification Fork Optimization
The user and assistant discussed a future optimization where the classification fork runs in PARALLEL with the main session starting, and injects skills mid-turn if needed. For MVP, classification is sequential (fork completes before main session processes).

---

## 10. Open Questions and Deferred Decisions

These were discussed but explicitly deferred:

1. **Agent name** -- "name TBD" in the system prompt
2. **Telegram connector** -- deferred post-MVP
3. **People tracking system** -- deferred post-MVP
4. **Embeddings/vector search** -- deferred, vault search via fork is MVP
5. **Template versioning and migration** -- user explicitly said "way ahead" and deferred
6. **Tier 1 detailed summaries** -- convention defined, activation deferred
7. **Speaker identification in meeting transcripts** -- deferred, user mentioned wanting this
8. **Decision records format** -- user unsure if one file or many; deferred
9. **Parallel fork classification** -- future optimization, MVP is sequential
