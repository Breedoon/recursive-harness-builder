# OpenClaw Telegram Integration: Deep Dive Research

## 1. Overall Architecture

OpenClaw is a Node.js/TypeScript monorepo that connects a single LLM-powered agent to multiple messaging channels (Telegram, WhatsApp, Discord, Slack, Signal, iMessage, Google Chat). The architecture follows a **plugin-per-channel** pattern with a shared "auto-reply" pipeline that routes inbound messages to an LLM agent and delivers responses back through the originating channel.

### Key Layers

```
[Channel Modules]  →  [Auto-Reply Pipeline]  →  [Agent/LLM]
   telegram/            auto-reply/              agents/
   discord/             routing/                 providers/
   whatsapp/            channels/
   slack/               sessions/
```

### Core Source Layout (relevant to us)

| Directory | Purpose |
|-----------|---------|
| `src/telegram/` | 87 files — Telegram-specific bot, send, receive, media, formatting |
| `src/channels/` | Channel abstraction: `dock.ts` (metadata), `registry.ts` (channel list), `plugins/` (type contracts) |
| `src/auto-reply/` | Shared dispatch pipeline: inbound processing → agent → reply delivery |
| `src/routing/` | Session key generation, agent binding resolution |
| `src/agents/` | Agent configuration, model selection, identity |
| `src/config/` | Configuration loading, session store |

### Architectural Patterns

1. **Channel as Plugin**: Each channel implements `ChannelPlugin` interface (`src/channels/plugins/types.plugin.ts`). The plugin defines capabilities, config resolvers, security, groups, threading, and outbound adapters.

2. **Channel Dock**: Lightweight metadata registry (`src/channels/dock.ts`) — a `Record<ChatChannelId, ChannelDock>` that holds per-channel capabilities, text chunk limits, streaming config, group/mention/threading adapters. This is the "light" abstraction shared code imports.

3. **Auto-Reply Pipeline**: Messages from any channel get normalized into a `MsgContext` object, dispatched through `dispatchReplyFromConfig()`, which calls the agent, then delivers replies back via channel-specific delivery functions.

4. **No Abstract Base Class**: OpenClaw does NOT use class inheritance for channel abstraction. Instead, it uses TypeScript interfaces/types (`ChannelPlugin`, `ChannelDock`, `ChannelCapabilities`) with function-based adapters. Each channel module is a standalone implementation that conforms to the type contracts.

---

## 2. Telegram Specifics: Polling vs Webhooks

### Library: grammY

OpenClaw uses **grammY** (`grammy` npm package) — a modern Telegram Bot API framework for TypeScript/Node.js.

- **File**: `src/telegram/bot.ts:146` — `const bot = new Bot(opts.token, client ? { client } : undefined);`
- grammY handles the low-level Bot API, provides context objects, middleware chains, and update processing

### Polling (Default)

**File**: `src/telegram/monitor.ts:90-215`

The default mode uses `@grammyjs/runner` for concurrent long-polling:

```typescript
const runner = run(bot, createTelegramRunnerOptions(cfg));
await runner.task(); // blocks until runner stops
```

Key details:
- **`@grammyjs/runner`** provides concurrent update processing (configurable concurrency via `resolveAgentMaxConcurrent`)
- **Long-polling timeout**: 30 seconds (grammY default)
- **Retry policy**: exponential backoff, max retry time 5 minutes
- **Update offset persistence**: `src/telegram/update-offset-store.ts` — persists `lastUpdateId` to a JSON file so restarts don't replay old updates
- **Conflict handling**: 409 errors (another bot instance polling) trigger a restart with backoff (initial 2s, max 30s, factor 1.8)
- **AbortSignal support**: graceful shutdown via signal propagation

### Webhook (Optional)

**File**: `src/telegram/webhook.ts:19-127`

When `useWebhook: true`, it starts an HTTP server:
- Creates a `node:http` server on configurable port (default 8787)
- Uses grammY's `webhookCallback(bot, "http")` to handle POST requests
- Health endpoint at `/healthz`
- Registers webhook URL with Telegram via `bot.api.setWebhook()`
- Supports webhook secrets for validation

**Takeaway for OBS**: Polling is the right default for us (no exposed ports). The `@grammyjs/runner` approach with concurrent update processing is mature and battle-tested.

---

## 3. Message Queuing and Background Tasks

### Sequential Processing via grammY Middleware

**File**: `src/telegram/bot.ts:148` — `bot.use(sequentialize(getTelegramSequentialKey));`

Messages are sequentialized per-conversation (chat ID + optional thread ID). This prevents race conditions where two messages from the same chat are processed concurrently and produce interleaved agent responses.

The sequential key function (`getTelegramSequentialKey`, `bot.ts:67-110`) routes:
- Control commands to a separate queue (`telegram:{chatId}:control`)
- Forum topics to per-topic queues (`telegram:{chatId}:topic:{threadId}`)
- Regular messages to per-chat queues (`telegram:{chatId}`)

### Inbound Debouncing

**File**: `src/telegram/bot-handlers.ts:77-138`

A configurable debouncer batches rapid-fire messages from the same sender in the same conversation:
- Combines text from consecutive messages into a single agent request
- Skips debouncing for media or control commands
- Configurable delay (`resolveInboundDebounceMs`)

### Media Group Buffering

**File**: `src/telegram/bot-handlers.ts:200-224`

Telegram sends multi-photo/video messages as separate updates with a shared `media_group_id`. OpenClaw buffers these with a timeout (`MEDIA_GROUP_TIMEOUT_MS`), then processes the group as a single message with all media attached.

### Text Fragment Reassembly

**File**: `src/telegram/bot-handlers.ts:69-75, 776-836`

Telegram splits long pasted text into multiple messages (~4096 chars each). OpenClaw detects these fragments by:
- Checking if text length >= 4000 chars (threshold)
- Consecutive message IDs within gap of 1
- Time gap <= 1500ms
- Max 12 parts, max 50K total chars

Buffered fragments are reassembled into a single message before agent dispatch.

### No Explicit Queue System

Unlike OBS Agent's message queue + hook pipeline, OpenClaw does NOT have a centralized message queue. Instead:
- Each channel handles its own message buffering/debouncing
- The auto-reply pipeline is synchronous per-message (await agent response, then deliver)
- Background tasks are not a concept — each message is processed to completion

---

## 4. Interface Abstraction

### Three-Layer Abstraction

**Layer 1: `ChannelPlugin` (Heavy)**
- Full channel implementation contract: `src/channels/plugins/types.plugin.ts`
- ~20 adapter interfaces: config, setup, pairing, security, groups, mentions, outbound, status, gateway, auth, elevated, commands, streaming, threading, messaging, agent prompt, directory, resolver, actions, heartbeat
- Each channel registers its plugin with capabilities

**Layer 2: `ChannelDock` (Light)**
- Lightweight metadata: `src/channels/dock.ts`
- Holds: id, capabilities, text chunk limit, streaming defaults, config resolvers, groups, mentions, threading
- Shared code imports from here (NOT from heavy plugins)
- Statically defined in a `DOCKS` record for core channels

**Layer 3: `MsgContext` (Normalized Message)**
- Channel-agnostic message representation: `src/auto-reply/templating.ts`
- Fields: Body, From, To, SessionKey, ChatType, SenderId, SenderName, Provider, Surface, MediaPath, MessageThreadId, etc.
- Every channel builds a `MsgContext` from its native message format
- The auto-reply pipeline only works with `MsgContext`

### The Abstraction is NOT Symmetrical

Critical observation: **inbound and outbound abstractions are completely different**.

**Inbound**: Each channel has its own message handler that builds a `MsgContext` and calls the shared auto-reply pipeline. The `MsgContext` is the abstraction point.

**Outbound**: Each channel has its own `sendMessage*` function. The auto-reply pipeline calls channel-specific delivery functions. There is no unified "send" interface — each channel's delivery is bespoke.

### What We Should Learn

For OBS Agent, the key insight is: **don't force CLI and Telegram into the same class hierarchy**. Instead:
1. Define a normalized message format (our equivalent of `MsgContext`)
2. Each interface (CLI, Telegram) handles its own inbound → normalized and normalized → outbound
3. The daemon/session layer only works with the normalized format

---

## 5. Message Splitting and Media Handling

### Text Chunk Limit

**File**: `src/channels/dock.ts:97` — `outbound: { textChunkLimit: 4000 }`

Telegram's actual limit is 4096 chars, but OpenClaw uses 4000 for safety margin. This is configurable per-channel in the dock.

### Markdown → HTML Rendering

**File**: `src/telegram/format.ts`

Telegram uses its own HTML subset for formatting. OpenClaw has a full markdown-to-Telegram-HTML pipeline:
1. Parse markdown to intermediate representation (IR): `markdownToIR()`
2. Render IR with Telegram-specific markers: `<b>`, `<i>`, `<s>`, `<code>`, `<pre><code>`, `<tg-spoiler>`, `<a href="...">`
3. Escape HTML entities properly
4. Handle markdown tables (configurable mode)

For chunked responses, `markdownToTelegramChunks()` splits at the IR level (preserving formatting across chunks).

### HTML Parse Error Fallback

**File**: `src/telegram/send.ts:357-380`

If Telegram rejects HTML-formatted text (`can't parse entities`), the system retries as plain text. This is a graceful fallback pattern.

### Caption Splitting

**File**: `src/telegram/caption.ts`

Telegram captions are limited to 1024 chars. If text exceeds this:
- Send media without caption
- Send text as a separate follow-up message

### Media Handling

**File**: `src/telegram/send.ts:385-568`

Full media type routing:
- **Images**: `sendPhoto`
- **Videos**: `sendVideo` (or `sendVideoNote` for round video notes)
- **Audio**: `sendAudio` (or `sendVoice` for voice bubbles)
- **GIFs**: `sendAnimation`
- **Documents**: `sendDocument` (fallback for unknown types)
- **Stickers**: `sendSticker` (separate function, `sendStickerTelegram`)

Each media type gets proper thread params, reply markup, and caption handling.

### Sticker Vision Cache

**Files**: `src/telegram/sticker-cache.ts`, `src/telegram/bot-message-dispatch.ts:194-243`

Innovative pattern: when a sticker is received and the model doesn't support vision, OpenClaw uses a separate vision API call to describe the sticker, caches the description by `fileUniqueId`, and injects it as text. This avoids redundant vision calls for commonly-used stickers.

### Draft Streaming

**Files**: `src/telegram/draft-stream.ts`, `src/telegram/draft-chunking.ts`

Telegram supports a proprietary draft streaming feature (`sendMessageDraft`). OpenClaw implements a throttled draft updater that streams LLM output to a draft message in private chats, providing typing-indicator-like live preview. Two modes:
- `partial`: direct partial text updates
- `block`: chunked updates with configurable min/max chars and break preferences

---

## 6. Error Handling and Retry Logic

### Network Error Classification

**File**: `src/telegram/network-errors.ts`

Comprehensive error classification system:
- **Recoverable error codes**: ECONNRESET, ETIMEDOUT, ENOTFOUND, etc. (20+ codes)
- **Recoverable error names**: AbortError, TimeoutError, ConnectTimeoutError
- **Message snippets**: "fetch failed", "socket hang up", "timeout"
- **Deep error traversal**: walks `cause`, `reason`, `errors[]`, and grammY's `HttpError.error`

### Retry Policy

**File**: `src/infra/retry-policy.js` (referenced by send.ts)

Configurable retry with exponential backoff for all API calls. Each send operation wraps calls in `createTelegramRetryRunner()`.

### Thread-Not-Found Fallback

**File**: `src/telegram/send.ts:300-322`

If a send fails with "message thread not found" (e.g., forum topic deleted), automatically retry without `message_thread_id`. This prevents messages from being lost due to stale thread references.

### Chat-Not-Found Wrapping

Error messages for "chat not found" are enriched with diagnostic context: possible causes (bot not started, removed from group, group migrated, wrong token).

### Unhandled Rejection Handler

**File**: `src/telegram/monitor.ts:97-103`

Registers a global unhandled rejection handler specifically for grammY `HttpError` + recoverable network errors. This catches network errors that escape the polling loop's try-catch (e.g., from background `setMyCommands`).

---

## 7. Testing Infrastructure

### Unit Tests (Extensive)

The Telegram module has ~30 test files directly in `src/telegram/`:
- `bot.test.ts` (96KB — massive, tests bot creation, message handling, access control)
- `send.returns-undefined-empty-input.test.ts` (24KB)
- `send.caption-split.test.ts` (11KB)
- `bot-message-context.*.test.ts` (multiple focused test files)
- `format.test.ts`, `network-errors.test.ts`, `token.test.ts`, etc.

### E2E Tests

**File**: `vitest.e2e.config.ts`

E2E tests use real API calls (not mocks). File naming convention: `*.e2e.test.ts`. Examples:
- `reply.directive.directive-behavior.*.e2e.test.ts` (many focused scenarios)
- `reply.triggers.trigger-handling.*.e2e.test.ts`

### Testing Approach for Telegram

OpenClaw does NOT test Telegram by sending messages to a real bot and verifying responses (what we plan to do). Instead:
- **Bot creation tests**: verify grammY bot is configured correctly
- **Handler tests**: simulate Telegram updates, verify message processing
- **Send tests**: mock the Bot API, verify correct API calls
- **Access control tests**: verify allowFrom/groupPolicy logic

### Key Test Patterns

1. **Grammy context mocking**: Tests create synthetic `TelegramContext` objects with just `message`, `me`, `getFile` fields
2. **Inline test file names**: Long descriptive names (`bot.create-telegram-bot.accepts-group-messages-mentionpatterns-match-without-botusername.test.ts`)
3. **Focused test files**: Each test file covers one specific behavior/scenario
4. **No real bot testing**: All tests mock the Telegram Bot API

**Takeaway**: For OBS, we'll go further than OpenClaw — our eval judge will actually interact with a real Telegram bot. OpenClaw's approach is thorough for unit testing but doesn't verify the real Telegram round-trip.

---

## 8. Session/Conversation Management

### Session Keys

**File**: `src/routing/resolve-route.ts`

Session keys are hierarchical, encoding the full routing context:
```
agent:{agentId}:telegram:{chatType}:{peerId}
```

For groups with forum topics:
```
agent:{agentId}:telegram:group:{chatId}:{topicId}
```

### Routing Bindings

Agents are routed via configurable bindings:
1. **Peer binding**: specific chat/group → specific agent
2. **Parent peer**: thread inherits parent's binding
3. **Guild/Team**: Discord/Slack workspace → agent
4. **Account**: Telegram account → agent
5. **Channel**: all Telegram → agent
6. **Default**: fallback agent

### Session State

**File**: `src/config/sessions.ts`

Session state is persisted in a JSON store file:
- `lastChannel`, `lastTo`, `lastAccountId` — for reply routing
- `model`, `modelProvider` — per-session model overrides
- `groupActivation` — "always" or "mention" for groups
- Timestamp tracking for staleness detection

### DM Thread Support

Telegram DM "topics" (message threads in private chats) get their own session keys via `resolveThreadSessionKeys()`. This allows per-thread context isolation within a single DM conversation.

---

## 9. Gotchas, Lessons Learned, and Patterns to Borrow

### Borrow: Sequential Processing Per Chat

The `sequentialize(getTelegramSequentialKey)` pattern is essential. Without it, concurrent messages in the same chat produce interleaved agent responses. We should implement the same for OBS's Telegram interface — ensure only one agent response is in-flight per conversation.

### Borrow: Text Fragment Reassembly

Telegram silently splits long pasted text into multiple messages. We MUST handle this or the agent will get incomplete inputs. OpenClaw's 4000-char threshold + 1500ms gap + message ID adjacency check is a good heuristic.

### Borrow: Media Group Buffering

Multi-image/video sends arrive as separate updates. Buffer by `media_group_id` with a short timeout before processing.

### Borrow: HTML Parse Error Fallback

Always try HTML formatting first, fall back to plain text if Telegram rejects it. This prevents message delivery failures.

### Borrow: Thread-Not-Found Fallback

Retry sends without `message_thread_id` if the thread doesn't exist. Topics can be deleted, groups can migrate.

### Borrow: Update Offset Persistence

Persist the last processed update ID to disk. On restart, start from where you left off. OpenClaw uses atomic writes (write to temp, then rename).

### Avoid: No Abstract Base Class

OpenClaw's approach of NOT having an abstract base class for channels is intentional. Each channel is so different that forcing inheritance would create a leaky abstraction. Use **type contracts** (interfaces) instead.

### Avoid: Complex Plugin System

OpenClaw's plugin system has ~20 adapter interfaces. We don't need this complexity for two channels (CLI + Telegram). A simpler normalized message type with per-channel adapters is sufficient.

### Gotcha: Group Chat Complexity

OpenClaw's group chat handling is enormous (~800 lines in `bot-handlers.ts`). Groups require: mention detection, allowlist enforcement, group policy evaluation, sender prefix formatting, history buffering for context, topic/thread routing. We should start with DM-only for Telegram and add group support later.

### Gotcha: Telegram API Rate Limits

OpenClaw uses `@grammyjs/transformer-throttler` (middleware) to rate-limit outgoing API calls. Telegram has strict rate limits (~30 msgs/sec for bots). Without throttling, the bot gets 429 errors.

### Gotcha: Markdown ↔ HTML Conversion

Telegram doesn't support standard Markdown. OpenClaw has an entire IR-based markdown-to-HTML pipeline (`src/markdown/` + `src/telegram/format.ts`). We'll need similar conversion for our agent's markdown output.

### Pattern: Capabilities-Based Feature Gating

The `ChannelCapabilities` type declares what each channel supports (polls, reactions, media, threads, block streaming). Shared code checks capabilities before using features. This is cleaner than channel-specific if/else branches.

---

## 10. Specific Code Patterns and File Paths Most Relevant to Us

### Message Flow (Inbound)

1. **Polling**: `src/telegram/monitor.ts` → `@grammyjs/runner` → `bot.on("message", ...)`
2. **Bot setup**: `src/telegram/bot.ts` → `createTelegramBot()` creates grammY Bot, adds middleware
3. **Sequentialization**: `bot.use(sequentialize(getTelegramSequentialKey))`
4. **Message handler**: `src/telegram/bot-handlers.ts:668-927` → access control, fragment/media buffering, debouncing
5. **Message processing**: `src/telegram/bot-message.ts` → `createTelegramMessageProcessor()`
6. **Context building**: `src/telegram/bot-message-context.ts:128-700` → builds `MsgContext` from Telegram update
7. **Dispatch**: `src/telegram/bot-message-dispatch.ts:60-357` → calls auto-reply pipeline
8. **Agent call**: `src/auto-reply/dispatch.ts` → `dispatchReplyFromConfig()` → calls LLM agent
9. **Reply delivery**: `src/telegram/bot/delivery.ts` → channel-specific send logic

### Message Flow (Outbound)

1. **Send function**: `src/telegram/send.ts:232-592` → `sendMessageTelegram()`
2. **HTML rendering**: `src/telegram/format.ts:69-78` → `renderTelegramHtmlText()`
3. **Caption splitting**: `src/telegram/caption.ts` → handles 1024 char limit
4. **Media routing**: `src/telegram/send.ts:385-568` → type-based (photo/video/audio/document)
5. **Retry with fallback**: HTML → plain text, with-thread → without-thread

### Configuration

1. **Telegram account resolution**: `src/telegram/accounts.ts` → resolves bot token, config from YAML
2. **Bot token**: env var `TELEGRAM_BOT_TOKEN` or config `channels.telegram.accounts.{id}.botToken`

### Key Types

| Type | File | Purpose |
|------|------|---------|
| `ChannelPlugin` | `src/channels/plugins/types.plugin.ts` | Full channel contract |
| `ChannelDock` | `src/channels/dock.ts:40-64` | Lightweight metadata |
| `ChannelCapabilities` | `src/channels/plugins/types.core.ts:169-182` | Feature flags |
| `MsgContext` | `src/auto-reply/templating.ts` | Normalized inbound message |
| `TelegramContext` | `src/telegram/bot/types.ts:11-15` | Minimal Grammy projection |
| `TelegramStreamMode` | `src/telegram/bot/types.ts:4` | Draft streaming mode |

### Dependencies

| Package | Purpose | Version Notes |
|---------|---------|---------------|
| `grammy` | Telegram Bot API framework | Core dependency |
| `@grammyjs/runner` | Concurrent long-polling | For production polling |
| `@grammyjs/transformer-throttler` | API rate limiting | Prevents 429 errors |
| `@grammyjs/types` | Telegram type definitions | Type-only |

---

## Summary: What We Should Take From OpenClaw

### Must-Have Patterns
1. **grammY as the Telegram library** — mature, TypeScript-native, good middleware support
2. **Long-polling with `@grammyjs/runner`** for concurrent updates
3. **Sequential processing per chat** to prevent interleaved responses
4. **Text fragment reassembly** (4000+ char messages get split)
5. **Media group buffering** (multi-image messages)
6. **HTML formatting with plain-text fallback**
7. **Update offset persistence** for restart resilience
8. **API rate limiting** with throttler middleware

### Architecture Decisions
1. **No class inheritance** — use type contracts and per-channel adapters
2. **Normalized message format** as the abstraction point between channel and agent
3. **Start with DMs only** — group chat support is massive (do it later)
4. **Separate inbound/outbound abstractions** — they're fundamentally different

### Testing Strategy
1. OpenClaw does thorough unit testing with mocked Bot API
2. For OBS, we'll go further: real bot + eval judge that sends actual Telegram messages
3. Need dev bot + test bot, main account + secondary account for eval judge

---

## 11. Cross-Team Synthesis (Incorporating Teammate Findings)

### Architecture Mapping: OpenClaw → OBS Agent

Based on cross-pollination with the architecture-auditor's audit of our codebase:

| OpenClaw Component | OBS Agent Equivalent | Status |
|-------------------|---------------------|--------|
| `dispatchReplyFromConfig()` | **ConversationRunner** (proposed) | Needs extraction from daemon.py |
| `MsgContext` (normalized message) | No equivalent yet | Needs creation |
| `ReplyDispatcher` (callbacks) | `on_text`/`on_status`/`on_turn_complete` callbacks | Proposed in ConversationRunner |
| `ChannelDock` (channel metadata) | No equivalent yet | Lightweight, add when needed |
| `resolveAgentRoute()` → `sessionKey` | **SessionRegistry** (proposed) | Needs creation for multi-chat |
| grammY `sequentialize()` | `asyncio.Lock` per chat_id | Needs implementation |
| `HookState` (per-session mutable state) | `HookState` (currently singleton) | Needs per-session instances |

### Key Design Decision: Direct Integration, Not HTTP Proxy

OpenClaw's Telegram module calls `dispatchReplyFromConfig()` directly — there is no HTTP intermediary between the Telegram handler and the agent pipeline. This validates architecture-auditor's Approach B: extract a ConversationRunner that both the HTTP/SSE daemon and the Telegram handler call directly.

The alternative (Telegram bot → HTTP POST to daemon → daemon → SDK) adds unnecessary latency and couples Telegram to the CLI's transport assumptions (SSE streaming).

### Library Choices (from telegram-api-researcher)

| Component | Library | Rationale |
|-----------|---------|-----------|
| **Bot (OBS Agent)** | `python-telegram-bot` v22+ | Maps to grammY: polling, handlers, filters, async |
| **Eval judge client** | `Telethon` | MTProto user client, `Conversation` helper for send/receive |
| **Markdown → HTML** | Custom lightweight converter | Maps to OpenClaw's `src/telegram/format.ts` IR pipeline |

### Text Fragment Reassembly Correction

OpenClaw joins text fragments with empty string (`"".join()`), NOT newlines — because Telegram splits at exact character boundaries mid-text. The `FragmentBuffer` in our implementation must concatenate directly:

```python
combined = "".join(fragment.text for fragment in self._buffer)  # NOT "\n".join()
```

Reference: `src/telegram/bot-handlers.ts:236`

### Recommended Implementation Order

Based on all three research tracks:

1. **Extract ConversationRunner** from daemon.py (pure refactor, existing evals verify)
2. **Add SessionRegistry** (Dict[str, (SessionManager, HookState)] with LRU eviction)
3. **Implement Telegram bot** using PTB, calling ConversationRunner directly
4. **Implement TelegramPlatform** for evals using Telethon
5. **Write Telegram-specific eval scenarios** (message splitting, fragment reassembly, /stop handling)
6. **Add media support** (voice, images, documents) — later phase

### Open Questions (Team Consensus Needed)

1. **`/reset` for eval isolation**: Send between tests to clear session state? (Recommended: yes)
2. **CLI + Telegram evals**: Run sequentially? (Recommended: yes, simpler timing)
3. **Debounce duration**: 1-2s acceptable latency for message batching? (OpenClaw uses configurable per-channel)
4. **Background fork delivery**: Send fork results as new Telegram messages? (Natural mapping, but needs persistent per-chat watcher loop)
