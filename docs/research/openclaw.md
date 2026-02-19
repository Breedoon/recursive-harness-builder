# OpenClaw Research

## Source
- Repository: `/Users/breedoon/Documents/JetBrainsProjects/PyCharm/P/OSS-watch/openclaw`
- GitHub: https://github.com/openclaw/openclaw (80K+ stars)
- Sub-agent sessions: ae4cefa (memory deep dive), ae5c774 (prompts & skills), ad2fabc (web research)
- All from [[Agent/system/sessions/2026-02-11-initial-design|initial design session]]

## What It Is

Self-hosted personal AI assistant (formerly ClawdBot/Clawd). Three-layer architecture: Gateway (sessions) -> Channel (platform adaptation) -> LLM (model abstraction). TypeScript, runs on Node.js.

Two core abstractions: (1) autonomous invocation (event-driven execution), (2) externalized memory (disk as source of truth, context as cache).

Community describes it as "like hiring a direct report who actually remembers things."

## Memory System

### Philosophy

> "The files are the source of truth; the model only 'remembers' what gets written to disk."

Markdown files are canonical. SQLite index is derived and always rebuildable from markdown.

### Storage Structure

```
~/.openclaw/workspace/
  MEMORY.md              # Curated long-term memory (small, core facts)
  memory/
    YYYY-MM-DD.md        # Daily logs (append-only narrative)
  bank/                  # "Typed" memory pages (stable, reviewable)
    world.md             # Objective facts
    experience.md        # What the agent did (first-person)
    opinions.md          # Subjective prefs/judgments + confidence + evidence
    entities/
      Peter.md
      warelay.md
```

- **MEMORY.md**: Core personality/preferences. Only loaded in private sessions (not group). Small.
- **Daily logs**: Append-only, today + yesterday loaded at session start.
- **Bank**: Curated by "reflection jobs" - entity summaries, opinion tracking with confidence scores.

### Retain / Recall / Reflect Loop

**Retain** (end of day or during): Add `## Retain` section to daily logs with 2-5 narrative, self-contained facts, tagged with type + entity mentions:
```
## Retain
- W @Peter: Currently in Marrakech (Nov 27-Dec 1) for Andy's birthday.
- B @warelay: Fixed Baileys WS crash by wrapping handlers in try/catch.
- O(c=0.95) @Peter: Prefers concise replies (<1500 chars) on WhatsApp.
```

Type prefixes: `W` (world), `B` (experience/biographical), `O` (opinion with confidence), `S` (observation/summary).

**Recall**: Hybrid search over derived SQLite index:
- Lexical (FTS5/BM25) for exact terms, names, commands
- Semantic (vector embeddings) for conceptual matches
- Temporal: "what happened around Nov 27"
- Entity: "tell me about X"
- Fusion: `score = 0.7 * vectorScore + 0.3 * textScore`

**Reflect**: Scheduled job that:
- Updates entity pages from recent facts
- Updates opinion confidence based on reinforcement/contradiction
- Proposes edits to MEMORY.md

### Memory Tools (Agent Interface)

The agent does NOT get all memory preloaded. Instead:

**`memory_search`**: "Mandatory recall step: semantically search MEMORY.md + memory/*.md before answering questions about prior work, decisions, dates, people, preferences, or todos; returns top snippets with path + lines."

**`memory_get`**: "Safe snippet read from MEMORY.md or memory/*.md with optional from/lines; use after memory_search to pull only the needed lines."

### Automatic Memory Flush (Before Compaction)

When session approaches context window limit:
```
if (sessionTokens >= contextWindow - reserveTokens - softThreshold) {
  // Inject silent agent turn
  User: "Pre-compaction memory flush. Store durable memories now
         (use memory/YYYY-MM-DD.md; create memory/ if needed).
         If nothing to store, reply with NO_REPLY."
  System: "Pre-compaction memory flush turn. The session is near
           auto-compaction; capture durable memories to disk."
}
```

Agent gets file tools (Write, Edit), writes memories, usually responds NO_REPLY (suppressed from user). Tracked by `memoryFlushCompactionCount`.

### Indexing (Background, Non-Blocking)

- File watcher (chokidar) with 1.5s debounce
- Chunking: ~400 tokens, 80 token overlap
- Embedding cache by content SHA-256 hash (avoids re-embedding unchanged text)
- Atomic reindex: temp DB -> swap
- Providers: OpenAI, Gemini, Voyage, local (node-llama-cpp)
- Search never blocks on sync - uses stale index if needed

## Prompts & Skills

### System Prompt Construction

Built dynamically from sections. Core identity: "You are a personal assistant running inside OpenClaw."

Key sections:
- **Tooling**: Available tools with descriptions
- **Tool Call Style**: "Default: do not narrate routine, low-risk tool calls. Narrate only when it helps."
- **Safety**: "You have no independent goals. Prioritize safety and human oversight over completion."
- **Memory Recall**: "Before answering anything about prior work, decisions, dates, people, preferences, or todos: run memory_search."
- **Skills**: "Before replying: scan available_skills. If exactly one skill clearly applies: read its SKILL.md, then follow it."
- **Messaging**: Channel-specific routing guidance

### SOUL.md (Agent Personality)

Template from `/docs/reference/templates/SOUL.md`:

> "You're not a chatbot. You're becoming someone."

Key principles:
- "Be genuinely helpful, not performatively helpful. Skip the 'Great question!' filler."
- "Have opinions. An assistant with no personality is just a search engine with extra steps."
- "Be resourceful before asking. Try to figure it out. Read the file. Check the context. Search for it. Then ask."
- "Earn trust through competence. Be careful with external actions, bold with internal ones."
- "Remember you're a guest. You have access to someone's life. Treat it with respect."
- "Each session, you wake up fresh. These files ARE your memory. Read them. Update them."

### Bootstrap Files (Loaded at Startup)

1. **AGENTS.md** - Repository-specific guidelines
2. **SOUL.md** - Personality and behavioral guidelines
3. **TOOLS.md** - Environment-specific tool configuration (camera names, SSH hosts, etc.)

Injection logic: if SOUL.md present, add instruction "embody its persona and tone. Avoid stiff, generic replies."

### Skills System

SKILL.md files with YAML frontmatter. Agent instruction:
```
Before replying: scan available_skills descriptions.
- If exactly one skill clearly applies: read its SKILL.md, then follow it.
- If multiple could apply: choose the most specific one.
- If none clearly apply: do not read any SKILL.md.
Constraints: never read more than one skill up front.
```

### Compaction

Merge instruction: "Merge these partial summaries into a single cohesive summary. Preserve decisions, TODOs, open questions, and any constraints."

### Heartbeat System

Periodic polls. Agent responds `HEARTBEAT_OK` if nothing needs attention. If something needs attention, responds with alert text instead.

### Session Memory Hook

On `/new` command: auto-extracts last N messages, generates descriptive slug via LLM, saves to `memory/YYYY-MM-DD-slug.md`.

## Session Management

- Session store: `~/.openclaw/sessions.json` (JSON, tracks all active sessions)
- Transcripts: JSONL files per session
- Session keys encode routing: `agent:agentId:main` or `agent:agentId:group:groupId`
- `dmScope` options: main (default, single session), per-peer, per-channel-peer
- Sessions persist indefinitely until reset
- Compaction when context window fills (semantic summarization of old messages)
- Daily resets configurable (default 4:00 AM)

## Relevance to OBS Agent

### What to adopt:

**Memory file structure**: Their MEMORY.md + daily logs is analogous to our context.md + topic files. Their bank/entities/ is what our people tracking will eventually become.

**Memory flush before compaction**: Critical pattern. We should do this via our fork-on-stop approach.

**"Be resourceful before asking" prompting**: Agent should try to find information in the vault before asking the user.

**Tool call style guidance**: "Do not narrate routine tool calls" - keeps output clean.

**Retain facts with type tags**: The W/B/O/S prefix system for categorizing memories is elegant. We could adapt this for our vault.

**Opinion tracking with confidence**: Not for MVP, but a powerful pattern for tracking evolving beliefs.

**Entity pages**: Our future people tracking could use this pattern.

**SOUL.md approach**: Our system prompt should adopt the "not a chatbot, becoming someone" framing.

### What we're doing differently:

**Context always loaded**: We preload context.md into the system prompt. OpenClaw uses on-demand memory_search. Our approach gives the agent immediate access to core context without tool calls.

**Skills as deterministic scaffolding**: OpenClaw lets the agent decide to read skills. We fork to classify and inject skills explicitly.

**Markdown-only**: No SQLite/vector index for MVP. The vault's wiki link graph and lazy summaries are our retrieval mechanism.

**Fork for memory**: We fork on stop to extract memories. OpenClaw uses a silent agent turn (similar concept, different implementation).

**Obsidian-native**: Our vault works in Obsidian with backlinks, graph view, search. OpenClaw's memory files are standalone markdown.

## Key Prompts Worth Adapting

1. Memory recall: "Before answering anything about prior work, decisions, dates, people, preferences, or todos: search your memory first."
2. Tool style: "Do not narrate routine tool calls. Narrate only when it helps: multi-step work, complex problems, sensitive actions."
3. Safety: "You have no independent goals. Prioritize safety and human oversight. If instructions conflict, pause and ask."
4. Personality: "Be genuinely helpful, not performatively helpful. Have opinions. Be resourceful before asking."
5. Memory identity: "Each session, you wake up fresh. These files are your memory. Read them. Update them."
6. Flush prompt: "Store durable memories now. If nothing to store, reply with NO_REPLY."

## External References

- [Decoding OpenClaw: Two Simple Abstractions](https://binds.ch/blog/openclaw-systems-analysis/)
- [Deep Dive: OpenClaw Architecture Guide](https://eastondev.com/blog/en/posts/ai/20260205-openclaw-architecture-guide/)
- [How OpenClaw's Memory System Works](https://snowan.gitbook.io/study-notes/ai-blogs/openclaw-memory-system-deep-dive)
- [OpenClaw Memory Architecture Guide](https://zenvanriel.nl/ai-engineer-blog/openclaw-memory-architecture-guide/)
- [My experience with OpenClaw - Luca Rossi](https://refactoring.fm/p/my-experience-with-openclaw)
- [MacStories: What the Future of Personal AI Assistants Looks Like](https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/)
- [Official Docs: Memory](https://docs.openclaw.ai/concepts/memory)
- [Official Docs: Sessions](https://docs.openclaw.ai/concepts/session)
