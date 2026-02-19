# OBS Agent Architecture

## Overview

An agentic personal assistant using the Claude Agent SDK for Python, with this Obsidian vault as its knowledge base. Runs locally, processes requests via CLI (Telegram later), maintains a structured knowledge graph of goals, decisions, people, meetings, and context.

## Core Principles

1. **The vault is a knowledge graph, not a data lake** - store pointers with context, not raw bulk data
2. **Agent knowledge lives inside the vault** (as markdown); runtime code lives outside
3. **Universal document lifecycle** - files start small, grow, split into directories
4. **Lazy generation** - summaries and indexes built on first touch, not upfront
5. **Skills over code** - prefer markdown instructions guiding agent judgment over rigid scripts
6. **Git as safety net** - version control for catastrophic recovery, not for working memory

## File Split

| What | Where | Why |
|------|-------|-----|
| Python runtime (agent, tools, config) | `/Users/breedoon/Documents/obs/` | Traditional code, own git repo, not browsed in Obsidian |
| Agent knowledge (memory, skills, context) | Vault: `T/Agent/` | Needs Obsidian indexing, backlinks, search |
| Session transcripts (JSONL) | SDK default (`~/.claude/projects/`) | Large, not useful to browse in Obsidian |
| Vault content (journal, notes, meetings) | Vault: `T/` (existing structure) | Already there, stays as-is |

## File Access

- **Direct filesystem** for most reads/writes. Obsidian's file watcher picks up changes automatically.
- **Obsidian CLI/URI** only for creating files from Templater templates (daily/weekly/monthly journal entries that use `tp.*` syntax).
- Agent has full read/write access to the vault directory.

## Session Management

- Leverage Claude Agent SDK's built-in prompt caching (~1 hour window)
- Within cache window: continue previous session seamlessly
- Cache expired: start fresh session, load `Agent/context.md` + core skills
- **Auto-offboarding**: before cache expiry, a skill triggers persisting important context to vault files
- **Context compaction**: when approaching context limits, summarize current conversation and persist key information
- Goal: feels like one long-running session to the user

## Memory System

### Core Context (`Agent/context.md`)
Always loaded at session start. Contains current priorities, active threads, recent decisions, index of topic files. This is the root - everything is reachable from here.

### Topic Files (`Agent/topics/`)
Created on demand when a section in `context.md` exceeds ~15-20 lines. Standard structure:
- **Current** section: active state
- **History** section: append-only timestamped changes with links to relevant vault files
- **References** section: contextual wiki links

History is explicit in the files (append-only), not reliant on git archaeology.

### Wiki-Style Linking
All references use contextual wiki links: `[[path/to/file|why this is relevant]]`. Obsidian backlinks provide reverse navigation.

## Summary & Indexing

### Parent Notes
Each content directory has a sibling parent note (e.g., `Meeting Notes.md` next to `Meeting Notes/`). Parent notes contain:
- Conventions for the directory
- Curated one-line summaries (lazy-appended when agent touches a file)
- Archive links for overflow

### Three-Tier Detail

| Tier | Detail | Location | When |
|------|--------|----------|------|
| 0 | One-line summary | Parent note | On first touch (MVP) |
| 1 | Detailed summary (~paragraph + key points) | `_summaries.md` in directory | On touch if original > 2KB (post-MVP) |
| 2 | Full original | The file itself | Already exists |

### Reference Cards
For external data (sessions, articles, videos) that has no native vault markdown: a reference card in the vault with type, source, capture date, summary, and links. Reference cards follow the same universal lifecycle.

## Git & Version Control

Two-layer commit strategy:

1. **Obsidian Git plugin** - auto-commits every 15 minutes when Obsidian is open. Background safety net for manual edits. Message: `vault backup: YYYY-MM-DD HH:mm:ss`.
2. **Agent commits** - explicit commits with descriptive messages after meaningful operations (session offboard, document processing, context updates).

Storage impact is negligible: git delta-compresses text diffs efficiently. Commit frequency has near-zero effect on `.git` size - what matters is total new content created, not how often it's captured.

`.gitignore` excludes only `.DS_Store`. Everything else (including `.obsidian/` config and `.trash/`) is tracked.

## Skills System

### Core Skills (always referenced in system prompt)
1. `update-context.md` - Persist session learnings to context and topic files
2. `manage-summaries.md` - Generate and append summaries to parent notes
3. `create-reference.md` - Create reference cards for external content
4. `file-conventions.md` - Master reference for all vault file conventions

### Deeper Skills (loaded on demand via core skill references)
- `split-topic.md`, `session-offboard.md`, `process-meeting.md`, `daily-planning.md`

### Routing
Core skills listed in system prompt with trigger descriptions. Each core skill references deeper skills for sub-tasks. Agent judgment determines which apply. Future: dedicated skill router from separate project.

## MVP Scope

### Build
1. Python project skeleton with Claude Agent SDK
2. `Agent/` folder: `context.md`, core skills, `drafts/`
3. Git setup for the vault (`.gitignore`, initial commit)
4. Session continuity via SDK caching + basic offboarding
5. `Misc/Meeting Notes.md` parent note as proof of concept

### Don't Build Yet
- Telegram bot
- People tracking
- Decision records (beyond this design log)
- Tier 1 detailed summaries
- External data pipelines
- Template versioning
- Embeddings
- Bulk indexing

### Success Criteria
- CLI session works, agent updates `context.md` appropriately
- Agent follows skills for file conventions
- Meeting notes get lazily summarized in parent note
- Sessions feel continuous within cache window
- Vault is under Git
- File structure is clean and navigable
