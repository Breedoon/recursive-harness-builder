# claude-mem Research

## Source
- Repository: `/Users/breedoon/Documents/JetBrainsProjects/PyCharm/P/OSS-watch/claude-mem`
- Sub-agent session: acc1337 (from [[Agent/system/sessions/2026-02-11-initial-design|initial design session]])

## What It Is

A memory layer for Claude Code / Cursor that adds persistent memory across sessions. Uses a separate "memory observer" AI agent that watches the primary session and extracts knowledge.

## Architecture: Memory Observer Pattern

The key insight: claude-mem runs a **completely separate Claude agent** alongside the primary session. This observer:
- Receives tool execution events via lifecycle hooks
- Generates structured observations (categorized knowledge)
- Persists findings to SQLite + Chroma (vector search)
- Injects relevant past context into new sessions

The observer has its own identity prompt:
> "You are Claude-Mem, a specialized observer tool for creating searchable memory FOR FUTURE SESSIONS. CRITICAL: Record what was LEARNED/BUILT/FIXED/DEPLOYED/CONFIGURED, not what you (the observer) are doing."

## 5-Stage Lifecycle

1. **sessionInit** (before first prompt) - initialize observer session
2. **contextInject** (before prompt submit) - inject past context into primary session
3. **observation** (after tool use) - feed tool results to observer for knowledge extraction
4. **summary** (on stop) - observer generates session summary
5. **cleanup** (on stop) - finalize and persist

## Observation Types (6 categories)

| Type | Meaning |
|------|---------|
| bugfix | Something was broken, now fixed |
| feature | New capability or functionality added |
| refactor | Code restructured, behavior unchanged |
| change | Generic modification (docs, config, misc) |
| discovery | Learning about existing system |
| decision | Architectural/design choice with rationale |

## Knowledge Concepts (7 categories)

| Concept | Meaning |
|---------|---------|
| how-it-works | Understanding mechanisms |
| why-it-exists | Purpose or rationale |
| what-changed | Modifications made |
| problem-solution | Issues and their fixes |
| gotcha | Traps or edge cases |
| pattern | Reusable approach |
| trade-off | Pros/cons of a decision |

## Structured Output Format

Each observation is XML:
```xml
<type>decision</type>
<title>Short title</title>
<subtitle>One sentence, max 24 words</subtitle>
<facts>3+ concise, self-contained statements</facts>
<narrative>Full context - What was done, how, why</narrative>
<concepts>2-5 knowledge-type categories</concepts>
<files_read>All files touched</files_read>
<files_modified>All files modified</files_modified>
```

## Skip Logic

The observer knows to skip:
- Empty status checks
- Package installations with no errors
- Simple file listings
- Repetitive operations already documented
- File research with empty results

## Session Continuity

Uses **dual session IDs**:
- `contentSessionId` - stable, from the primary Claude Code session
- `memorySessionId` - the observer's own SDK session

The observer only resumes if its session is still warm AND it has processed more than 1 prompt.

## Worker Service

Runs on port 37777, HTTP API:
- `POST /api/sessions/init`
- `POST /api/sessions/observations`
- `POST /api/sessions/summarize`
- `GET /api/context/inject?project=...`

Storage: SQLite + Chroma for hybrid keyword + semantic search.

## Mode Configuration

Modes are JSON files supporting **inheritance via parent--override pattern**:
- `code` (base mode)
- `code--ko` (Korean override, deep-merges with base)

Allows localization and specialization without duplicating the full config.

## Relevance to OBS Agent

**What to adopt:**
- Observation categories (bugfix/feature/refactor/change/discovery/decision) as a starting taxonomy for what to persist
- Knowledge concepts as a way to tag memories for retrieval
- Skip logic for filtering noise
- The principle of a dedicated "decide what to remember" step

**What we're doing differently:**
- No separate daemon observer agent. Instead, fork the session on Stop to extract memories.
- Markdown files in the vault instead of SQLite/Chroma
- Skills-based instructions instead of mode JSON files
- Vault's wiki link graph instead of vector search (for MVP)
