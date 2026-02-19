# OBS Agent Architecture Audit for Telegram Integration

## 1. Current Architecture Map

### Data Flow Overview

```
User Input
    |
    v
[cli.py] -- HTTP POST --> [daemon.py (FastAPI)] -- ClaudeSDKClient --> Claude SDK
    ^                           |
    |                           | SSE stream
    +------ text/event-stream --+
```

The system follows a client-server model:
- **CLI** (cli.py) is an HTTP client that talks to the daemon via REST + SSE
- **Daemon** (daemon.py) is a FastAPI server that owns the SDK session
- **Session** (session.py) manages the `ClaudeSDKClient` lifecycle
- **Hooks** (hooks.py) are SDK-level callbacks that run inside the daemon process
- **Tools** (tools.py) are MCP tools exposed to the agent via the SDK

### File-by-File Audit

#### `config.py` — Paths, constants, validation
- **Transport-agnostic**: YES — entirely about vault paths and daemon network config
- **Key types**: `OBSConfig` dataclass
- **Dependencies**: None (leaf module)
- **Key elements**:
  - Vault path resolution, immutable pattern checking
  - Daemon host/port, cache window, background fork timeout
  - `from_env()` classmethod for env var overrides
  - `is_immutable()` for file guard checks
- **Telegram impact**: Needs Telegram-specific config (bot token, allowed chat IDs). Could add to `OBSConfig` or create a separate `TelegramConfig`.

#### `prompt.py` — System prompt loading
- **Transport-agnostic**: YES — reads `CLAUDE.md` from vault, returns string
- **Dependencies**: `config.py`
- **Key elements**: `build_system_prompt(config)` reads vault's `CLAUDE.md`
- **Telegram impact**: None. The agent's system prompt is the same regardless of interface.

#### `hooks.py` — SDK hooks (immutable guard, interrupt, queue injection, compact)
- **Transport-agnostic**: MOSTLY YES
- **CLI-specific parts**: None explicitly, but `HookState` is shared mutable state that both the daemon endpoints (HTTP handlers) and the hook callbacks read/write. The queuing model assumes a single concurrent user.
- **Key types**:
  - `HookState` — shared state: `message_queue`, `status_queue`, `interrupt_flag`, `session_id`, `background_tasks`
  - `HookPipeline` — chains check functions, runs at PreToolUse/PostToolUse boundaries
  - Check factories: `_make_interrupt_check`, `_make_immutable_check`, `_make_queue_check`
  - `create_hook_matchers(config, state)` — builds hook dict for `ClaudeAgentOptions`
- **Data flow**: Daemon endpoints write to `HookState` (enqueue, set interrupt). Hook callbacks read from `HookState` (drain queue, check interrupt). Status events go to `status_queue` for SSE delivery.
- **Telegram impact**: `HookState` is per-session. For multi-chat Telegram, each chat needs its own `HookState`. The interrupt/queue model maps cleanly to Telegram: a new Telegram message during agent processing = enqueue, a `/stop` command = interrupt.

#### `session.py` — Session lifecycle, ClaudeSDKClient management
- **Transport-agnostic**: YES — manages `ClaudeSDKClient` lifecycle, no I/O assumptions
- **Dependencies**: `hooks.py`, `prompt.py`, `tools.py`
- **Key types**: `SessionManager`
- **Key elements**:
  - Owns a single `ClaudeSDKClient`, reuses within cache window (58 min)
  - `get_client()` — creates/reconnects client, runs `connect()` in detached asyncio.Task (critical: prevents Starlette cancel scope from killing SDK reader)
  - `should_resume()` — checks cache window for session continuity
  - `_build_options()` — assembles `ClaudeAgentOptions` with prompt, hooks, MCP tools, permissions
  - `reset()` / `async_reset()` — for fresh start after memory flush
- **CRITICAL DESIGN**: One `SessionManager` = one conversation. The session manager holds a single `HookState` and a single `ClaudeSDKClient`. This is inherently single-user/single-conversation.
- **Telegram impact**: Each Telegram chat needs its own `SessionManager` instance. A `SessionRegistry` or similar map of `chat_id -> SessionManager` would be needed.

#### `tools.py` — MCP tools (self_fork)
- **Transport-agnostic**: YES — the `self_fork` tool uses `query()` for forking, results go to `hook_state.message_queue`
- **Dependencies**: `config.py`, `hooks.py` (for `HookState`)
- **Key elements**:
  - `create_obs_tools(config, get_session_id, hook_state)` — returns MCP server
  - `self_fork` tool: foreground (blocking) and background modes
  - Background forks: `asyncio.create_task()`, result enqueued to `hook_state.message_queue`
  - Background task tracking: `hook_state.background_tasks` set
- **Telegram impact**: Fork results go to `message_queue` which is per-HookState. This works naturally for Telegram if each chat has its own HookState.

#### `fork.py` — Fork runner (generic fork + extract_memory)
- **Transport-agnostic**: YES — pure SDK operations, no I/O
- **Dependencies**: `config.py`, `metrics.py`
- **Key types**: `ForkRunner`
- **Key elements**: `run()` for arbitrary fork tasks, `extract_memory()` for session offboard
- **Telegram impact**: None. Fork runner is session-level.

#### `daemon.py` — FastAPI server, SSE streaming, HTTP endpoints
- **Transport-agnostic**: NO — this is the HTTP/SSE transport layer
- **Dependencies**: `config.py`, `hooks.py`, `session.py`, `commands.py`, `events.py`, `metrics.py`
- **Key elements**:
  - `create_app(config)` factory — wires SessionManager, HookState, CommandRegistry
  - `POST /chat` — non-streaming endpoint, full request-response cycle
  - `POST /chat/stream` — SSE streaming endpoint, the primary path
  - `POST /chat/enqueue` — queue a message for injection at next hook boundary
  - `POST /chat/interrupt` — set interrupt flag + SDK-level interrupt
  - `GET /health` — health check
  - `GET /commands` — list available commands
  - **SSE event_generator()**: Streams `TextBlock` as data lines, `ToolUseBlock`/`ThinkingBlock` as status events, drains status_queue for hook-generated events
  - **Continuation loop**: After primary response, drains message_queue and sends continuation prompts (up to `max_queue_continuations`)
  - **Background fork wake-up**: After continuations, waits for `background_tasks` to complete, drains results, sends to agent
  - **Pending messages**: Un-drained queue contents saved to `app.state.pending_messages` for next turn
- **CRITICAL**: The daemon is the core orchestration layer. It handles:
  1. Message routing (user message -> SDK)
  2. Response streaming (SDK -> SSE -> client)
  3. Queue continuation (queued messages -> continuation prompts)
  4. Background fork wake-up (fork results -> continuation prompts)
  5. Session lifecycle (touch, reconnect on CLIConnectionError)
- **Telegram impact**: This is the main refactoring target. The orchestration logic (continuation loops, background fork wake-up, queue management) is reusable but currently embedded in HTTP handler closures. A Telegram interface needs the same orchestration but different I/O (send Telegram messages instead of SSE chunks).

#### `cli.py` — CLI client (pexpect, terminal I/O)
- **Transport-agnostic**: NO — terminal-specific
- **Dependencies**: `config.py`, `input.py`
- **Key elements**:
  - `async_main()` — REPL loop: read input, stream response, handle commands
  - `stream_with_input()` — concurrent SSE consumption + stdin monitoring
  - `_consume_sse()` — parses SSE stream, renders text/status through `InputChannel`
  - `_handle_input_during_stream()` — reads stdin during streaming, handles /stop, /quit, enqueue
  - `start_daemon()` — auto-starts daemon as subprocess
  - `send_message()` — non-streaming sync helper
  - `parse_slash_command()` — command parser
- **Telegram impact**: This entire file is CLI-specific. Telegram would have a parallel `telegram.py` that consumes the same daemon API but renders to Telegram messages instead of terminal.

#### `input.py` — Input channel abstraction
- **Transport-agnostic**: PARTIALLY — defines `InputChannel` Protocol, but both implementations are terminal-specific
- **Key types**:
  - `InputChannel` Protocol: `read_input()`, `print_output()`, `print_status()`, `print_queued()`, `close()`
  - `SimpleChannel` — builtin `input()` + `select()`
  - `PromptToolkitChannel` — rich terminal with async prompt
- **Telegram impact**: The `InputChannel` protocol is a precursor to an interface abstraction, but it's too narrow — it only covers terminal I/O (read_input with prompt string, print_output with raw text). A Telegram interface needs: message sending (with formatting), media handling, message editing, reply threading, etc.

#### `events.py` — SSE status events
- **Transport-agnostic**: PARTIALLY — `StatusEvent` is a generic data class, but `to_sse()` is SSE-specific
- **Key types**: `StatusEvent` (type, summary, count, messages), `summarize_tool_use()`
- **Telegram impact**: `StatusEvent` is a good intermediate representation. `to_sse()` is one serialization. A Telegram interface would need `to_telegram()` or similar. Better: separate the event data from serialization.

#### `commands.py` — Command registry
- **Transport-agnostic**: YES — pure command dispatch operating on `HookState`
- **Key types**: `CommandRegistry`, `CommandResult`
- **Commands**: `stop` (interrupt), `quit` (end session), `enqueue` (queue message)
- **Telegram impact**: Telegram can use the same command registry. `/stop` in Telegram maps to the stop command.

#### `metrics.py` — Metrics logging
- **Transport-agnostic**: YES — pure logging, no I/O assumptions
- **Telegram impact**: None.

### Module Dependency Graph

```
config.py (leaf — no deps)
    ^
    |
prompt.py (reads config.context_path)
    ^
    |
hooks.py (uses config for immutable check, defines HookState)
    ^
    |
tools.py (uses config, HookState for fork result delivery)
    ^
    |
session.py (composes: config + prompt + hooks + tools -> ClaudeAgentOptions)
    ^
    |
daemon.py (composes: session + hooks + commands + events -> FastAPI app)
    ^
    |
cli.py (HTTP client for daemon, uses input.py for terminal I/O)
```

---

## 2. Refactoring Seams

### Where does the "interface" boundary live today?

The boundary is **between daemon.py and cli.py**, connected by HTTP/SSE. This is actually a clean architectural seam:

```
[Interface Layer]          [Orchestration Layer]         [SDK Layer]
cli.py                     daemon.py                     session.py
input.py                   commands.py                   hooks.py
                           events.py                     tools.py
                                                         fork.py
                                                         prompt.py
                                                         config.py
```

The daemon IS the orchestration layer. The CLI is one interface that talks to it over HTTP.

### Two possible approaches for Telegram:

**Approach A: Telegram as another HTTP client (like CLI)**
- Telegram bot talks to the same daemon HTTP API
- Simplest approach, but breaks down for multi-chat because daemon has a single `SessionManager`
- Would need daemon changes for multi-session support anyway

**Approach B: Extract orchestration, make daemon and telegram peers**
- Extract the core loop (send message -> stream response -> handle continuations -> handle background forks) into a reusable `ConversationRunner` or similar
- Both daemon (HTTP/SSE) and telegram (Bot API) wrap this runner
- More work but cleaner for multi-chat

### What's already transport-agnostic?
- `config.py`, `prompt.py`, `fork.py`, `metrics.py` — completely agnostic
- `hooks.py` — agnostic (HookState is per-session, not per-transport)
- `tools.py` — agnostic (fork results go to HookState.message_queue)
- `session.py` — agnostic (SessionManager manages one conversation)
- `commands.py` — agnostic (operates on HookState)

### What's tightly coupled to CLI/terminal?
- `cli.py` — 100% CLI-specific (REPL, pexpect-compatible, terminal rendering)
- `input.py` — terminal I/O only (stdin/stdout)
- `daemon.py` — tightly coupled to HTTP/SSE as the transport, but the ORCHESTRATION LOGIC inside it (continuation loops, background fork wake-up, pending message management) is conceptually transport-agnostic

### What code needs to move into a base class?

The orchestration logic currently in `daemon.py`'s endpoint handlers:

1. **Turn execution**: Send user message to SDK, collect response
2. **Continuation loop**: Drain message queue, send continuation prompts (up to max_queue_continuations)
3. **Background fork wake-up**: Wait for background tasks, drain results, send to agent
4. **Pending message management**: Save un-drained queue contents for next turn
5. **Session touch**: Update activity timer after each turn
6. **Reconnect on error**: Handle CLIConnectionError

These 6 concerns are duplicated between `/chat` and `/chat/stream` (non-streaming vs streaming), and would need to be duplicated again for Telegram. They should be extracted.

---

## 3. Interface Design Proposal

### Option 1: ConversationRunner (Recommended)

Extract the core orchestration into a class that both daemon and telegram use:

```python
class ConversationRunner:
    """Runs a single conversation turn through the SDK, handling
    continuations, background forks, and queue management."""

    def __init__(self, session_manager: SessionManager, config: OBSConfig):
        self.session = session_manager
        self.config = config

    async def run_turn(
        self,
        message: str,
        on_text: Callable[[str], Awaitable[None]],      # text chunk arrived
        on_status: Callable[[StatusEvent], Awaitable[None]],  # status event
        on_turn_complete: Callable[[], Awaitable[None]],  # full turn done
    ) -> None:
        """Execute a full turn: primary response + continuations + bg forks.

        Calls on_text/on_status as chunks arrive. Transport-agnostic.
        """
        ...
```

Then:
- **daemon.py** wraps `ConversationRunner` with SSE serialization
- **telegram.py** wraps `ConversationRunner` with Telegram message sending
- Both get continuation loops, background fork wake-up, etc. for free

### Option 2: Abstract Interface Protocol

```python
class AgentInterface(Protocol):
    """Protocol that all interfaces (CLI, Telegram, Web) implement."""

    async def send_to_user(self, text: str) -> None:
        """Send a text message to the user."""
        ...

    async def send_status(self, event: StatusEvent) -> None:
        """Send a status update to the user."""
        ...

    async def receive_from_user(self) -> str | None:
        """Receive the next message from the user. Returns None on disconnect."""
        ...

    async def handle_interrupt(self) -> None:
        """Handle user interrupt request."""
        ...

    async def on_turn_complete(self) -> None:
        """Called after each turn completes."""
        ...
```

This is cleaner but requires more restructuring. The `InputChannel` protocol in `input.py` is a primitive version of this, but only covers terminal I/O.

### Recommended: Hybrid

Use `ConversationRunner` for the orchestration logic, with callback-based output delivery. Each interface provides its own callbacks:

- **CLI/daemon**: SSE yield / terminal print
- **Telegram**: `bot.send_message()` / `bot.edit_message()`

---

## 4. Edge Cases and Concerns

### Message Queuing

**Current**: User sends message during agent streaming -> CLI POSTs to `/chat/enqueue` -> daemon puts in `HookState.message_queue` -> hook callback drains at next PreToolUse/PostToolUse boundary -> injected as `additionalContext`. If queue isn't drained by hooks, continuation loop sends them as follow-up turns.

**Telegram equivalent**: User sends a Telegram message while agent is processing. The Telegram handler should enqueue it to the same `HookState.message_queue`. The same hook-based injection and continuation loop handles delivery. This maps naturally.

**Concern**: Currently `HookState` is a singleton in the daemon (one per app). For multi-chat Telegram, each chat needs its own `HookState` and `SessionManager`. The daemon would need a session registry.

### Interrupts

**Current**: CLI detects `/stop`, POSTs to `/chat/interrupt`, which: (1) sets `HookState.interrupt_flag`, (2) calls `client.interrupt()` on the SDK client. The hook pipeline checks the flag at next tool boundary and stops.

**Telegram equivalent**: User sends `/stop` in Telegram chat. The Telegram handler calls the same interrupt logic on the chat's `HookState` and `SessionManager`. Identical mechanism.

**Concern**: SDK-level `client.interrupt()` is immediate but only stops tool execution. Pure text generation without tool calls cannot be interrupted (this is a known limitation documented in MEMORY.md).

### Background Forks

**Current**: Agent calls `self_fork(background=true)` -> `asyncio.create_task()` runs the fork -> result enqueued to `HookState.message_queue` -> daemon's SSE generator waits for `background_tasks` to complete -> drains results -> sends continuation prompt -> streams response.

**Telegram equivalent**: Same mechanism. When background fork completes and result is enqueued, the Telegram interface needs to be notified to send a new message to the user. This requires the Telegram handler to be "watching" the background tasks, similar to how the daemon's SSE generator does.

**Concern**: The SSE stream naturally stays open while waiting for background forks. Telegram doesn't have a persistent connection per-turn — it's fire-and-forget. The Telegram interface would need a persistent loop per chat that watches for background fork completions and sends results proactively.

### SSE Streaming

**Current**: The `/chat/stream` endpoint yields SSE events. The CLI parses them and renders text/status.

**Telegram equivalent**: Telegram can't stream tokens — messages arrive as complete units. Two options:
1. **Batch mode**: Wait for the full response, then send as a single Telegram message. Simple but loses real-time feedback.
2. **Progressive update**: Send an initial message, then edit it periodically as more text arrives. Better UX but more complex and subject to Telegram API rate limits (editing a message is limited).
3. **Status messages**: Send a "thinking..." message, then replace with the full response when done.

**Recommendation**: Option 3 (status message) for simplicity. Send a "thinking..." or typing indicator, then send the full response. For very long responses (>4096 chars), split into multiple messages.

### Session Management

**Current**: Single user, single session. `SessionManager` manages one `ClaudeSDKClient` with cache-window-based reconnection.

**Telegram**: Multiple chats, each potentially a different conversation. Options:
1. **One session per chat**: Each Telegram chat gets its own `SessionManager`. Clean separation. Memory/cost scales linearly with active chats.
2. **Shared session**: All chats share one session. Simpler but conversations bleed into each other. Bad.
3. **Pool**: Pool of sessions with LRU eviction. Complex but efficient for many chats.

**Recommendation**: Option 1 (one session per chat). The cache window (58 min) already handles cleanup — idle chats naturally expire.

### Message Length Limits

Telegram messages are capped at 4096 characters. Agent responses can easily exceed this. Need a message splitting strategy:
- Split at paragraph boundaries
- If no paragraph boundary, split at sentence boundaries
- If still too long, hard-split at 4096 with continuation indicator
- Markdown formatting must be preserved across splits (close/reopen code blocks, etc.)

### Media Types

Telegram supports images, files, voice messages, etc. The current system is text-only. For V1, treating all Telegram input as text (extracting text from media where possible) is reasonable. Media support can be added later.

---

## 5. Testing Infrastructure Audit

### How CLI Evals Work Today

The eval system has 4 components:

1. **Scenario files** (`tests/evals/scenarios/*.md`): Markdown files defining steps and pass criteria
2. **Scenario parser** (`tests/evals/scenario.py`): Parses markdown into `EvalScenario` objects with `EvalStep` items
3. **Platform** (`tests/evals/platform.py`): `CLIPlatform` drives the real CLI via pexpect
4. **Judge** (`tests/evals/judge.py`): An SDK agent (`ClaudeSDKClient`) that either drives interaction via MCP tools (sequential) or evaluates a transcript (concurrent)

**Dual-mode architecture**:
- **Sequential scenarios** (basic_chat, vault_write, etc.): Judge agent gets MCP tools (`send_message`, `read_output`) that wrap `CLIPlatform`. Judge follows scenario steps, sends messages, reads responses, evaluates criteria.
- **Concurrent scenarios** (queue_message, interrupt): Harness drives `CLIPlatform` directly using `pexpect` (send_nowait, sleep for timing). Captures transcript. Judge evaluates transcript without MCP tools.

**Key infrastructure pieces**:
- `CLIPlatform` spawns the real CLI via `pexpect.spawn()` with env vars for vault, port, prompt
- `_ensure_fixture_vault()` clones the real vault via `scripts/clone_vault.sh`
- Eval-specific daemon port (7833) to avoid conflicts
- `OBS_EVAL_PROMPT` env var prevents prompt collision with markdown blockquotes

### What Would Need to Change for Telegram Evals

The `Platform` protocol in `platform.py` is the key abstraction:

```python
class Platform(Protocol):
    async def send(self, text: str) -> str: ...
    async def send_nowait(self, text: str) -> None: ...
    async def read(self) -> str: ...
    async def wait_for_prompt(self, timeout: int = 120) -> str: ...
    async def close(self) -> None: ...
```

A `TelegramPlatform` would implement this protocol by:
- `send()` — send a Telegram message to the bot, wait for response message(s)
- `send_nowait()` — send without waiting
- `read()` — return last received message(s)
- `wait_for_prompt()` — wait for the bot to send a response (Telegram has no "prompt" concept — need to wait for a message from the bot, possibly with a timeout)
- `close()` — stop the bot

**Reusable components**:
- Scenario parser (`scenario.py`) — fully reusable, transport-agnostic
- Judge agent (`judge.py`) — fully reusable for sequential scenarios (MCP tools wrap `Platform`, not `CLIPlatform`). Also reusable for transcript-based concurrent judging.
- Scenario files — mostly reusable. Some may need tweaks for Telegram-specific behavior (e.g., message splitting, typing indicators).

**Needs replacement**:
- `CLIPlatform` — Telegram needs a `TelegramPlatform` that talks to a test bot or mock server
- `conftest.py` fixtures — need Telegram-specific fixtures (test bot token, test chat setup)

**New Telegram-specific scenarios**:
- Long message splitting (>4096 chars)
- Media message handling
- Multi-message rapid fire
- Bot command handling (/start, /stop, /help)
- Group chat vs private chat behavior

### Evaluation Architecture Summary

The eval system is well-designed for multi-platform testing. The `Platform` protocol is the right abstraction point. Adding Telegram evals requires:
1. Implement `TelegramPlatform` (either real bot or mock server)
2. Add Telegram-specific scenario files
3. Reuse the judge and scenario parser as-is

---

## 6. Summary: Key Refactoring Recommendations

### Must Do (before Telegram integration)
1. **Extract ConversationRunner from daemon.py** — the turn execution, continuation loop, and background fork wake-up logic should be reusable
2. **Session registry for multi-chat** — map of `chat_id -> (SessionManager, HookState)` for Telegram
3. **Separate StatusEvent from SSE serialization** — `to_sse()` should be one of many serialization options

### Should Do (for clean architecture)
4. **Config extension** — add Telegram-specific config (bot token, allowed chats) without bloating `OBSConfig`
5. **Message splitting utility** — for Telegram's 4096-char limit, usable by any future interface
6. **Background fork notification** — mechanism for non-streaming interfaces to be notified when forks complete

### Nice to Have
7. **Typing indicators** — Telegram "typing..." action during agent processing
8. **Inbound debouncing** — batch rapid-fire Telegram messages into single agent request (OpenClaw pattern)

### What NOT to Refactor
- `hooks.py` — already per-session via `HookState`, no changes needed
- `tools.py` — already uses `HookState.message_queue`, works for any interface
- `prompt.py`, `fork.py`, `metrics.py` — completely agnostic, leave alone
- `cli.py` — leave as-is, it's a CLI client. Telegram is a separate client.

---

## 7. Insights from OpenClaw Research

(Integrated from openclaw-researcher's findings -- see `spikes/openclaw-telegram-research.md` for full details)

### Key Architecture Patterns from OpenClaw

1. **No abstract base class for channels**: OpenClaw deliberately avoids class inheritance. Each channel (Telegram, Discord, WhatsApp, etc.) is a standalone module that conforms to TypeScript type contracts (`ChannelPlugin`, `ChannelDock`). The abstraction point is a **normalized message format** (`MsgContext`), not a shared base class.

2. **Three-layer abstraction**:
   - **ChannelPlugin** (heavy) -- full channel implementation contract (~20 adapter interfaces)
   - **ChannelDock** (light) -- metadata registry with capabilities, text limits, streaming config
   - **MsgContext** (normalized) -- channel-agnostic message representation

3. **Inbound and outbound are NOT symmetrical**: Inbound normalizes to `MsgContext`. Outbound uses channel-specific delivery functions. No unified "send" interface.

4. **Sequential processing per chat**: grammY's `sequentialize()` middleware ensures only one message is processed at a time per conversation. Without this, concurrent messages produce interleaved agent responses.

5. **No centralized message queue**: Unlike OBS Agent's queue + hook pipeline, OpenClaw processes each message synchronously to completion. No background task concept.

### Patterns We Should Borrow

1. **Text fragment reassembly**: Telegram silently splits long pasted text (>4096 chars) into multiple messages. OpenClaw detects these via: text length >= 4000 chars, consecutive message IDs, time gap <= 1500ms. Reassembles up to 12 parts / 50K chars. **We MUST implement this or the agent gets incomplete inputs.**

2. **Media group buffering**: Multi-image/video messages arrive as separate updates with shared `media_group_id`. Buffer with short timeout, then process as single message.

3. **Inbound debouncing**: Batch rapid-fire messages from the same sender into a single agent request. Configurable delay.

4. **Markdown-to-HTML conversion**: Telegram uses its own HTML subset, not standard Markdown. OpenClaw has a full IR-based pipeline. We need similar conversion.

5. **HTML parse error fallback**: Try HTML formatting first, fall back to plain text if Telegram rejects it.

6. **Update offset persistence**: Persist last processed update ID to disk for restart resilience.

7. **API rate limiting**: Telegram has strict rate limits (~30 msgs/sec). Need throttling middleware.

### What This Means for Our Architecture

- **Drop the AgentInterface protocol idea**. Instead of forcing CLI and Telegram into a class hierarchy, define a normalized message type and per-channel adapters. Keep the `ConversationRunner` proposal (callback-based, aligns with OpenClaw's function-based adapter pattern).
- **Start with DMs only**. Group chat support is massive complexity (~800 lines in OpenClaw). Add later.
- **Keep our message queue for background forks**. OpenClaw doesn't need it (no background tasks), but our `self_fork(background=true)` pattern requires it. The queue is OBS-specific and should stay.

---

## 8. Insights from Telegram API Research

(Integrated from telegram-api-researcher's findings -- see `spikes/telegram-api-testing-research.md` for full details)

### Key API Facts

- **Max message length**: 4096 UTF-16 code units (after entity parsing). Use 4000 as safe limit (matches OpenClaw).
- **Caption length**: 1024 chars (for media messages).
- **Rate limits**: ~1 msg/s to same chat, 30 msg/s across chats. Not an issue for personal bot.
- **Formatting**: Convert agent Markdown to HTML (`parse_mode="HTML"`). Simpler escaping than MarkdownV2. Fall back to plain text on parse failure.
- **Bots cannot initiate conversations**: User must send `/start` first.
- **Bots cannot message bots**: Bot-to-bot testing is impossible.

### Library Recommendations

- **For the bot**: `python-telegram-bot` (PTB) v22+ -- most mature, well-documented, pure async, built-in polling with `Application.run_polling()`. This is the Python equivalent of OpenClaw's grammY.
- **For eval testing**: `Telethon` -- MTProto user client that can act as a real Telegram user. Has `Conversation` helper for send-and-wait patterns. Uses `StringSession` for persistent auth via env var.

### Testing Architecture

The `TelegramPlatform` implementation is straightforward:
- Uses Telethon to act as a real user talking to the bot
- Implements existing `Platform` protocol (same as `CLIPlatform`)
- Handles message splitting collection (wait 2-3s for all parts after each send)
- No "prompt" concept -- uses timeout-based waiting instead of prompt pattern
- Same judge agent, same scenario parser, same test runner -- only the Platform changes

### Telegram-Specific Challenges to Address

1. **Text fragment reassembly (inbound)**: Telegram clients split long pasted text into multiple messages. Need buffering with 1500ms timeout (from OpenClaw research).
2. **Response splitting (outbound)**: Split at paragraph/sentence boundaries, max 4000 chars per chunk, ~1s delay between chunks.
3. **Markdown-to-HTML conversion**: Agent outputs CommonMark, Telegram needs HTML subset. Need a converter with plain-text fallback.
4. **State isolation in evals**: CLI gets fresh process per test. Telegram bot is persistent. Need `/reset` command or accept persistent state.
5. **Slower round-trips**: Telegram has network latency. Scenarios need longer `Wait:` values.

### Required Credentials (Two-Bot Setup)

- Dev bot token (personal use) + test bot token (evals)
- Telegram API ID + hash (from my.telegram.org, for Telethon)
- StringSession for secondary account (generated once, stored as env var)

---

## 9. Final Synthesis: Recommended Architecture

### Phase 1: Pre-Telegram Refactoring

1. **Extract `ConversationRunner` from daemon.py**
   - Encapsulates: turn execution, continuation loop, background fork wake-up, pending message management
   - Callback-based output: `on_text(str)`, `on_status(StatusEvent)`, `on_turn_complete()`
   - Both daemon (SSE) and Telegram (message send) provide their own callbacks
   - This is the single biggest refactoring task

2. **Session registry for multi-chat**
   - `dict[str, tuple[SessionManager, HookState]]` keyed by conversation ID
   - CLI uses a single fixed key
   - Telegram uses `chat_id` as key
   - LRU eviction based on cache window expiry

3. **Separate StatusEvent serialization**
   - `to_sse()` stays for daemon
   - Add `to_dict()` for Telegram (or drop status events for Telegram V1)

### Phase 2: Telegram Bot Implementation

4. **New file: `src/obs_agent/telegram.py`**
   - Uses `python-telegram-bot` (PTB) v22+
   - Long polling via `Application.run_polling()`
   - Message handler → `ConversationRunner.run_turn()` with Telegram callbacks
   - Inbound: text fragment reassembly, media group buffering, inbound debouncing
   - Outbound: message splitting (4000 char limit), Markdown-to-HTML conversion, HTML parse fallback
   - DM-only for V1 (restrict to authorized `chat_id`)

5. **New file: `src/obs_agent/telegram_format.py`**
   - Markdown-to-HTML converter for Telegram's HTML subset
   - Message splitting with formatting preservation across chunks
   - Plain text fallback

6. **Config extension**
   - Add `TelegramConfig` (bot token, authorized chat ID, polling timeout)
   - Separate from `OBSConfig` or extend it

### Phase 3: Telegram Evals

7. **New file: `tests/evals/platform_telegram.py`**
   - `TelegramPlatform` implementing existing `Platform` protocol
   - Uses Telethon with `StringSession`
   - Multi-message collection with timeout-based waiting

8. **Scenario tweaks**
   - Increase `Wait:` values for Telegram-specific scenarios
   - New scenarios: long message splitting, `/stop` command, persistent state

### Architecture Diagram (Target State)

```
                                  [ConversationRunner]
                                    /              \
                                   /                \
[daemon.py]  -- SSE callbacks --  /                  \  -- Telegram callbacks -- [telegram.py]
     |                                                                               |
     |                                                                               |
[cli.py]                                                                    Telegram Bot API
(HTTP client)                                                              (long polling)
     |                                                                               |
     v                                                                               v
  Terminal                                                                    Telegram App
```

Both paths share: `session.py`, `hooks.py`, `tools.py`, `fork.py`, `prompt.py`, `config.py`, `commands.py`

### What NOT to Do

- **No abstract base class hierarchy** (learned from OpenClaw) -- use function-based adapters
- **No group chat support in V1** (learned from OpenClaw -- 800+ lines of complexity)
- **No message editing for streaming** (rate limits, complexity) -- use batch response instead
- **No media support in V1** -- text only, add incrementally later
- **Don't refactor cli.py** -- it's a working CLI client, leave it alone
- **Don't change the daemon's HTTP API** -- Telegram is a parallel path, not a replacement
