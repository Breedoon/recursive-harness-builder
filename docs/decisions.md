# Design Decisions

Decisions made during the design of the OBS Agent system. Each entry records what was decided, what alternatives were considered, and why.

D001-D017 were made during the [[Agent/system/sessions/2026-02-11-initial-design|initial design session on 2026-02-11]] (architecture & conventions phase). D018-D025 were made in the same session during the implementation planning phase, informed by research into [[Agent/system/research/openclaw|OpenClaw]], [[Agent/system/research/claude-mem|claude-mem]], and the [[Agent/system/research/claude-sdk|Claude Agent SDK]]. D026-D031 were made on 2026-02-12 during the message queuing, interrupt, hook pipeline, and observability implementation. D032-D038 were made on 2026-02-19 during Telegram flow simplification, eval hardening, and production startup reliability fixes.

---

## D001: Agent SDK Choice
**Decision**: Use Claude Agent SDK for Python (`claude-agent-sdk` pip package)
**Alternatives considered**:
- Anthropic Python SDK (raw API calls, build agent loop ourselves)
- Claude Code as subprocess (shell out to `claude` CLI)
**Rationale**: Agent SDK provides the full agent loop, tool use, and prompt caching out of the box. Bundles the Claude Code CLI. Gives us custom tools via in-process MCP servers without reimplementing infrastructure.

## D002: File Split - Knowledge vs Runtime
**Decision**: Agent knowledge (memory, skills, context) lives inside the Obsidian vault; Python runtime code lives in a separate project directory
**Alternatives considered**:
- Everything in vault
- Everything outside vault
- Hybrid with code in vault
**Rationale**: Knowledge files benefit from Obsidian indexing (backlinks, search, graph). Python code does not. Skills are knowledge documents (markdown with instructions), not code - they belong in the vault. Small utility scripts already live in the vault (`Assets/TemplateScripts/`). The principle: put things in the vault if and only if they benefit from the Obsidian knowledge graph.

## D003: Agent Folder Location
**Decision**: Top-level `Agent/` folder in the vault, same level as `Vault/`, `Misc/`, `Ж/`
**Alternatives considered**:
- `_agent/` (underscore prefix)
- Inside `Assets/`
**Rationale**: It's heavily used and referenced - deserves first-class status, not a hidden prefix.

## D004: File Access Method
**Decision**: Direct filesystem for most operations; Obsidian CLI/URI only for templated file creation
**Alternatives considered**:
- Always through Obsidian
- Always direct filesystem
**Rationale**: Direct access is simpler and faster. Obsidian's file watcher picks up changes. But Templater templates (daily/weekly/monthly entries with `tp.*` syntax) must be instantiated through Obsidian to process the template logic.

## D005: Memory Model
**Decision**: Single `context.md` root that grows organically; topics split off on demand
**Alternatives considered**:
- Pre-defined topic files from the start
- Wiki-style with no single entry point
- Layered: core + pre-defined topic categories
**Rationale**: Avoids empty placeholder files. Every topic file earns its place by growing from context.md. The root provides a guaranteed entry point every session. Wiki-style linking happens naturally within and between all files.

## D006: History Tracking
**Decision**: Append-only History sections within topic files; Git as catastrophic safety net only
**Alternatives considered**:
- Rely on Git history for past states
- Separate history log files
**Rationale**: The agent can't easily search Git history. Explicit history in files means the agent always knows what was important before. Git catches catastrophic mistakes but isn't the working memory mechanism.

## D007: Indexing Strategy
**Decision**: Lazy indexing via parent notes. Curated summaries appended on first touch. Filesystem search for exhaustive queries.
**Alternatives considered**:
- Comprehensive upfront indexing
- Separate `Agent/indexes/` folder
- Pure organic emergence (no indexes)
**Rationale**: Upfront indexing is wasteful (220+ unprocessed meeting notes). Separate index folder violates locality. Pure emergence misses files never linked. Lazy-append means zero upfront cost, grows for free (summary generated while file is already in context), and parent notes stay co-located with what they describe.

## D008: Parent Note vs _index.md
**Decision**: Sibling parent notes (e.g., `Meeting Notes.md` next to `Meeting Notes/`). No `_index.md` inside directories.
**Alternatives considered**:
- `_index.md` inside each directory
- Both patterns for different cases
**Rationale**: One convention instead of two. `[[Meeting Notes]]` resolves naturally to the parent note. Standard Obsidian MOC (Map of Content) pattern. Parent notes follow the same universal lifecycle as everything else.

## D009: Archive Naming
**Decision**: `_archive-001.md`, `_archive-002.md` etc. (sequential numbering, cutoff date noted inside)
**Alternatives considered**:
- `_archive-2025.md` (by calendar year)
- Date-based cutoff in filename
**Rationale**: Calendar year boundaries are arbitrary (not everything fits neatly into years). Sequential numbering is simpler. The actual cutoff date is documented inside the file.

## D010: External Data Handling
**Decision**: Reference cards (markdown) in the vault pointing to external data. The vault stores pointers with context, not raw data. Three tiers of representation: (1) native vault files self-represent, (2) external sources get reference cards, (3) high-volume streams get aggregated.
**Alternatives considered**:
- Copy all external data into vault
- YAML frontmatter on existing files
- No reference system
**Rationale**: Keeps the vault lean (iCloud storage constraint). Reference cards are the universal adapter - always markdown, always linkable. High-volume streams (text messages, emails) can still get reference cards at conversation/batch granularity, but not per individual message.

## D011: Summary Tiers
**Decision**: Three tiers (one-line in parent note / detailed in `_summaries.md` / full original). MVP implements Tier 0 only.
**Alternatives considered**:
- One-tier (summaries only in parent notes)
- Inline detailed summaries in parent notes
**Rationale**: The marginal cost of generating a summary when a file is already loaded is near-zero. Tier 0 (one-liners) is sufficient for MVP. Tier 1 (detailed summaries) is defined as a convention now but activated later. Three tiers prevent the agent from having to re-read full files to understand content.

## D012: Vault Cleanup
**Decision**: Minimal - just add `Agent/` folder. No restructuring of existing content.
**Alternatives considered**:
- Light cleanup (archive dead content)
- Full vault restructure
**Rationale**: Don't let cleanup block the system. Existing structure works. Clean up organically over time as the agent starts managing things.

## D013: MVP Scope
**Decision**: Agent skeleton (CLI) + Agent folder + core skills + Git setup. No Telegram, people tracking, decision records, embeddings, or external pipelines.
**Rationale**: Infrastructure first, features on top. The skeleton provides the foundation everything else builds on.

## D014: Session Continuity
**Decision**: Leverage SDK prompt cache for seamless continuation. Auto-offboard before cache expires. Fresh session loads context.md + skills.
**Alternatives considered**:
- Always fresh sessions
- Persistent resumable sessions with stored state
**Rationale**: SDK caching provides ~1hr window for free. Auto-offboarding ensures nothing is lost. Fresh sessions with context.md loading provide cold-start capability. Should feel like one long-running session.

## D015: Skill Routing
**Decision**: Core skills always referenced in system prompt. Deeper skills loaded on demand. Agent uses judgment. Future: dedicated skill router.
**Alternatives considered**:
- Skill router / dispatcher as first-pass classifier
- All skills always loaded
**Rationale**: Loading all skills wastes context. A router adds complexity. Core skills referencing deeper skills is simple and works with the SDK's built-in skill system. The user's separate skill router project will plug in later.

## D016: Unified Document Lifecycle
**Decision**: All documents (indexes, reference cards, topic files, etc.) follow the same lifecycle: start small, grow, split. No fundamentally different file categories.
**Alternatives considered**:
- Separate index files vs reference cards vs topic files with different structures
- Fixed categories with different behaviors
**Rationale**: A reference card that grows becomes an index naturally. A topic file that grows splits the same way. One lifecycle means one set of skills, one set of conventions. The `_` prefix distinguishes system-managed files without requiring different behavior.

## D017: Git Commit Strategy
**Decision**: Two-layer approach. Obsidian Git plugin auto-commits every 15 minutes (background safety net for manual edits). Agent makes explicit commits with descriptive messages after meaningful operations.
**Alternatives considered**:
- Commit on every file change (too noisy, no storage benefit)
- Daily auto-commits only (less granular undo history)
- Agent commits only, no auto-backup (misses manual edits when Obsidian is open)
**Rationale**: Git delta-compresses text diffs so efficiently that commit frequency has near-zero impact on storage. A 20MB text vault generates ~5-7MB of `.git` growth per year regardless of whether commits happen every 15 minutes or every day. The 15-minute interval provides fine-grained undo at no meaningful cost. Agent commits provide meaningful history for navigating changes (`git log`).

---

## D018: Forking as Core Primitive
**Decision**: Use SDK `fork_session=True` for skill classification, vault search, and memory extraction. A generic ForkRunner manages all fork types.
**Alternatives considered**:
- Native SDK subagents (AgentDefinition)
- Separate daemon processes
**Rationale**: Forks reuse the KV cache (near-zero marginal cost). More flexible than native subagents (which can't spawn sub-subagents). Managed from Python, giving full control over lifecycle. The system prompt must match exactly for cache reuse — forks inherit it automatically.

## D019: Skill Injection via Fork Classification
**Decision**: Before processing each user message, fork the session to classify what skills are needed. Fork returns skill names; Python reads SKILL.md files and injects content as system message.
**Alternatives considered**:
- Let the agent decide to read skills on its own (OpenClaw approach)
- Always load all skills
- SDK-native skill system (`.claude/skills/`)
**Rationale**: More deterministic than hoping the agent notices it should load a skill. Skills are the backbone of vault consistency — they must run when needed. SDK-native skills lack programmatic control. Always-loading wastes context.

## D020: Vault Search as Fork
**Decision**: The vault search "tool" is actually a fork that searches the vault and returns a structured summary with excerpts, file links, relevance explanations, and temporal context.
**Alternatives considered**:
- Vector/embedding search (Chroma, Milvus)
- Direct tool calls in main session
- Separate search daemon
**Rationale**: Preserves main session context (fork does the heavy reading). Enables future parallelization (multiple search forks with different strategies). No infrastructure overhead for MVP. Temporal weighting rules live in a tunable skill.

## D021: Memory Extraction via Fork + Distribution
**Decision**: On stop/pre-compaction, fork to extract memories. Distribute to context.md, topic files, journal. Also write a daily memory log at `Agent/memory/YYYY-MM-DD.md`.
**Alternatives considered**:
- Centralized memory store (OpenClaw's `bank/`)
- Separate observer agent (claude-mem pattern)
- Agent self-offboards without fork
**Rationale**: Vault IS the memory — information lives where it belongs. Daily log provides a dated "what I learned" ledger with links. Distribution prevents context.md from bloating. Inspired by OpenClaw's Retain/Recall/Reflect loop.

## D022: No Compaction - Flush and Restart
**Decision**: When approaching context limits, intercept via PreCompact hook, fork to persist memories, then restart with a fresh session that loads updated context.md.
**Alternatives considered**:
- SDK's built-in compaction (semantic summarization)
- Manual session management with token counting
**Rationale**: Avoids lossy compaction. Fresh session has full clean context. The fork captures everything important before the restart. Inspired by OpenClaw's pre-compaction memory flush, but uses fork instead of silent agent turn.

## D023: Token Metrics in Python Logs
**Decision**: Track token usage, cache hits/misses, and fork stats in Python log files, not in the vault.
**Alternatives considered**:
- Metrics in vault files
- External monitoring service
**Rationale**: Operational metrics don't belong in the knowledge graph. Vault stores knowledge, Python stores telemetry. Cache hit/miss monitoring via JSONL transcript analysis enables alerting on unexpected misses.

## D024: Proactive Behavior via System Prompt + Skill
**Decision**: Core proactive instructions in system prompt ("be resourceful, connect dots"). Detailed patterns in a dedicated skill loaded for complex interactions.
**Alternatives considered**:
- All proactive behavior in system prompt
- All in a skill (loaded every time)
**Rationale**: Always-on behavior belongs in the prompt. Tunable, detailed patterns belong in a skill. Inspired by OpenClaw's SOUL.md approach — core personality always present, specialized behavior loaded on demand.

## D025: Context.md as Orientation Document
**Decision**: context.md contains a vault map, current focus, active threads, recent decisions, people references, topic file links, and skill references. It's the agent's "brain state" on every fresh start.
**Alternatives considered**:
- On-demand memory search only (OpenClaw's approach)
- Separate orientation file + separate memory
**Rationale**: The agent wakes up fresh each session. context.md must orient it completely without requiring additional lookups for basic operation. Unlike OpenClaw (which uses memory_search tools), we preload context to avoid the cold-start latency of searching before every interaction.

---

## D026: Hook Pipeline as Extensible Middleware
**Decision**: SDK hooks (PreToolUse, PostToolUse) are implemented as a `HookPipeline` — a chain of check functions that run sequentially, short-circuit on interrupt/deny, and accumulate `additionalContext`. Each check is a factory closure capturing shared state.
**Alternatives considered**:
- Single monolithic hook function per event
- Decorator-based registration
- Direct SDK hook callbacks without pipeline
**Rationale**: The pipeline pattern makes hooks composable — interrupt check, immutable guard, and queue check are independent functions that can be reordered, added, or removed. Short-circuit semantics (interrupt stops before queue drain) are the correct priority. Factory closures capture `HookState` and `OBSConfig` cleanly without globals.

## D027: Message Queuing via HookState + additionalContext
**Decision**: Queued messages are stored in `HookState.message_queue` (asyncio.Queue) and injected into the agent via `additionalContext` at hook boundaries. Messages remaining after `query()` completes are drained and prepended to the next turn's prompt.
**Alternatives considered**:
- Inject as a new user message (requires SDK support for mid-turn injection)
- Store in a file the agent reads (too slow, requires tool use)
- Only drain at hook boundaries (messages lost if no tools used)
**Rationale**: `additionalContext` is the SDK's built-in mechanism for injecting context at hook boundaries — it's the right primitive. The two-phase drain (hook boundaries + post-query) ensures no messages are ever lost regardless of whether the agent uses tools. Prepending to the next prompt is simple and the agent naturally sees it as context.

## D028: Command Registry for Recursive Self-Access
**Decision**: Commands (stop, quit, enqueue) are implemented as a `CommandRegistry` with named handlers. Daemon endpoints are thin wrappers that call `registry.execute(name)`. The agent could also call commands via the same registry.
**Alternatives considered**:
- Inline command logic in each endpoint
- CLI-only commands with no daemon awareness
**Rationale**: Every capability exposed to the user must also be accessible to the agent itself (recursive self-access principle). The registry maps names to handlers callable from both CLI (via HTTP) and agent (via hooks or future MCP tools). Adding future commands (`/fork`, `/session-info`, `/flush-memory`) is trivial.

## D029: Non-blocking stdin via select() for Concurrent CLI Input
**Decision**: During streaming, CLI reads stdin using `select.select()` with 0.5s timeout + `asyncio.Event` stop signal, instead of blocking `sys.stdin.readline()` in a thread.
**Alternatives considered**:
- `sys.stdin.readline()` in ThreadPoolExecutor (original implementation — orphaned threads ate input)
- `prompt_toolkit` or `curses` for async input
- Accept the limitation and require Enter after response
**Rationale**: Blocking readline in a thread cannot be cancelled — the thread survives task cancellation and consumes the user's next line of input. `select.select()` with a short timeout allows clean exit when the SSE stream finishes. No external dependency needed (select is stdlib). The 0.5s poll interval is imperceptible to users.

## D030: SSE Status Events for Client-Agnostic Observability
**Decision**: The daemon emits structured status events in the SSE stream using the standard `event: status` field with JSON payloads (`type`, `summary`, optional `count`). Event types: `tool_use`, `queue_delivered`, `skill_classify`, `thinking`. CLI renders them as dimmed parenthetical text.
**Alternatives considered**:
- CLI-only status display (print directly, not in SSE)
- WebSocket with separate status channel
- Embed status in text stream with special markers (`[STATUS: ...]`)
**Rationale**: Status events are part of the message infrastructure, not client-specific. Any client (CLI, Telegram, web) consumes the same SSE stream. The SSE `event:` field is standard — clients that don't understand `event: status` silently ignore them (backward-compatible). JSON payloads allow progressive enhancement (Telegram can show tool icons, CLI just shows `summary`). `summary` is always human-readable — even a dumb client can display it.

## D031: LLM-as-Judge + pexpect for Real E2E Testing
**Decision**: The highest-confidence test layer uses `pexpect` to drive the real CLI process and Haiku as an LLM judge to evaluate response quality. All other test layers (unit, TestClient, live HTTP, real SDK) are necessary but insufficient.
**Alternatives considered**:
- Mocked tests only (fast but miss real bugs)
- Manual testing only (catches bugs but not automated)
- Selenium/Playwright-style browser automation (wrong tool for CLI)
**Rationale**: 263 passing mocked tests shipped two critical bugs (queue messages never reaching the agent, stdin consumed by orphaned threads). Both were immediately obvious in manual testing and caught instantly by pexpect tests. LLM-as-judge avoids brittle string matching — criteria are human-readable and robust to model output variations. The combination of pexpect (tests the plumbing) + LLM judge (tests the intelligence) is the gold standard for agent E2E. See `docs/testing-philosophy.md` for the full testing layer reference.

---

## D032: Telegram Per-Turn Chronological Messaging (No Editable Status Message)
**Decision**: Telegram output is sent as per-turn messages with inline status/tool visibility, flushed on runner `TurnEndEvent`, instead of using a separate editable status message plus end-of-turn text dump.
**Alternatives considered**:
- Editable status message (`StatusMessageManager`) + final response message
- One Telegram message per low-level block/event
**Rationale**: The editable status model was unreliable under concurrency and harder to reason about. Per-turn flushes preserve chronology while reducing rate-limit/edit conflicts and keeping output easy to audit in human order.

## D033: Per-Chat Lock + Simple Polling for Background Queue Auto-Delivery
**Decision**: Telegram processing is serialized per chat with `asyncio.Lock`, and background queue delivery uses a simple 3-second poller when the chat is idle.
**Alternatives considered**:
- No lock (allow races with `concurrent_updates=True`)
- Event-driven wake/coalescing infrastructure (OpenClaw-like heartbeat system)
**Rationale**: The lock removes common ordering races with minimal complexity. Polling is operationally simple and robust for the current single-user production shape; it provides background auto-delivery without introducing a larger scheduling subsystem.

## D034: `(done)` Sentinel as Completion Contract for Telegram
**Decision**: Every Telegram run sends content silently (`disable_notification=True`) and emits a final `(done)` message with notification enabled.
**Alternatives considered**:
- Track and notify only the exact final content chunk
- No explicit completion marker
**Rationale**: `(done)` is a low-complexity, explicit completion signal that is easy for humans and eval harnesses to detect. It avoids fragile “true last chunk” logic while preserving clear operator awareness.

## D035: Observability-First Tool Summaries
**Decision**: Tool summaries are standardized to 200-character truncation and unknown tools fall back to structured payload visibility instead of opaque labels.
**Alternatives considered**:
- Shorter, prettified summaries with limited detail
- Unknown-tool summaries with minimal metadata
**Rationale**: For this system, lack of visibility is a higher risk than verbose logs. Standardized 200-char summaries preserve consistency while exposing enough context to understand what the agent actually did.

## D036: Telegram Evals as Single Sequential Aggregate Test
**Decision**: Telegram scenarios (`tg_*`) run sequentially within one aggregate test (`test_eval_telegram_all`) using a real bot process + Telethon client, with `/new` reset between scenarios.
**Alternatives considered**:
- Parametrized per-scenario async Telegram tests
- Mocked Telegram adapter tests as primary validation
**Rationale**: Shared external state (chat history, bot process) makes parallel parametrized execution flaky and cross-contaminating. A single sequential aggregate run preserves causality and supports reliable real-message verification.

## D037: Intent-Aware Judge Output with Mandatory `NOTES`
**Decision**: Eval scenarios include an `Intent` section and judge output is required to include `CRITERIA CHECK`, `INTENT CHECK`, and `NOTES` even when verdict is PASS.
**Alternatives considered**:
- Criteria-only pass/fail reporting
- Human post-hoc review without structured judge notes
**Rationale**: Criteria-only verdicts can miss “technically passing but clearly off” behavior. Intent + notes create a first-class channel for suspicious observations and improve decision quality without forcing failures on every soft anomaly.

## D038: Production Telegram Entrypoint Loads `.env` Directly
**Decision**: `telegram_main.py` loads `.env` into `os.environ` (if keys are unset) before `OBSConfig.from_env()`.
**Alternatives considered**:
- Require shell-exported env vars for production startup
- Add a new dependency solely for dotenv loading
**Rationale**: Manual shell export semantics are error-prone and differed from test/eval startup behavior. A lightweight built-in loader aligns production with harness behavior and removes startup friction without adding a dependency.
