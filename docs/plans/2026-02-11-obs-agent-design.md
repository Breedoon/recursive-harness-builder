# OBS Agent - Personal AI Assistant Design

## 1. Project Overview

An agentic personal assistant that uses an Obsidian vault as its knowledge base. The system runs locally, processes requests via CLI (and later Telegram), and maintains a structured knowledge graph of the user's life - goals, decisions, people, meetings, and context.

**Core principles:**
- The vault is a knowledge graph, not a data lake
- Agent knowledge lives inside the vault (as markdown); runtime code lives outside
- Files follow a universal lifecycle: start small, grow, split
- Summaries and indexes are generated lazily (on first touch), not upfront
- Skills (markdown instructions) guide agent behavior; prefer loose instructions over rigid code
- Everything is version-controlled via Git

**Tech stack:**
- Claude Agent SDK for Python (`claude-agent-sdk`) - provides the agent loop, tool use, and CLI bundling
- Obsidian vault on iCloud (`/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T`)
- Python runtime in project directory (`/Users/breedoon/Documents/obs/`)
- Git for version control of both the vault and the project

**Reference implementations:**
- OpenClaw (`/Users/breedoon/Documents/JetBrainsProjects/PyCharm/P/OSS-watch/openclaw`) - personal AI assistant with memory system, multi-channel support, skills
- Additional repos in OSS-watch: `claude-mem`, `claude-context`, `claude_code_agent_farm`, `cli-agent-orchestrator`

---

## 2. Architecture

### File Split

| What | Where | Why |
|------|-------|-----|
| Python runtime (agent, tools, config) | `/Users/breedoon/Documents/obs/` | Traditional code, own git repo, not browsed in Obsidian |
| Agent knowledge (memory, skills, context) | Vault: `T/Agent/` | Needs Obsidian indexing, backlinks, search |
| Session transcripts | SDK default location | Large, not useful to browse in Obsidian |
| Vault content (journal, notes, meetings) | Vault: `T/` (existing structure) | Already there, stays as-is |

### File Access

- **Direct filesystem** for most reads/writes - Obsidian's file watcher picks up changes automatically
- **Obsidian CLI/URI** only for creating files from Templater templates (daily/weekly/monthly journal entries)
- Agent has full read/write access to the vault directory

### Session Management

- Sessions leverage the Claude Agent SDK's built-in prompt caching (~1 hour window)
- If within cache window: continue previous session seamlessly
- If cache expired: start fresh session, load `Agent/context.md` + core skills
- **Auto-offboarding**: before cache expiry, a skill triggers the agent to persist important context from the current session to vault files
- **Context compaction**: when approaching context limits, a skill triggers summarization of the current conversation and persistence of key information
- Goal: feels like one long-running session to the user

---

## 3. Vault File System Conventions

### Directory Structure (MVP)

```
T/                                    (Obsidian vault root)
  Agent/                              NEW - top-level, first-class section
    context.md                        Core context file, always loaded
    skills/                           Agent instruction files
    drafts/                           Temporary artifacts, partitioned by month
      2026-02/
    topics/                           Grows organically from context.md splits
  Ж/                                  Existing journal hierarchy (D/W/M/S/Y)
  Vault/                              Existing knowledge categories
  Misc/                               Existing miscellaneous
    Meeting Notes.md                  NEW - parent note for meeting notes
    Meeting Notes/                    Existing meeting transcripts
  Assets/                             Existing templates, scripts
```

### Naming Conventions

| Convention | Meaning | Examples |
|-----------|---------|---------|
| `_` prefix | System-managed file | `_summaries.md`, `_archive-001.md` |
| No prefix | User-authored or primary content | `decisions.md`, `goals.md` |
| Parent note = sibling of folder | Entry point for a directory | `Meeting Notes.md` next to `Meeting Notes/` |
| `YYYY-MM-DD` in filename | Date-stamped content | `2026-02-10 Call with Daniil.md` |

### Universal Document Lifecycle

Every document in the system (except raw immutable content like transcripts) follows the same lifecycle:

1. **Single file** - created with initial content
2. **Grows** - content appended over time via agent interactions
3. **Splits** - when too large, sections extracted into child files in a same-named directory
4. **Parent becomes hub** - retains summaries + links to children
5. **Overflow archives** - when parent note grows too large, older entries move to `_archive-001.md`, `_archive-002.md` (numbered, cutoff date noted inside)

Links to the parent note always work (`[[decisions]]` resolves to `decisions.md`). Following one extra hop to a child is acceptable.

### File Bloat Mitigation

- Default agent action: **extend an existing file**, not create a new one
- New files **must have a parent** - the agent must know which parent note links to this file
- Temporary artifacts go to `Agent/drafts/YYYY-MM/` - partitioned by month, no indexing
- Agent **never creates new top-level directories** without user approval
- `Agent/drafts/` files either get **promoted** (moved to a proper home) or decay into irrelevance

---

## 4. Memory System

### Core Context File (`Agent/context.md`)

Always loaded at session start. Contains:
- Current priorities and focus areas (3-5 lines)
- Active threads with links to topic files
- Recent decisions with links
- Index of existing topic files

This is the agent's "working memory root." Everything is reachable from here.

### Topic Files (`Agent/topics/`)

Created on demand when a section in `context.md` exceeds ~15-20 lines. Each topic file has:

```markdown
# [Topic Name]
Last updated: YYYY-MM-DD

## Current
Active state of this topic - what's happening now.

## History
### YYYY-MM-DD
- What changed and why (linked to [[relevant vault files]])

### YYYY-MM-DD
- Previous state change...

## References
- [[path/to/related/file|why it's relevant]] - contextual wiki links
```

**History is append-only within files.** When something changes, old state moves to History with a date stamp. The agent never deletes history - it just grows the History section. Git is the catastrophic safety net, but the agent's working memory of past states is explicit in the file.

### Wiki-Style Linking

Every reference uses contextual wiki links: `[[path/to/file|why this is relevant]]`

Links appear:
- Inline in prose (preferred - most natural, provides context)
- In References sections (for explicit relationship documentation)
- In parent notes (for navigation/indexing)

Obsidian backlinks provide reverse navigation automatically.

---

## 5. Summary & Indexing System

### Parent Notes (Lazy-Append Summaries)

Each directory of content has a sibling parent note (e.g., `Meeting Notes.md` for `Meeting Notes/`). Parent notes contain:

- **Conventions**: how files in this directory are structured/named
- **Curated summaries**: one-line summaries appended lazily when the agent touches a child file
- **Archive links**: when summaries overflow, older entries move to `_archive-NNN.md`

The parent note is **curated, not comprehensive.** Not every file needs to be listed. The agent uses filesystem search (grep/glob) for exhaustive queries.

### Three-Tier Detail System

| Tier | Detail Level | Location | Generated When |
|------|-------------|----------|----------------|
| 0 | One-line summary | Parent note | On first touch (always) |
| 1 | Detailed summary (~1 paragraph + key points) | `_summaries.md` in same directory | On first touch, if original > 2KB |
| 2 | Full original content | The file itself | Already exists |

**MVP: implement Tier 0 only.** Tier 1 is defined as a convention and skill, activated later when the system is running and the need is validated.

### Reference Cards (External Sources)

For data that doesn't have a native vault representation (session JSONL, articles, videos, emails, GitHub repos):

- A markdown reference card lives in the vault
- Contains: type, source URL/path, capture date, summary, links to related vault content
- Reference cards follow the same universal document lifecycle (can grow, split)
- High-volume streams (text messages, emails) are aggregated rather than individual cards

Reference cards are the **universal adapter** - always markdown, always linkable, always Obsidian-indexable, regardless of what they point to.

---

## 6. Skills System

### Structure

Skills are markdown files in `Agent/skills/`. They contain natural-language instructions that guide agent behavior for specific tasks.

### Core Skills (Always Referenced)

These are referenced in the agent's system prompt and checked on every interaction:

1. **`update-context.md`** - After meaningful conversations, update `context.md` and relevant topic files. Append history entries. Update links.

2. **`manage-summaries.md`** - When reading a file, check if a one-line summary exists in the parent note. If not, generate and append one. (Later: generate detailed summaries for large files.)

3. **`create-reference.md`** - When processing external content (session transcript, article, etc.), create or update a reference card. Follow the reference card conventions.

4. **`file-conventions.md`** - The master reference for vault file system conventions. How to name files, when to create vs extend, parent notes, archives, underscore prefix rules, draft folder usage.

### Deeper Skills (Referenced by Core Skills)

Loaded on demand when core skills determine they're needed:

- **`split-topic.md`** - How to extract a section from a file into its own child file
- **`session-offboard.md`** - How to persist context before session cache expires
- **`process-meeting.md`** - How to handle a meeting transcript (summarize, link people, extract action items)
- **`daily-planning.md`** - How to create/update daily/weekly/monthly plans using existing templates

### Skill Routing

- Core skills are listed in the agent's system prompt with trigger descriptions
- Each core skill references which deeper skills to consult for specific sub-tasks
- The agent uses its own judgment to determine which skills apply
- Strong encouragement in system prompt: "Before taking action, check if a skill applies"
- Future: dedicated skill router (from the user's separate project)

---

## 7. Git & Version Control

### Vault Git Setup

The vault already has `.git` initialized but appears inactive. Actions needed:

1. Create `.gitignore` for the vault (exclude `.obsidian/workspace*`, `.DS_Store`, `.trash/`, large binary files)
2. Initial commit of all existing vault content
3. Set up regular commit cadence (agent commits after significant changes, or via a periodic hook)

### Project Git Setup

The project directory (`/Users/breedoon/Documents/obs/`) gets its own git repo for the Python runtime code.

### iCloud Constraint

- Vault is on iCloud with limited storage
- Only text/markdown files in the vault - no binaries, no large files
- `.gitignore` must be strict about this
- Reference cards point to external files rather than copying content into the vault

---

## 8. Future Expansion (Post-MVP)

These features are explicitly deferred but the architecture accommodates them:

### Telegram Bot
- Thin interface layer over the same agent
- Voice messages transcribed (Whisper or similar) before processing
- Text + voice from day one when implemented
- The agent runtime handles all logic; Telegram is just I/O

### People Tracking System
- Person files with YAML frontmatter (name, relationship tier, how met, birthday, etc.)
- Template for person files
- Auto-linked meeting notes via speaker identification pipeline
- Relationship tiers reusing existing S/A/B/C system

### Decision Records
- ADR-style decision tracking
- `decisions.md` → `decisions/` lifecycle
- Links to journal entries and context that informed each decision

### External Data Sources
- Email ingestion → aggregated reference cards or absorbed into existing files
- Social media / chat history → reference cards per conversation
- Articles / videos → reference cards with summaries
- GitHub repos → point-in-time snapshot reference cards
- All handled by the reference card universal adapter pattern

### Template Versioning & Migration
- Track template changes over time
- Skills for assessing migration between template versions
- Agent-assisted migration scripts with user confirmation

### Embeddings
- Future addition for semantic search
- Not needed for MVP - filesystem search + lazy summaries provide sufficient findability

---

## 9. MVP Scope

### What We Build

1. **Python project skeleton** - Claude Agent SDK setup, basic agent that can interact via CLI
2. **`Agent/` folder in vault** - `context.md`, `skills/` with core skills, `drafts/`
3. **Core skills** (4 files) - update-context, manage-summaries, create-reference, file-conventions
4. **Git setup** for the vault - `.gitignore`, initial commit
5. **Session continuity** - leverage SDK caching, basic offboarding skill
6. **Parent note for Meeting Notes** - `Misc/Meeting Notes.md` as proof of concept

### What We Don't Build Yet

- Telegram bot
- People tracking system
- Decision records
- Tier 1 detailed summaries
- External data ingestion pipelines
- Template versioning
- Embeddings
- Bulk indexing

### Success Criteria

The MVP is successful when:
- You can start a CLI session, talk to the agent, and it updates `context.md` appropriately
- The agent follows skills to maintain file conventions
- Meeting notes can be discussed and get lazily summarized in the parent note
- Sessions feel continuous within the cache window
- The vault is under Git version control
- The file structure is clean and navigable
