# Telegram as Agent Orchestration & Observability Platform

**Status**: Spec (scoping)
**Date**: 2026-03-01 (updated from initial 2026-02-27 draft)

---

## 1. Vision

Transform the Telegram adapter from a single-chat, single-session text interface into
a full orchestration and observability platform for coding agents. The platform uses
Telegram's native features (forum topics, message replies, deep links, forwarding,
inline keyboards) as the primary UI, avoiding custom frontend work while gaining remote
access, concurrency visualization, and multi-agent coordination for free.

### What the platform should feel like

- **Orchestration engine**: Spin up agents, fork conversations, delegate sub-tasks,
  manage permissions — all from Telegram.
- **Observability surface**: Every running agent, fork, and background command is
  visible in its own topic. You can see what's happening, where, and navigate between
  contexts via hyperlinks.
- **Remote-first**: Works from phone, desktop, or anywhere Telegram runs. No SSH
  tunnels, no port forwarding, no local terminal needed.
- **Recursively composable**: Agents can fork, sub-agents can fork, and the UI
  represents the tree — even though Telegram topics are flat.
- **Telegram is a shell, not a storage layer**: The canonical state lives in JSONL
  session files, vault files, and (eventually) SQLite. Telegram visualizes and
  interacts. Old Telegram chats can be cleaned up after sessions expire without
  data loss.

### Why Telegram

1. Topics in group chats = free multi-pane agent view with lifecycle management
2. Deep links to every message = free traceability
3. Message forwarding between topics = free context portability
4. Inline keyboards = free interactive permission prompts
5. Pinned messages = persistent navigation anchors and context status
6. Multi-user group membership = future collaborative use without extra auth
7. Battle-tested mobile + desktop clients = no UI maintenance
8. Rich API for bots (create/close/rename/delete topics programmatically)

### Where Telegram falls short

1. **No nested topics** — topics are flat, but agent trees are recursive. This is the
   single biggest UX gap. Mitigation: deep-linked navigation commands, tree-view
   summaries, forwarded-message breadcrumbs, and overflowing into new group chats
   for deeper recursion.
2. **No real-time token streaming** — can only send/edit complete messages. Current
   per-turn flush model is fine, but no character-by-character streaming.
3. **4096 char message limit** — already handled by code-block-aware splitting.
4. **Rate limits** (~30 msg/min per bot in groups) — becomes real with many concurrent
   agents. Mitigation: message batching, multiple bot tokens for load distribution.
5. **No native tree/graph visualization** — the "where am I in the agent tree" question
   requires a synthesized text/hyperlink answer, not a visual graph.
6. **No collapsible sections** — long `/tree` output wraps on phone screens. No way
   to expand/collapse subtrees.
7. **No reply-thread isolation** — replying to a message doesn't create an isolated
   thread view (unlike Slack). Multiple concurrent forks in the same topic are visually
   interleaved.

---

## 2. Current Architecture (What Exists)

### Assumptions baked into the current code

| Assumption | Where | Impact of changing |
|------------|-------|--------------------|
| **One session per daemon** | `SessionManager` holds a single `_client`, `_session_id` | Core refactor. Every component that touches session state needs a session identifier. |
| **One user** | `_last_chat_id`, `_last_bot`, `telegram_allowed_user_ids` as flat list | Medium. Auth model needs per-session or per-chat scoping. |
| **One chat** | `TelegramBot` sends all output to `_last_chat_id`, background poller has single target | Medium-high. Topic routing needs `(chat_id, thread_id)` tuples everywhere. |
| **No topic awareness** | `send_message` never passes `message_thread_id` | Additive. Every send call needs optional topic routing. |
| **Single HookState** | Shared `HookState` across all activity (queues, interrupt, background tasks) | High. Per-session or per-agent HookState isolation needed. |
| **Single ConversationRunner** | One runner active at a time, continuation loop is sequential | High. Concurrent runners for concurrent agents in different topics. |
| **Forks are invisible** | `self_fork` results enqueue to `message_queue` as text; no routing metadata | Medium. Fork results need to know which topic to deliver to. |
| **No message-to-JSONL mapping** | Telegram messages are fire-and-forget; no stored correlation | New capability. |

### Component coupling map

```
config.py ──> session.py ──> runner.py ──> telegram.py
                  │               │              │
                  │               │              ├── FragmentBuffer (per-chat-user)
                  │               │              ├── _chat_locks (per-chat)
                  │               │              ├── _busy_chats (global set)
                  │               │              └── _last_chat_id (single target)
                  │               │
                  │               └── hooks.py (single HookState)
                  │                       │
                  │                       └── tools.py (self_fork, background_tasks)
                  │
                  └── ClaudeSDKClient (one instance)
```

The key observation: `SessionManager`, `HookState`, and `ConversationRunner` are all
singletons in the current design. The Telegram adapter assumes a single active
conversation at any time. Breaking that singleton assumption is the core architectural
change — but it happens incrementally, not all at once (see Phase 0 below).

---

## 3. Concept Model

### Resolved design decisions

These were open questions in the initial draft, now resolved:

- **General topic = the trunk agent.** The main conversation lives in General. All
  other topics in the group chat are its direct children (forks, sub-agents, background
  tasks). One level of depth per group chat.

- **One group chat = one agent tree**, not one project. A project can have multiple
  trees (multiple group chats). Topics are always agents, not projects. A topic that
  needs its own sub-agents overflows into a new group chat where it becomes the General
  (trunk) of that sub-tree. The parent group chat's topic and the child group chat's
  General are linked bidirectionally.

- **DM with the bot = control plane.** Create workspaces, list active trees, configure
  projects. All agent work happens in group chats.

- **Telegram is visualization, not storage.** Session summaries, memories, and
  compaction outputs are stored in the vault or OBS agent code, not as Telegram messages.
  Telegram chats can be cleaned up (even deleted) after all sessions in a tree have
  expired and been summarized, without losing data.

- **Session auto-summarization at idle timeout.** When a session has been inactive for
  ~58 minutes (cache window), it auto-compacts: saves session summary, extracts
  memories, offboards the session. This is being built now (out of scope for this spec;
  assume it exists for later phases).

- **Fork = JSONL copy, not SDK `fork_session`.** Forking from a specific message works
  by copying the JSONL parent chain up to that entry and resuming from the copied file.
  This is necessary because `fork_session=True` forks from the HEAD of the session, not
  from an arbitrary point. No depth limit on forks.

- **Codex integration comes after first version of topics.** After we have Claude agents
  in topics with basic fork/sub-agent support, then we experiment with Codex as a
  sub-agent backend. Different SDKs can't fork from each other — they're only usable as
  sub-agents with independent sessions.

- **Permissions tabled until after multi-SDK.** The permission model (policy-based
  auto-approve, ephemeral evaluator agents, user escalation via inline keyboard,
  sandboxing, worktrees) is a large design space. Noted but not addressed in Phase 0-B.

### Entities and relationships

```
Control Plane (DM with bot)
  └── manages Workspaces

Workspace (= Telegram group chat with forum topics enabled)
  ├── config: project path, vault path, SDK backend
  ├── General topic: trunk agent session (depth 0)
  ├── Topic A: fork/sub-agent session (depth 1)
  ├── Topic B: fork/sub-agent session (depth 1)
  │
  ├── If a depth-1 agent needs sub-agents:
  │     Topic B becomes General of a NEW group chat
  │     (linked bidirectionally via deep links)
  │
  └── MessageMap (in-memory, later SQLite)
        └── telegram_message_id → jsonl_entry_uuid

Session (per-topic)
  ├── SessionManager (owns ClaudeSDKClient)
  ├── HookState (own queues, interrupt, background tasks)
  ├── ConversationRunner (instantiated per-turn)
  ├── thread_id: Telegram message_thread_id
  ├── parent: optional (fork source session + message uuid)
  └── children: list of forked session IDs
```

### Telegram feature utilization plan

| Telegram feature | Used for |
|-----------------|----------|
| **Forum topics** | One topic per agent/fork/background task |
| **Topic names** | Prefix + descriptive task summary (128 char limit) |
| **Topic icons/colors** | Indicate agent type (fork, sub-agent, Codex) or status (active, idle) |
| **Topic close/reopen** | Close when no live agents remain in topic |
| **Pinned messages** | Service message at topic creation: fork source link, context usage, links to active fork heads within this topic. Updated as context changes. |
| **Reply-to** | System messages reply to the user message they reference. Fork-point annotations. Selective, not every message. |
| **Deep links** | Navigate between topics, between group chats (for deeper recursion), to specific messages in `/tree` output |
| **Message forwarding** | Move fork history from one topic to a new topic (forwarded messages are clickable to original) |
| **Inline keyboards** | Permission prompts (later phase) |

---

## 4. Feature Inventory

### Phase 0: Immediate (pre-topic, single-chat improvements)

| Feature | Description | Depends on |
|---------|-------------|------------|
| **F-0A: Reply-to for system messages** | System messages ("received", "working", "queued", completion) use `reply_to_message_id` pointing to the user message they're about. For queued messages, "working" replies to the original user message — this IS the delivery notification, replacing the separate "queued message delivered" status. | Nothing |
| **F-0B: In-memory message mapping** | Process-wide dict mapping `telegram_msg_id → jsonl_entry_uuid`. Populated from SDK stream as messages flow through the runner. Enables fork-via-reply and future traceability. Not persisted (restart loses it; persistence comes with SQLite later). | SDK UUID patch (see §12) |
| **F-0C: Fork via reply** | User replies to any previous bot message with text. If the replied-to message is the latest assistant message → normal continuation. If it's an earlier message → fork: copy JSONL parent chain to that point, create new session, process user's message there. Fork stays in the same chat (no topics yet). Introduces `self._sessions` dict (singleton → dict). | F-0B (mapping) |

### Tier 0: Foundation (topics)

| Feature | Description | Breaking change? |
|---------|-------------|-----------------|
| **F01: Topic-routed messaging** | Every `send_message` call includes `message_thread_id`. The adapter routes incoming messages by their topic. | Yes — every send path changes. |
| **F02: Per-topic session isolation** | Each topic gets its own `SessionManager`, `HookState`, `ConversationRunner`. A `SessionRegistry` manages the collection. | Yes — core architectural change. |
| **F03: Forum group setup** | Bot operates in a supergroup with forum topics enabled (manually created). General topic = trunk agent. | Additive (migration from DM). |
| **F04: Topic lifecycle for agents** | Fork/sub-agent starts → `createForumTopic`. Finishes → `closeForumTopic`. Topic name reflects the task. | New capability. |

### Tier 1: Core user workflows

| Feature | Description |
|---------|-------------|
| **F05: User-initiated fork to topic** | `/fork` replying to a message → creates topic, copies JSONL chain, starts session, links parent. |
| **F06: Agent-initiated fork to topic** | `self_fork` → creates topic, streams output there, parent gets status link. |
| **F07: Navigation commands** | `/tree` — hyperlinked tree of active sessions with depth, status, context %, and deep links. `/sessions` — flat list with status. |
| **F08: Session management from DM** | DM = control plane. `/workspace create <name>`, `/workspace list`. |
| **F09: Pinned service message** | At topic creation: pinned message with fork source link, context usage, heads of all forks within this topic. Updated on context changes. |

### Tier 2: Observability & quality of life

| Feature | Description |
|---------|-------------|
| **F10: Persistent message mapping (SQLite)** | Persist `telegram_msg_id → jsonl_uuid` + `session_id → topic mapping` + `session tree structure`. Enables crash recovery, cross-restart navigation. |
| **F11: Background command topics** | Verbose background output streams into dedicated topics. |
| **F12: Topic metadata & icons** | Descriptive names with prefix convention, color-coded by type/status. |
| **F13: "Move to topic" command** | `/totopic` replying to first message of an inline fork → forwards messages to new topic, breadcrumb link in original. |
| **F14: Compaction summary storage** | Summaries stored in vault (not Telegram). Linked from topic's pinned message. |

### Tier 3: Multi-SDK & advanced orchestration

| Feature | Description |
|---------|-------------|
| **F15: SDK backend abstraction** | `AgentBackend` protocol abstracting over Claude SDK and Codex CLI. |
| **F16: Codex CLI backend** | Drives Codex as sub-agent. No hooks — observe-only. |
| **F17: Multi-SDK workspace** | Topics running different backends in the same group chat. |
| **F18: Delegated permissions** | Multi-layer: policy auto-approve → evaluator agent → user escalation via inline keyboard. Sandboxing and worktrees. |
| **F19: Slash command parity** | `/compact`, `/clear`, `/model`, `/cost`, `/permissions` — scoped per-topic. |
| **F20: Multi-user support** | Multiple users in group chat interact with agents. Identity tracked per-message. |

### Tier 4: Polish & scale

| Feature | Description |
|---------|-------------|
| **F21: Multi-bot load distribution** | Distribute output across bot tokens when rate-limited. |
| **F22: Userbot automation** | Telethon handles operations Bot API can't: list topics, pin, toggle forum mode, create groups. |
| **F23: Chat lifecycle cleanup** | Auto-clean Telegram chats after tree fully expires and summarizes. |

---

## 5. Architectural Impact Assessment

### What breaks

**High-impact changes (refactoring needed):**

1. **SessionManager → SessionRegistry** (Tier 0): The single `SessionManager` becomes
   a registry keyed by `(chat_id, thread_id)`. Every component that accesses
   `self._session_manager` goes through a lookup.

2. **HookState isolation** (Tier 0): Each session needs its own `message_queue`,
   `interrupt_flag`, and `background_tasks`. Otherwise `/stop` in one topic interrupts
   another, and fork results leak across sessions.

3. **ConversationRunner concurrency** (Tier 0): Multiple runners run concurrently in
   different topics. The runner itself is already stateless per-invocation — the change
   is in the surrounding lifecycle.

4. **Background poller rethink** (Tier 0): Currently assumes single `_last_chat_id`.
   Needs per-session wake-up or multi-session polling.

**Medium-impact changes (for Phase 0, before topics):**

5. **Singleton → dict for sessions** (Phase 0, F-0C): When forking is introduced,
   `self._session_manager` becomes `self._sessions: dict[str, SessionManager]` with
   `self._active_session_id`. Minimal change, but touches routing logic.

6. **Queue item type** (Phase 0, F-0A): `message_queue` items change from `str` to a
   struct carrying `(text, telegram_msg_id)` so "working" can reply to the original
   user message.

**Low-impact changes (mostly additive):**

7. **`send_message` with `message_thread_id`**: Every send call gets optional topic
   parameter. Mechanically simple, many call sites.

8. **SDK UUID patch** (Phase 0): Monkey-patch `parse_message` to preserve UUIDs on
   all message types (see §12 for details).

### What doesn't break

- **ConversationRunner internals**: The run loop is per-invocation and doesn't assume
  singleton-ness.
- **Hook pipeline logic**: `HookPipeline`, check functions, immutable guard are
  stateless relative to the session.
- **Telegram formatting**: `md_to_telegram_html`, `split_message`, HTML fallback.
- **FragmentBuffer core**: Already keyed by `(chat_id, user_id)`. Extension to include
  `thread_id` is straightforward.

### Risk assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Concurrent session resource consumption** | High | Each session = a subprocess. Auto-summarize at 58min idle timeout. Cap concurrent sessions. |
| **Telegram rate limits with many topics** | Medium | Batch messages, `disable_notification=True`, multi-bot pool for heavy workloads. |
| **State management complexity** | High | SQLite for crash recovery (Tier 2). Before that, accept that restart loses in-memory mapping. |
| **Flat topic list for deep trees** | Medium | Overflow to new group chat at depth > 1. `/tree` command with deep links. Close/hide inactive topics. |
| **HookState cross-contamination** | High | Per-session HookState. Tests that verify isolation. |
| **Group chat setup friction** | Medium | Manual setup initially. Userbot automation at Tier 4. Validation command (`/setup`) checks permissions. |

---

## 6. Telegram API Feasibility Notes

### What the Bot API can do (confirmed)

- Create topics: `createForumTopic(chat_id, name, icon_color, icon_custom_emoji_id)`
- Send to topics: `send_message(chat_id, text, message_thread_id=topic_id)`
- Reply within topics: `send_message(..., reply_to_message_id=msg_id, message_thread_id=topic_id)`
- Close/reopen topics: `closeForumTopic`, `reopenForumTopic`
- Delete topics: `deleteForumTopic` (deletes all messages too)
- Rename topics: `editForumTopic(chat_id, message_thread_id, name)`
- Forward between topics: `forwardMessage(chat_id, from_chat_id, message_id, message_thread_id=dest_topic_id)`
- Pin messages: `pinChatMessage(chat_id, message_id)` — works within topics
- Hide/unhide General: `hideGeneralForumTopic`, `unhideGeneralForumTopic`
- Deep links: `https://t.me/c/<channel_id>/<topic_id>/<message_id>`
- Topic icon colors: 6 predefined (blue, yellow, purple, green, pink, red)
- Limits: up to 1,000,000 topics per group; topic names up to 128 chars

### What requires a userbot (MTProto / Telethon)

- **List existing topics**: Bot API has no `getForumTopics`.
- **Pin/reorder topics**: Bot API can't pin topics to the top of the forum list.
- **Toggle forum mode**: Bot API can't enable/disable forum mode.
- **Create supergroups**: Bots can't create groups.

### Practical implication

For Phase 0 through Tier 1, the Bot API is sufficient. Userbot is only needed for
Tier 2+ (listing topics, pinning, auto-creating groups).

All forum topic methods are available in `python-telegram-bot` v20+ on the `Bot` object.
All send methods accept `message_thread_id` as an optional parameter.

---

## 7. Landscape: What Exists

### Closest existing projects

| Project | Approach | Relevant to us? |
|---------|----------|-----------------|
| **[ccbot](https://github.com/six-ddc/ccbot)** (121 stars) | 1:1 topic ↔ tmux window ↔ Claude session. Polls JSONL files every 2s. Inline keyboards for permissions. | **Most relevant.** Proves the topic-per-session model works. Key diff: ccbot is a tmux bridge, we're SDK-native. ccbot can't do agent-initiated topic creation. |
| **[claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram)** (1.8k stars) | SDK wrapper with SQLite sessions, event bus, cost tracking. Single-agent-per-chat. | Good reference for session persistence. Not multi-agent. |
| **[Praktor](https://github.com/mtzanidakis/praktor)** | Docker-isolated multi-agent with Telegram I/O + Mission Control web UI. NATS messaging. | Good reference for multi-agent orchestration. Different approach (Docker containers). |
| **[CCCC](https://github.com/ChesterRa/cccc)** (433 stars) | Append-only ledger, polyglot agent teams (Claude, Codex, Gemini), IM bridges. | Good reference for multi-SDK abstraction and ledger-based state. |
| **[OpenClaw](https://docs.openclaw.ai/channels/telegram)** | Per-topic agent routing in Telegram forums. Each agent owns a topic. | Good reference for topic routing. Pre-configured bindings only. |
| **[claudegram](https://github.com/NachoSEO/claudegram)** | Per-forum-topic independent sessions. `/resume`, `/continue`. Telegraph for long output. | Good reference for per-topic session resume. |

### The gap no one fills

**No existing project maps agent-initiated forks/sub-tasks to Telegram topics.**
All topic-based approaches are user-initiated or admin-pre-configured. An architecture
where `self_fork` automatically creates a topic, and the agent tree is navigable via
hyperlinks, would be novel.

---

## 8. Codex CLI Integration Assessment

### Current state

- **Language**: Rust binary + TypeScript SDK (no native Python SDK)
- **Hooks**: None. Only `notify` on `agent-turn-complete`.
- **Session resume/fork**: Supported via CLI. Fork backtracks to a turn, then branches.
- **Multi-agent**: Experimental. Built-in roles (worker, explorer, monitor).
- **MCP**: Supported as server (`codex mcp-server`) and client.
- **Python integration**: Only via MCP bridge. Two tools: `codex` and `codex-reply`.

### What can't be unified across SDKs

| Capability | Claude | Codex | Gap |
|-----------|--------|-------|-----|
| PreToolUse hook (guard/block) | Native | Not available | Can't guard vault writes from Codex |
| PostToolUse hook (queue inject) | Native | Not available | Can't inject queued messages mid-stream |
| PreCompact hook | Native | Not available | Can't intercept compaction |
| Interrupt at tool boundary | Via hook | `/stop` or signal | Different mechanism, same effect |
| Fork with cache | `fork_session=True` | `codex fork` | Need to verify cache behavior |

**Practical recommendation**: Codex agents are autonomous sub-agent workers that report
results. They don't participate in the hook-mediated orchestration loop. Different
SDKs can't fork from each other — only delegate tasks.

---

## 9. Open Questions

### Resolved (from scoping discussions)

| # | Question | Resolution |
|---|----------|------------|
| 1 | What is the General topic? | Trunk agent (main conversation). |
| 2 | Group chat per project or per tree? | **Per tree.** Depth > 1 overflows to new group chat. |
| 3 | DM as control plane? | **Yes.** Create/list workspaces from DM. |
| 6 | How should fork-from-reply work? | User replies to any bot message with text. No command needed. If reply target = last message → continue. If earlier → fork. |
| 7 | What happens when topic is done? | **Close** (readable, no new messages). Close when no live agents remain. |
| 11 | Fork = SDK fork or JSONL copy? | **JSONL copy.** Copy parent chain to target entry, resume from new file. |
| 12 | Continue original after fork? | **Yes.** Fork creates independent session. Original unaffected. |
| 13 | Fork from fork from fork? | **No depth limit.** `/tree` command for navigation. |
| 16 | Build Codex backend now? | **No.** After first version of topics. Medium-term. |
| 17 | Cross-SDK context sharing? | **No.** Sub-agent delegation only ("here's a task, go do it"). |
| 18 | Topic-specific compaction? | **Yes.** Each session compacts independently. |
| 19 | Compaction summaries in Telegram? | **No.** Stored in vault/OBS code. Telegram is not storage. |

### Still open

**Architecture:**

1. **Process limits**: Max concurrent sessions? Idle suspension and on-demand resume?
   Auto-summarization at 58min handles cleanup, but spikes of many concurrent agents
   could exhaust resources. Deferred until real usage data exists.

2. **Session registry persistence**: In-memory dict for Phase 0. SQLite for Tier 2
   crash recovery. What schema? What recovery procedure on restart?

3. **Group chat recycling**: When a tree fully expires (all sessions summarized), how
   long before the group chat is cleaned up? User confirmation? Auto-delete after N
   days? Keep indefinitely?

**User experience:**

4. **Reply-to for every message vs selective**: Currently decided as selective (system
   messages reply to the user message they reference). Revisit when multiple forks
   exist within the same topic — reply-to becomes useful for distinguishing which fork
   a message belongs to. But Telegram has no UI to isolate reply threads, so it helps
   only partially.

5. **Topic naming convention**: Prefix indicating type (fork, sub-agent, Codex, background)
   + descriptive summary. Exact format TBD. Emoji on topic icon for status (active,
   idle, done). Configurable vs hardcoded.

6. **Mobile UX for `/tree`**: When the tree has 20+ nodes, the text wraps and is
   hard to read on phone. Collapsible output not possible in Telegram. Maybe: show
   only active nodes by default, `/tree all` for everything?

**Technical:**

7. **Queued messages and forking**: User messages sent while the bot is busy get
   queued and injected as `additionalContext`. They have no JSONL entry. You can't fork
   from a queued message. Error message: "can't fork from this message." Is this
   acceptable? (Decision: yes, for now. Fork only from messages with JSONL entries.)

8. **Multiple forks within same topic vs separate topics**: Phase 0 (F-0C) creates
   forks within the same chat. What happens when you have 3 active forks in one chat?
   Output interleaves. Reply-to helps a little but Telegram can't isolate threads.
   `/totopic` (Tier 2) is the escape hatch. Is this acceptable for Phase 0?

9. **Background commands in topics**: Should verbose background command output go to
   its own topic? Useful for observability but creates topic proliferation. Maybe only
   for long-running commands? Deferred to Tier 2.

---

## 10. Implementation Sequencing

### Phase 0: Pre-topic single-chat improvements

Assumes: session auto-summarization at idle timeout is already built.
Assumes: the SDK UUID monkey-patch is in place (see §12).

**Step 0.1: Reply-to for system messages (F-0A)**
- Thread `reply_to_message_id` through all send paths in `telegram.py`
- "received" and "working" reply to the user's Telegram message
- Change `message_queue` item type from `str` to a struct carrying
  `(text, telegram_msg_id)` so "working" can reply to queued messages'
  original Telegram message
- Eliminates need for separate "queued message delivered" notification
- **No architectural changes. No multi-session. No mapping.**

**Step 0.2: SDK UUID patch + in-memory message mapping (F-0B)**
- Apply the `parse_message` monkey-patch at startup (see §12)
- Add process-wide `dict[int, str]` on `TelegramBot`: `telegram_msg_id → jsonl_uuid`
- Expose UUIDs from the runner to the Telegram adapter (new event type or property)
- Populate the mapping when sending Telegram messages (both user input and bot responses)
- For split messages: all chunks map to the same JSONL UUID
- System messages: not mapped (no JSONL entry)
- **No multi-session yet. Just the mapping infrastructure.**

**Step 0.3: Fork via reply (F-0C)**
- Detect `reply_to_message_id` on incoming messages
- Look up mapping → get `jsonl_uuid`
- If no mapping (system message, historic message) → error: "can't fork from this"
- If uuid = latest assistant entry → normal continuation
- If uuid ≠ latest → **fork**:
  1. Read the JSONL file (path: `~/.claude/projects/{encoded-path}/{session_id}.jsonl`)
  2. Parse entries, traverse `parentUuid` chain from target uuid to root
  3. Write those entries to a new JSONL file (new UUID filename)
  4. Create new `SessionManager` with `resume=new_session_id`
  5. Process user's message through the new session
  6. Response sent to same chat
- **Introduces multi-session**: `self._session_manager` → `self._sessions: dict` +
  `self._active_session_id`. Active session = most recently used (for non-reply messages).
  This is the minimal de-singletoning needed for forking.

### Phase A: Forum group support (Tier 0)

Prerequisite: manually create a supergroup with forum topics enabled, add bot as admin.

1. `message_thread_id` threading through all send paths
2. `SessionRegistry` managing `(chat_id, thread_id) → Session` bindings
3. Route incoming messages by `(chat_id, message_thread_id)` to the right session
4. Per-session `HookState` isolation
5. General topic = trunk agent (backward compatible with current single-session behavior)

### Phase B: Fork-to-topic (Tier 1)

1. `/fork` replying to a message → creates topic, copies JSONL chain, links parent
2. Agent-initiated `self_fork` → creates topic, streams there, links parent
3. Topic lifecycle (close on completion, rename to reflect outcome)
4. Pinned service message at topic creation
5. `/tree` navigation command

### Phase C: Observability & persistence (Tier 2)

1. SQLite for message mapping, session registry, tree structure
2. Crash recovery on daemon restart
3. Background tasks → dedicated topics
4. Topic metadata (names, colors, icons)
5. `/totopic` move-to-topic command

### Phase D: Multi-SDK & advanced (Tier 3-4)

1. `AgentBackend` protocol
2. Codex backend
3. Multi-SDK workspace
4. Delegated permissions (policy, evaluator agents, inline keyboards, sandboxing)
5. Slash command parity
6. Multi-user support
7. Multi-bot load distribution
8. Userbot automation
9. Chat lifecycle cleanup

---

## 11. Pinned Messages Architecture (Future Reference)

This is a design idea for Tier 1+ — not immediate, but worth capturing.

Each topic gets a **pinned service message** created at topic birth. This message is
the navigation anchor for that topic. It contains:

- **Fork source**: deep link to the parent message in the parent topic/chat
- **Context usage**: current token usage, estimated remaining (updated after each turn)
- **Active fork heads**: deep links to the latest message in each fork that originated
  from this topic

When there are multiple forks within the same topic (Phase 0 behavior, before topics),
each fork could have its own pinned message. Users click through pinned messages to
navigate between forks. This partially compensates for Telegram's lack of reply-thread
isolation.

The pinned message is edited (not re-sent) when context changes — keeps the pin slot
stable rather than producing new pins that push old ones down.

---

## 12. SDK UUID Mapping: Spike Results and Architecture

### The problem

To fork from a specific message, we need to map a Telegram message back to its
corresponding JSONL entry. The JSONL uses `uuid` as the primary key for each entry.
We need to capture this uuid when messages flow through the SDK so we can store the
mapping `telegram_msg_id → jsonl_uuid`.

### What the SDK gives us (spike results)

Spike script: `spikes/sdk_uuid_spike.py`, `spikes/sdk_uuid_spike2.py`

Using `ClaudeSDKClient` (the same client our runtime uses), we sent a message that
triggered tool use and logged every message from `receive_response()`:

```
Message #1: SystemMessage    — uuid on type: NO    — uuid in raw data: YES (f8de6b29...)
Message #2: AssistantMessage — uuid on type: NO    — uuid in raw data: YES (c44b077e...)
  (thinking block)
Message #3: AssistantMessage — uuid on type: NO    — uuid in raw data: YES (2eb1727a...)
  (tool_use: Bash)
Message #4: UserMessage      — uuid on type: YES   — uuid in raw data: YES (d1771017...)
  (tool_result)
Message #5: AssistantMessage — uuid on type: NO    — uuid in raw data: YES (858f2f9b...)
  (text response)
Message #6: ResultMessage    — uuid on type: NO    — uuid in raw data: YES (a93b445e...)
```

**Key finding**: Every message has a UUID in the raw JSON data that the CLI outputs.
The SDK's `parse_message()` function receives this data as a Python dict — the UUID
is right there — but only copies it to the typed object for `UserMessage`. For
`AssistantMessage`, `SystemMessage`, and `ResultMessage`, the UUID is discarded.

This is an oversight in the SDK, not a design choice.

Also notable: `parentUuid` is NOT present in the raw streamed data. It only exists in
the JSONL file on disk. This means parent-chain traversal for forking requires reading
the JSONL file — but that's only needed at fork time, not during normal message flow.

### Solution: monkey-patch `parse_message` to preserve UUIDs

A 5-line patch applied once at startup, entirely on our side (no SDK files modified):

```python
# In a module loaded at startup (e.g., _sdk_patch.py)
from claude_agent_sdk._internal import message_parser

_original_parse = message_parser.parse_message

def _parse_with_uuid(data):
    message = _original_parse(data)
    if message is not None and isinstance(data, dict):
        message._raw_uuid = data.get("uuid")
    return message

message_parser.parse_message = _parse_with_uuid
```

After this patch, every message object from `receive_response()` has `_raw_uuid`
accessible via `getattr(message, '_raw_uuid', None)`.

**Why this is safe:**
- Python dataclasses allow setting arbitrary attributes (no `__slots__`)
- If Anthropic adds `uuid` natively to all types, our patch just overwrites with
  the same value (no-op)
- If Anthropic changes the `parse_message` function signature, it fails loudly at
  import time, not silently at runtime
- The patch is a pure wrapper — it doesn't modify behavior, only preserves data
  that was already in memory

### Message flow with the patch

```
CLI subprocess writes JSON to stdout
  ↓
SDK reads it as a Python dict (has uuid, parentUuid, sessionId, etc.)
  ↓
Our patched parse_message(data) is called
  ↓
Original parser creates typed object (AssistantMessage, etc.)
  ↓
Patch attaches data["uuid"] as message._raw_uuid
  ↓
Runner receives message with _raw_uuid available
  ↓
Runner exposes uuid to Telegram adapter (via new event type or method)
  ↓
Telegram adapter sends message to chat, stores telegram_msg_id → _raw_uuid
```

### The mapping in practice

**Data structure**: Process-wide `dict[int, str]` on `TelegramBot`.
Key = Telegram message ID, Value = JSONL entry UUID.

**Population points:**

| When | What | How |
|------|------|-----|
| Bot sends a per-turn message | `telegram_msg_id → assistant_uuid` | `send_message` returns a `Message` object with `message_id`. The UUID comes from the runner. |
| Bot sends split messages | All `telegram_msg_ids → same uuid` | One JSONL entry → N Telegram messages. All map to the same UUID. |
| User sends a text message | `telegram_msg_id → user_uuid` | The user message's UUID appears as a `UserMessage` in the SDK stream. Map after we see it. |

**NOT mapped (no JSONL entry):**

| Telegram message | Why no mapping |
|-----------------|----------------|
| System messages (received, working, queued, done) | Generated by the adapter, not the SDK. No JSONL entry. |
| Queued user messages (sent while bot was busy) | Injected as `additionalContext` in hooks, absorbed into continuation prompts. No dedicated JSONL entry. |

**Fork-via-reply flow using the mapping:**

1. User replies to a bot message with text
2. Look up `reply_to_message_id` in the mapping dict
3. **Not found** → error: "Can't fork from this message (system message or expired mapping)"
4. **Found, uuid = latest assistant entry** → normal continuation, not a fork
5. **Found, uuid ≠ latest** → fork:
   a. Get session_id from SessionManager
   b. Read JSONL file at `~/.claude/projects/{encoded-path}/{session_id}.jsonl`
   c. Parse all entries, build uuid → entry index
   d. From target uuid, traverse `parentUuid` chain to root
   e. Collect all entries in the chain (preserves conversation path, skips dead-end branches)
   f. Write to new JSONL file with new session UUID
   g. Create new SessionManager with `resume=new_session_uuid`
   h. Process user's reply text through the new session
   i. Update `self._active_session_id` to the fork (user is pursuing this branch)

### Edge cases

| Scenario | Handling |
|----------|----------|
| User replies to a system message ("received", "working") | Not in mapping. Error: "Can't fork from this message." |
| User replies to a message from a dead-end fork they abandoned | In mapping (if session hasn't been garbage-collected). Fork works — creates a new branch from that point. |
| User replies to the very last bot message | uuid = latest → normal continuation, no fork. |
| Daemon restarts, mapping lost | All prior messages become un-forkable. Acceptable for Phase 0. SQLite persistence (Tier 2) fixes this. |
| User replies to a queued message that has no JSONL entry | Not in mapping. Error: "Can't fork from this message." Acceptable — user can fork from the assistant response that incorporated their queued message instead. |
| Split message — user replies to chunk 2 of 3 | All chunks map to same UUID. Fork works correctly. |
