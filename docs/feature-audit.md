# OBS Agent Platform — Comprehensive Feature Audit

**Audit date:** 2026-03-11
**Codebase version:** 0.1.0
**Source location:** `/Users/breedoon/Documents/obs/`

---

## 1. Module Inventory

### 1.1 `src/obs_agent/__init__.py`
- **Purpose:** Package initialization. Declares `__version__ = "0.1.0"`.
- **Key exports:** `__version__`
- **Dependencies:** None

### 1.2 `src/obs_agent/config.py`
- **Purpose:** Central configuration dataclass and path resolution. Defines all tunable parameters, filesystem paths, Telegram bot configuration, and vault structure validation.
- **Key exports:**
  - `OBSConfig` — dataclass with `from_env()` classmethod factory reading ~20 env vars
  - `IMMUTABLE_PATTERNS` — list of file patterns that must never be written to
  - `_resolved(path)` — path expansion helper
  - `_is_within(path, parent)` — path containment check
- **Dependencies:** None (leaf module, pure stdlib)

### 1.3 `src/obs_agent/prompt.py`
- **Purpose:** Builds the system prompt by reading `CLAUDE.md` from the vault root. Minimal — delegates all prompt content to the vault file.
- **Key exports:**
  - `build_system_prompt(config) -> str`
- **Dependencies:** `config.OBSConfig` (type only)
- **Notes:** [UNCERTAIN] This module may be legacy — the `SessionManager` uses `setting_sources=["project"]` for SDK-native prompt loading, not `build_system_prompt()`. It may still be used by other callers or kept for backward compatibility.

### 1.4 `src/obs_agent/session.py`
- **Purpose:** Manages `ClaudeSDKClient` lifecycle for interactive multi-turn conversations. Handles connection creation, cache-window session resumption, reconnection, and `ClaudeAgentOptions` construction.
- **Key exports:**
  - `SessionManager` — primary session lifecycle class
- **Dependencies:** `_sdk_patch`, `hooks`, `tools`, `config` (type), `claude_agent_sdk`
- **Key details:**
  - `permission_mode = "bypassPermissions"` — agent runs without permission prompts
  - `setting_sources = ["project"]` — reads vault `.claude/` settings
  - Cache window default: 1000 hours (sessions effectively never expire)
  - `asyncio.Lock` serializes client access
  - Error recovery: `reconnect()`, `soft_reset()`, `async_reset()`, `reset()`

### 1.5 `src/obs_agent/daemon.py`
- **Purpose:** FastAPI HTTP server exposing the agent as a daemon with REST and SSE streaming endpoints.
- **Key exports:**
  - `ChatRequest`, `ChatResponse` — Pydantic models
  - `create_default_app()` — factory for uvicorn
  - `create_app(config)` — configurable factory
- **Dependencies:** `commands`, `events`, `hooks`, `runner`, `session`, `config` (type)
- **Endpoints:** `GET /health`, `POST /chat`, `POST /chat/stream`, `POST /chat/enqueue`, `POST /chat/interrupt`, `GET /commands`

### 1.6 `src/obs_agent/runner.py`
- **Purpose:** Core orchestration loop for a single conversation turn. Manages query, stream, queue continuations, background fork completion, and event yielding.
- **Key exports:**
  - `TextEvent`, `TurnEndEvent`, `DoneEvent` — event dataclasses
  - `RunnerEvent` — union type alias
  - `ConversationRunner` — main orchestration class
- **Dependencies:** `events`, `hooks`, `metrics`, `queueing`, `session`, `config` (type), `claude_agent_sdk`
- **Key details:**
  - 6-phase `run()`: pending injection → client acquisition → initial stream → continuation loop → background fork wait → final drain
  - Automatic reconnect on recoverable errors (preserves session ID)
  - Max 3 queue continuations per turn

### 1.7 `src/obs_agent/hooks.py`
- **Purpose:** SDK hook system. Implements security guards (immutable file protection, `.env` blocking), interrupt handling, message queue injection, notification forwarding, and stop-event lifecycle.
- **Key exports:**
  - `HookState` — shared mutable state (message queue, status queue, interrupt flag, background tasks, callback slots)
  - `HookPipeline` — chains check functions into SDK `HookCallback`
  - `CheckFn` — type alias for check functions
  - `on_pre_tool_use()` — standalone guard function
  - `create_hook_matchers(config, state)` — factory building full hook matcher dictionary
- **Dependencies:** `queueing`, `events` (lazy), `config` (type), `claude_agent_sdk.types`
- **Hook matchers:**
  - `PreToolUse`: interrupt check → immutable guard → queue check
  - `PostToolUse`: queue check
  - `Notification`, `SubagentStart`, `SubagentStop`: notification check
  - `Stop`: stop check

### 1.8 `src/obs_agent/tools.py`
- **Purpose:** MCP tool server for the agent. Registers 13 tools for task orchestration, scheduling, team messaging, and context introspection.
- **Key exports:**
  - `create_obs_tools(config, get_session_id, hook_state)` — factory returning MCP server
- **Dependencies:** `context_probe`, `context_stats`, `config` (type), `hooks` (type), `claude_agent_sdk`
- **Registered tools:** AgentTask, AgentTaskOutput, AgentTaskStop, ForkTask, ForkTaskOutput, ForkTaskStop, CronCreate, CronList, CronDelete, SendInboxMessage, ReadInbox, session_info, context_info

### 1.9 `src/obs_agent/fork.py`
- **Purpose:** `ForkRunner` class for running subtasks in forked Claude sessions using SDK's `query()`.
- **Key exports:**
  - `ForkRunner` — fork execution class with `run()` and `extract_memory()` methods
- **Dependencies:** `metrics`, `config` (type), `claude_agent_sdk`
- **Notes:** `extract_memory()` contains a hardcoded session-offboard prompt referencing paths (`.claude/memory/`, `.claude/topics/`) that may predate current vault structure. [UNCERTAIN: whether this is actively used]

### 1.10 `src/obs_agent/jsonl_fork.py`
- **Purpose:** JSONL-level session forking — copies the ancestor chain from a source session file into a new session file.
- **Key exports:**
  - `fork_session_jsonl(session_id, target_uuid, cwd, ...) -> str` — returns new session ID
- **Dependencies:** `context_jsonl`
- **Key details:** Traverses `parentUuid` chain, includes metadata entries, cycle detection, raises on missing ancestors

### 1.11 `src/obs_agent/context_jsonl.py`
- **Purpose:** Extracts token usage statistics from Claude session JSONL files for context window estimation.
- **Key exports:**
  - `JsonlUsageSnapshot` — frozen dataclass with token metrics
  - `find_session_jsonl(session_id, cwd, ...) -> Path | None`
  - `load_jsonl_usage_snapshot(session_id, cwd, ...) -> JsonlUsageSnapshot | None`
- **Dependencies:** None (leaf module)

### 1.12 `src/obs_agent/context_probe.py`
- **Purpose:** Authoritative context window measurement via Claude CLI `/context` command subprocess.
- **Key exports:**
  - `ContextProbe` — dataclass with `used_tokens`, `window_tokens`, `used_pct`
  - `parse_context_markdown(markdown) -> ContextProbe | None`
  - `probe_context_via_claude_cli(session_id, cwd, ...) -> ContextProbe | None`
- **Dependencies:** None (leaf module)
- **Key details:** 2-attempt retry with 0.3s delay, 12s timeout, fails silently

### 1.13 `src/obs_agent/context_stats.py`
- **Purpose:** Composition layer combining SDK result data, JSONL snapshots, and CLI probes into unified context statistics.
- **Key exports:**
  - `build_context_snapshot(...)` — builds normalized snapshot dict
  - `format_context_snapshot_lines(snapshot)` — 24-field key-value lines
  - `format_context_snapshot_compact(snapshot)` — one-line summary
  - `apply_context_probe(snapshot, probe)` — overlays CLI probe data
- **Dependencies:** `context_jsonl`, `context_probe` (type)

### 1.14 `src/obs_agent/events.py`
- **Purpose:** SSE status event system and tool-use summary formatting.
- **Key exports:**
  - `StatusEvent` — frozen dataclass with `type`, `summary`, `count`, `messages`; `to_sse()` method
  - `summarize_tool_use(tool_name, tool_input) -> str`
- **Dependencies:** `config._DEFAULT_VAULT`
- **Key details:** Specialized summaries for 11 known tools; fallback JSON dump for unknown tools

### 1.15 `src/obs_agent/metrics.py`
- **Purpose:** Lightweight metrics logging for SDK responses via Python standard logging.
- **Key exports:**
  - `log_result(message, label="query")` — logs token usage, cost, timing
- **Dependencies:** None (leaf module)
- **Key details:** Wrapped in blanket `try/except` — never breaks main flow

### 1.16 `src/obs_agent/queueing.py`
- **Purpose:** Shared data types for the message queue system.
- **Key exports:**
  - `QueuedMessage` — frozen dataclass with `text`, `telegram_message_id`, `reply_to_message_id`
  - `coerce_queued_message(item)` — normalizes strings to `QueuedMessage`
  - `queued_texts(items)` — extracts plain text list
- **Dependencies:** None (leaf module)

### 1.17 `src/obs_agent/commands.py`
- **Purpose:** Command registry mapping command names to handler functions.
- **Key exports:**
  - `CommandResult` — dataclass with `success` and `message`
  - `CommandRegistry` — dispatcher for `stop`, `quit`, `enqueue` commands
- **Dependencies:** `hooks.HookState`

### 1.18 `src/obs_agent/input.py`
- **Purpose:** Pluggable I/O channel abstraction for the CLI.
- **Key exports:**
  - `InputChannel` — Protocol with 5 methods
  - `SimpleChannel` — minimal `input()` implementation
  - `PromptToolkitChannel` — rich terminal with async prompt, Esc+Enter for newlines
  - `MAX_INPUT_LENGTH = 100_000`
- **Dependencies:** None (optional `prompt_toolkit`)

### 1.19 `src/obs_agent/cli.py`
- **Purpose:** CLI REPL client. Auto-starts daemon, streams SSE responses, supports concurrent input during streaming.
- **Key exports:**
  - `parse_slash_command(text)` — input parser
  - `check_daemon(base_url)` — health check
  - `start_daemon(config)` — launches daemon subprocess
  - `send_message(message, base_url)` — synchronous one-shot
  - `stream_message(message, base_url, channel)` — SSE streaming
  - `stream_with_input(message, base_url, channel)` — concurrent input+stream
  - `async_main()`, `main()` — entry points
- **Dependencies:** `config`, `input`

### 1.20 `src/obs_agent/telegram.py` (6,472 lines — largest module)
- **Purpose:** Complete Telegram bot adapter. Handles message receiving, multi-turn conversation processing, fork/agent task orchestration, scheduling, transport, and all Telegram API interaction.
- **Key exports:**
  - `TelegramBot` — core bot class with all state management and orchestration
  - `run_telegram_bot(config)` — async entry point
  - `create_telegram_app(config)` — application factory
  - `FragmentBuffer` — reassembles Telegram auto-split messages
  - `MediaGroupBuffer` — batches media group (album) messages
  - `TelegramRoute` — frozen dataclass `(chat_id, thread_id)`
  - `TelegramSessionState` — per-route runtime state
  - `_ForkTaskRecord` — agent-launched child topic lifecycle
  - `_TopicScheduleRecord` — per-topic schedule
- **Dependencies:** `runner`, `events`, `hooks`, `session`, `config`, `queueing`, `context_probe`, `context_stats`, `context_jsonl`, `jsonl_fork`, `telegram_format`, `telegram_ingest`, `telegram_state_store`

### 1.21 `src/obs_agent/telegram_format.py`
- **Purpose:** Markdown → Telegram HTML conversion and message splitting.
- **Key exports:**
  - `md_to_telegram_html(text) -> str`
  - `split_message(html, limit=4000) -> list[str]`
  - `MAX_MESSAGE_LENGTH = 4000`
- **Dependencies:** None (uses `markdown_it`)

### 1.22 `src/obs_agent/telegram_ingest.py`
- **Purpose:** Normalizes incoming Telegram media (photos, docs, voice, video, stickers) into unified text for the agent. Downloads attachments, optionally transcribes voice.
- **Key exports:**
  - `TelegramInboundNormalizer` — main normalizer class
  - `DownloadedAttachment` — attachment metadata
  - `NormalizedInbound` — result with `agent_text`, `attachments`, `user_warnings`
- **Dependencies:** None (standalone)

### 1.23 `src/obs_agent/telegram_main.py`
- **Purpose:** Entry point for standalone Telegram bot process.
- **Key exports:**
  - `main()` — loads `.env`, configures logging, validates config, runs bot
- **Dependencies:** `config`, `telegram`

### 1.24 `src/obs_agent/telegram_state_store.py`
- **Purpose:** SQLite persistence for Telegram bot runtime state. Write-through persistence of routes, message bindings, session heads, team workers, and schedules.
- **Key exports:**
  - `TelegramStateStore` — main persistence class (6 tables)
  - `PersistedRouteState`, `PersistedMessageBinding`, `PersistedSystemMessage`, `PersistedTeamWorkerState`, `PersistedTopicSchedule` — row dataclasses
  - `TelegramStateSnapshot` — bulk snapshot
- **Dependencies:** None (standalone, uses `sqlite3`)

### 1.25 `src/obs_agent/_sdk_patch.py`
- **Purpose:** Monkey-patch for Claude Agent SDK's `parse_message` to preserve UUID fields as `_raw_uuid` on parsed message objects.
- **Key exports:**
  - `ensure_raw_uuid_patch()` — applies patch once
- **Dependencies:** `claude_agent_sdk._internal.message_parser`

---

## 2. Feature Catalog

### 2.1 Interactive Multi-Turn Conversations
- **What it does:** Maintains persistent conversations with the Claude agent across multiple user messages, with automatic session resumption.
- **How it works:** `SessionManager` wraps `ClaudeSDKClient`. Sessions are identified by UUID. A 1000-hour cache window means sessions are almost always resumed on reconnect. `ConversationRunner.run()` orchestrates the full turn: send query, stream response, process queue, wait for background forks.
- **Key code:** `session.py`, `runner.py`
- **Configuration:** `OBS_CACHE_WINDOW_SECONDS` (default 3,600,000 — ~1000 hours)
- **Known limitations:** None apparent from code.

### 2.2 CLI REPL Interface
- **What it does:** Terminal-based interactive chat with the agent. Auto-starts the daemon, supports streaming responses, concurrent input during streaming, and slash commands.
- **How it works:** `cli.py` checks if daemon is running, starts it if not via `uvicorn` subprocess. Streams SSE from `/chat/stream`. Uses `asyncio.wait(FIRST_COMPLETED)` for concurrent input+streaming. Two input channel implementations: `PromptToolkitChannel` (rich) and `SimpleChannel` (basic).
- **Key code:** `cli.py`, `input.py`
- **Configuration:** `OBS_EVAL_PROMPT` (custom prompt string), `OBS_SIMPLE_INPUT` (force simple channel)
- **CLI entry point:** `obs-agent` (registered in `pyproject.toml`)
- **Slash commands:** `/stop` (interrupt agent), `/quit` (exit REPL)

### 2.3 HTTP Daemon API
- **What it does:** FastAPI server exposing the agent via REST and SSE endpoints. Powers both CLI and can serve other HTTP clients.
- **How it works:** `daemon.py` creates a FastAPI app with 5 endpoints. Uses `ConversationRunner` for conversation orchestration. Maintains `pending_messages` across turns.
- **Key code:** `daemon.py`
- **Endpoints:**
  - `GET /health` — returns status + version
  - `POST /chat` — synchronous (non-streaming) chat
  - `POST /chat/stream` — SSE streaming chat
  - `POST /chat/enqueue` — queue message for injection at next hook boundary
  - `POST /chat/interrupt` — interrupt agent + SDK-level stop
  - `GET /commands` — list available commands
- **Configuration:** `OBS_DAEMON_HOST` (default `127.0.0.1`), `OBS_DAEMON_PORT` (default `7832`)

### 2.4 Telegram Bot Adapter
- **What it does:** Full-featured Telegram bot that receives messages, processes them through the Claude agent, and sends chronological per-turn responses back. Supports forum topics, multi-bot sending, media handling, and voice transcription.
- **How it works:** `telegram.py` (`TelegramBot`) uses `python-telegram-bot` for polling. Each `(chat_id, thread_id)` is a `TelegramRoute` with its own `SessionManager`, `HookState`, and state. Outbound messages go through a priority queue with rate limiting and round-robin sender rotation. Fragment and media group buffers handle Telegram message batching.
- **Key code:** `telegram.py`, `telegram_main.py`, `telegram_format.py`, `telegram_ingest.py`, `telegram_state_store.py`
- **Configuration:** `OBS_TELEGRAM_BOT_TOKEN`, `OBS_TELEGRAM_BOT_TOKENS` (comma-separated for multi-bot), `OBS_TELEGRAM_AUTHORIZED_USER_ID`, `OBS_TELEGRAM_NOTIFY_USERNAME`, `OBS_TELEGRAM_TRANSCRIPTION_SCRIPT`, `OBS_TELEGRAM_TEMP_ROOT`, `OBS_TELEGRAM_STATE_DB`, `OBS_TELEGRAM_LOG_POLLING`, `OBS_RUNTIME_LOG_FILE`
- **Slash commands (Telegram):** `/clear`, `/unschedule`, `/stop`, `/context`, `/report`, `/fork`, `/delete`
- **Known limitations:** Telegram 4096 char message limit (handled by splitting), rate limiting (handled by backoff + multi-bot rotation), no nested topics

### 2.5 Authorization / Auth Guard
- **What it does:** Restricts Telegram bot access to an allowlisted set of Telegram user IDs.
- **How it works:** `_is_authorized()` in `telegram.py` checks incoming message user IDs against `OBS_TELEGRAM_AUTHORIZED_USER_ID`. Empty allowlist means deny-by-default (nobody can use the bot).
- **Key code:** `telegram.py`
- **Configuration:** `OBS_TELEGRAM_AUTHORIZED_USER_ID`

### 2.6 Immutable File Guard
- **What it does:** Prevents the agent from writing to protected files (meeting transcripts in `Sources/Meeting Notes/`, `.env` files).
- **How it works:** `hooks.py` `on_pre_tool_use()` intercepts `Write`, `Edit`, `NotebookEdit` tool calls. Checks file paths against `IMMUTABLE_PATTERNS` (from `config.py`) and `_BLOCKED_FILE_PATTERNS` (`.env`). Returns `permissionDecision: "deny"` if matched.
- **Key code:** `hooks.py`, `config.py`
- **Configuration:** `IMMUTABLE_PATTERNS` in `config.py` (currently `["Misc/Meeting Notes"]`; vault CLAUDE.md references `Sources/`)

### 2.7 Message Queue Injection
- **What it does:** Allows users to send additional messages while the agent is processing a response. Messages are injected at the next hook boundary as `additionalContext`.
- **How it works:** `HookState.message_queue` (asyncio.Queue) is populated by CLI enqueue endpoint or Telegram message handler. The queue check in `HookPipeline` drains messages and injects them as additional context. `ConversationRunner.run()` processes up to 3 continuation batches after the initial response.
- **Key code:** `hooks.py` (`_make_queue_check`), `runner.py` (continuation loop), `queueing.py`
- **Configuration:** `max_queue_continuations = 3` in config
- **Known limitations:** Messages with `reply_to_message_id` are deferred to `pending_messages` rather than injected immediately.

### 2.8 Interrupt / Stop
- **What it does:** Allows the user to interrupt the agent mid-response.
- **How it works:** Sets `HookState.interrupt_flag = True`. The interrupt check in `HookPipeline` returns `continue_: False` at the next hook boundary. The daemon endpoint also calls `client.interrupt()` for immediate SDK-level stop.
- **Key code:** `hooks.py` (`_make_interrupt_check`), `commands.py`, `daemon.py` (`/chat/interrupt`)

### 2.9 Agent-Initiated Forking (AgentTask / ForkTask MCP Tools)
- **What it does:** Allows the agent to delegate subtasks to child sessions running in separate Telegram forum topics. Supports forking from current context (`fork=true`) or starting fresh (`fork=false`).
- **How it works:** The agent calls `AgentTask` or `ForkTask` MCP tools (defined in `tools.py`). These validate parameters and delegate to `hook_state.fork_task_launcher` — a callback injected by the Telegram adapter. The Telegram bot creates a new forum topic, forks the session JSONL (via `jsonl_fork.py`), runs the child session, monitors lifecycle (idle detection, heartbeats), and queues completion results back to the parent.
- **Key code:** `tools.py` (MCP tool definitions), `telegram.py` (launch/monitor/complete lifecycle), `jsonl_fork.py` (JSONL-level forking)
- **Key parameters:** `prompt`, `description`, `fork` (bool), `resume` (session ID), `timeout_ms`, `max_turns`, `run_in_background` (always true), `name`, `team_name`
- **Known limitations:** All tasks run in background (synchronous not supported). Recursion depth not explicitly limited but practical limit exists.

### 2.10 JSONL Session Forking
- **What it does:** Creates a new session JSONL file by copying the ancestor chain from a source session up to a specific message UUID.
- **How it works:** `fork_session_jsonl()` reads the source JSONL, traverses `parentUuid` chain from target UUID to root, copies ancestor chain + metadata to new file.
- **Key code:** `jsonl_fork.py`, `context_jsonl.py` (`find_session_jsonl`)
- **Known limitations:** Missing ancestors raise `KeyError` (fails hard).

### 2.11 SDK-Level Session Forking (ForkRunner)
- **What it does:** Runs subtasks in forked Claude sessions using SDK's `query()` function.
- **How it works:** `ForkRunner.run()` creates `ClaudeAgentOptions` with `resume=session_id, fork_session=True` and streams the response.
- **Key code:** `fork.py`
- **Notes:** [UNCERTAIN] Whether `ForkRunner` is actively used at runtime or has been superseded by the JSONL fork + AgentTask approach. `extract_memory()` references old vault paths.

### 2.12 Per-Topic Scheduling (Cron System)
- **What it does:** Allows creating recurring schedules for Telegram topics — interval-based (inactivity-triggered), cron-based (wall-clock), or on-topic-stop triggers.
- **How it works:** Agent calls `CronCreate` MCP tool (in `tools.py`) which delegates to `hook_state.cron_creator` callback in the Telegram adapter. Supports windowed execution (`from`/`until`), max runs, retry policies, and inheritance to child topics. Schedules are persisted in SQLite via `TelegramStateStore`. Two runtime trigger channels: poll tick for intervals, SDK Stop hook for on-topic-stop.
- **Key code:** `tools.py` (CronCreate/List/Delete), `telegram.py` (schedule handlers), `telegram_state_store.py` (persistence)
- **Configuration:** Settings in `.claude/settings.json` under `obs.scheduling.defaults` and `obs.retry`
- **MCP tools:** `CronCreate`, `CronList`, `CronDelete`

### 2.13 Team / Inbox Messaging
- **What it does:** Enables inter-agent messaging via JSON inbox files on disk. Supports team creation with member rosters.
- **How it works:** `SendInboxMessage` tool writes to `~/.claude/teams/{team_name}/inboxes/{recipient}.json`. `ReadInbox` reads from the same files. Per-file async locks prevent corruption. Team config at `~/.claude/teams/{team_name}/config.json`. The Telegram adapter integrates team worker support with member tracking and state persistence.
- **Key code:** `tools.py` (SendInboxMessage, ReadInbox), `telegram.py` (team worker integration), `telegram_state_store.py` (team worker persistence)
- **MCP tools:** `SendInboxMessage`, `ReadInbox`

### 2.14 Context / Session Introspection
- **What it does:** Provides the agent with visibility into its own context window usage, session metrics, and token consumption.
- **How it works:** Three-tier estimation: (1) JSONL-backed usage from session files, (2) SDK result data fallback, (3) authoritative CLI probe via `claude -p /context`. Formatted as key-value lines (24 fields) or compact one-liner.
- **Key code:** `tools.py` (session_info, context_info), `context_jsonl.py`, `context_probe.py`, `context_stats.py`
- **MCP tools:** `session_info`, `context_info`
- **Configuration:** `context_window_estimate_tokens = 200_000` in config

### 2.15 Telegram File / Media Ingestion
- **What it does:** Receives any Telegram file/media type, downloads to temp directory, auto-transcribes voice messages, and presents everything as structured text to the agent.
- **How it works:** `TelegramInboundNormalizer` downloads attachments to `/tmp/obs-agent/<boot-id>/<chat-id>/<scope-id>/`. Voice messages are transcribed via external script. Agent-facing representation uses `<system-note>` XML blocks. Media groups are aggregated into one logical turn.
- **Key code:** `telegram_ingest.py`, `telegram.py`
- **Supported types:** photo, document, video, voice, audio, video_note, animation, sticker
- **Configuration:** `OBS_TELEGRAM_TRANSCRIPTION_SCRIPT`, `OBS_TELEGRAM_TEMP_ROOT` (default `/tmp/obs-agent`)

### 2.16 Telegram Multi-Topic Support
- **What it does:** Routes conversations by `(chat_id, thread_id)`. Each topic has its own session, hook state, and pending queue. Supports forum groups with per-topic isolation.
- **How it works:** `TelegramRoute` (frozen dataclass) identifies routes. `TelegramSessionState` holds per-route runtime state. `TelegramBot` maintains a registry mapping routes to states. Inline reply-forks stay local; `/fork` creates new topics.
- **Key code:** `telegram.py`
- **Topic commands:** `/fork` (reply mode or head mode), `/clear`, `/stop`, `/context`, `/delete`

### 2.17 Telegram Transport Queue
- **What it does:** Manages all outbound Telegram sends through a priority queue with rate limiting, exponential backoff, and multi-bot rotation.
- **How it works:** Single `asyncio.PriorityQueue` with priority levels: system (0) > assistant (10) > observability (30). Dedicated `_transport_worker_loop` processes operations with per-chat rate limiting. Supports blacklisting bots per-chat if they lack permissions.
- **Key code:** `telegram.py` (`_TransportSendOp`, `_TransportEnvelope`, transport worker)
- **Constants:** `_CHUNK_DELAY_SECONDS = 1.0`, `_BACKGROUND_POLL_SECONDS = 3.0`

### 2.18 Telegram Fragment Reassembly
- **What it does:** Reassembles Telegram auto-split long messages (>4096 chars) and batches rapid same-user messages within a quiet period.
- **How it works:** `FragmentBuffer` buffers fragments with ~1.0s gap detection. `MediaGroupBuffer` handles album messages within ~0.75s window.
- **Key code:** `telegram.py` (`FragmentBuffer`, `MediaGroupBuffer`)
- **Constants:** `_FRAGMENT_MAX_GAP_SECONDS = 1.0`, `_FRAGMENT_MIN_PART_LENGTH = 4000`

### 2.19 Telegram Message Binding
- **What it does:** Maps every Telegram message ID to its corresponding JSONL UUID in the conversation session, enabling reply-based forking and debug traceability.
- **How it works:** `_TelegramMessageBinding` maps `(chat_id, message_id)` → `(session_id, jsonl_uuid, role)`. Bindings are persisted via `TelegramStateStore`. The `_sdk_patch.py` monkey-patch preserves UUIDs on parsed SDK messages to enable this mapping.
- **Key code:** `telegram.py`, `telegram_state_store.py`, `_sdk_patch.py`

### 2.20 Telegram State Persistence
- **What it does:** SQLite-backed persistence allowing the bot to survive restarts with continuity of sessions, bindings, schedules, and team state.
- **How it works:** `TelegramStateStore` uses SQLite with WAL mode. Six tables: `route_state`, `message_binding`, `session_head`, `system_message`, `team_worker_state`, `topic_schedule`. Write-through pattern (autocommit). Schema migrations via `ALTER TABLE ADD COLUMN`.
- **Key code:** `telegram_state_store.py`
- **Configuration:** `OBS_TELEGRAM_STATE_DB` (default `.obs-agent/state/telegram.db`)
- **Features:** Pruning by retention period, snapshot loading for bulk restore

### 2.21 Observability / Status Events
- **What it does:** Streams real-time status updates (tool use summaries, queue delivery notices, fork lifecycle events) to clients via SSE.
- **How it works:** `StatusEvent` dataclass with `to_sse()` method. `summarize_tool_use()` creates human-readable summaries for tool invocations. Events flow through `HookState.status_queue`. Telegram adapter coalesces tool-call-only turns (1.5s window) before delivery.
- **Key code:** `events.py`, `hooks.py`, `runner.py`, `telegram.py`

### 2.22 Case Reports
- **What it does:** The `/report` Telegram command writes a debug case file with full routing, session, and binding metadata.
- **How it works:** Writes to `.claude/reports/cases/` in the vault.
- **Key code:** `telegram.py`

### 2.23 Markdown → Telegram HTML Conversion
- **What it does:** Converts Markdown to Telegram-compatible HTML subset and splits long messages into chunks.
- **How it works:** Uses `markdown-it-py` for parsing, then post-processes: tables → `<pre>` blocks, lists → bullet/numbered lines, headings → bold text, code blocks simplified. Splits at 4000 chars respecting paragraph/line/word boundaries, never splitting `<pre>` blocks if possible.
- **Key code:** `telegram_format.py`

### 2.24 Memory Extraction (Fork-Based)
- **What it does:** Extracts decisions, information, actions, and open threads from a conversation and persists them to vault files.
- **How it works:** `ForkRunner.extract_memory()` forks the session with a hardcoded prompt instructing the agent to review the conversation and write memory logs. Limited to 10 turns.
- **Key code:** `fork.py`
- **Notes:** [UNCERTAIN] Whether actively used. References old vault paths. The vault's skill-based session-offboard may have superseded this.

### 2.25 System Prompt from Vault
- **What it does:** The agent's system prompt is sourced from the vault's `CLAUDE.md` file and `.claude/` directory structure.
- **How it works:** Two mechanisms: (1) `prompt.py` reads `CLAUDE.md` directly, (2) `SessionManager` sets `setting_sources=["project"]` which tells the SDK to read `.claude/` for settings, skills, and project-level CLAUDE.md.
- **Key code:** `prompt.py`, `session.py`

### 2.26 Metrics Logging
- **What it does:** Logs token usage, cost, and timing from SDK responses via Python standard logging.
- **How it works:** `log_result()` extracts metrics from SDK message objects and logs them. Wrapped in `try/except` to never break the main flow.
- **Key code:** `metrics.py`

### 2.27 Eval System
- **What it does:** End-to-end evaluation framework using real runtime adapters (Telegram, CLI), real vault clones, and LLM-as-judge assessment.
- **How it works:** Markdown scenario files define user journeys. Two lanes: deterministic (assertions) and judge (behavioral evaluation via `ClaudeSDKClient`). Three profiles: smoke, feature, full. Telegram evals run sequentially in one aggregate test via Telethon userbot. CLI evals use pexpect.
- **Key code:** `tests/evals/` directory
- **Configuration:** `OBS_EVAL_ENABLE_CLI`, `OBS_EVAL_PROFILE`, `OBS_TG_SCENARIOS`, `OBS_EVAL_PROMPT`
- **Agent:** `.claude/agents/eval-guardian.md` — adversarial QA auditor with veto power

---

## 3. MCP Tools

All tools are defined in `src/obs_agent/tools.py` via `create_obs_tools()`.

### 3.1 `AgentTask`
- **Parameters:** `prompt` (str, required), `description` (str), `fork` (bool, default varies), `resume` (str — session ID), `timeout_ms` (str/int), `max_turns` (str/int), `run_in_background` (str/bool), `name` (str), `team_name` (str)
- **What it does:** Launches a delegated child agent in a new Telegram topic. `fork=true` clones current session context; `fork=false` starts fresh.
- **Implementation:** Validates params, delegates to `hook_state.fork_task_launcher` callback (injected by Telegram adapter). Always runs in background.

### 3.2 `AgentTaskOutput`
- **Parameters:** `task_id` (str, required), `block` (str/bool), `timeout` (str/int)
- **What it does:** Inspects a running or completed child agent task. Returns status and output.
- **Implementation:** Delegates to `hook_state.fork_task_outputter`.

### 3.3 `AgentTaskStop`
- **Parameters:** `task_id` (str, required), `shell_id` (str, deprecated)
- **What it does:** Stops a running child agent task.
- **Implementation:** Delegates to `hook_state.fork_task_stopper`.

### 3.4 `ForkTask`
- **Parameters:** Same as `AgentTask`
- **What it does:** Compatibility alias for `AgentTask` with `fork=true` as default.
- **Implementation:** Calls `_launch_task(args, "ForkTask", default_fork=True)`.

### 3.5 `ForkTaskOutput`
- **Parameters:** Same as `AgentTaskOutput`
- **What it does:** Compatibility alias for `AgentTaskOutput`.

### 3.6 `ForkTaskStop`
- **Parameters:** Same as `AgentTaskStop`
- **What it does:** Compatibility alias for `AgentTaskStop`.

### 3.7 `CronCreate`
- **Parameters:** `schedule_mode` (str: "interval" | "cron"), `cron` (str — 5-field expression), `interval_seconds` (str/int), `prompt` (str, required), `reset_session` (str/bool), `description` (str), `max_runs` (str/int), `from` (str — ISO timestamp), `until` (str — ISO timestamp), `inherit` (str: "none" | "fork" | "all"), `run_mode` (str, deprecated alias for `reset_session`)
- **What it does:** Creates a per-topic schedule. Interval mode is inactivity-based; cron mode is wall-clock-based.
- **Implementation:** Delegates to `hook_state.cron_creator`.

### 3.8 `CronList`
- **Parameters:** None
- **What it does:** Lists schedules for the current topic route.
- **Implementation:** Delegates to `hook_state.cron_lister`.

### 3.9 `CronDelete`
- **Parameters:** `id` (str, required — schedule ID)
- **What it does:** Deletes a schedule by ID.
- **Implementation:** Delegates to `hook_state.cron_deleter`.

### 3.10 `SendInboxMessage`
- **Parameters:** `team_name` (str, required), `recipient` (str, required), `content` (str, required), `summary` (str, required), `sender` (str, required)
- **What it does:** Writes a JSON message to a team inbox file at `~/.claude/teams/{team_name}/inboxes/{recipient}.json`.
- **Implementation:** Reads existing JSON array, appends new message with `id`, `sender`, `content`, `summary`, `timestamp`, `read: false`. Uses per-file async locks. Optionally calls `hook_state.inbox_message_notifier`.

### 3.11 `ReadInbox`
- **Parameters:** `team_name` (str, required), `agent` (str, required), `include_read` (str/bool), `mark_read` (str/bool), `limit` (str/int)
- **What it does:** Reads messages from a team inbox JSON file. Can filter by read status and mark messages as read.
- **Implementation:** Reads from `~/.claude/teams/{team_name}/inboxes/{agent}.json`. Uses per-file async locks.

### 3.12 `session_info`
- **Parameters:** None
- **What it does:** Returns current session and context usage snapshot.
- **Implementation:** Calls `_render_context_and_session()` which builds snapshot from `hook_state.last_result_data`, optionally augments with CLI context probe. Returns 24 key-value lines.

### 3.13 `context_info`
- **Parameters:** None
- **What it does:** Identical to `session_info`. Both exist for different invocation contexts.
- **Implementation:** Same as `session_info`.

---

## 4. CLI Commands

### 4.1 `obs-agent`
- **Syntax:** `obs-agent` or `obs-agent --help`
- **What it does:** Starts the CLI REPL. Auto-starts the daemon if not running. Streams responses via SSE.
- **Implementation:** `cli.py:main()` → `async_main()`
- **Registered in:** `pyproject.toml` `[project.scripts]`

### 4.2 `/stop` (CLI slash command)
- **Syntax:** `/stop` (typed during REPL)
- **What it does:** Interrupts the agent at the next hook boundary. During streaming, sends interrupt to daemon.
- **Implementation:** `cli.py:_handle_input_during_stream()` → `POST /chat/interrupt`

### 4.3 `/quit` (CLI slash command)
- **Syntax:** `/quit` (typed during REPL)
- **What it does:** Interrupts the agent and exits the REPL loop. If CLI started the daemon, terminates the daemon process.
- **Implementation:** `cli.py:async_main()` loop exit

### 4.4 Telegram Bot Commands
- `/clear` — Clears current topic session
- `/unschedule` — Removes schedules for current topic
- `/stop` — Interrupts agent in current topic
- `/context` — Shows context window usage
- `/report` — Writes debug case report to vault
- `/fork` — Creates a new forum topic (reply mode or head mode)
- `/delete` — Deletes current topic
- **Implementation:** `telegram.py` (`_set_bot_commands`, various handlers)

---

## 5. Configuration & Environment

### 5.1 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBS_VAULT_PATH` | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/T` | Obsidian vault path |
| `OBS_DAEMON_HOST` | `127.0.0.1` | Daemon bind host |
| `OBS_DAEMON_PORT` | `7832` | Daemon bind port |
| `OBS_CACHE_WINDOW_SECONDS` | `3600000` (~1000 hours) | Session cache window |
| `OBS_TELEGRAM_BOT_TOKEN` | — | Primary Telegram bot token |
| `OBS_TELEGRAM_PROD_BOT_TOKEN` | — | Alias for primary bot token |
| `OBS_TELEGRAM_BOT_TOKENS` | — | Comma-separated list for multi-bot sending |
| `OBS_TELEGRAM_AUTHORIZED_USER_ID` | — | Allowed Telegram user ID(s) |
| `OBS_TELEGRAM_NOTIFY_USERNAME` | — | Telegram username for notifications |
| `OBS_TELEGRAM_TEMP_ROOT` | `/tmp/obs-agent` | Temp directory for downloads |
| `OBS_TELEGRAM_STATE_DB` | `.obs-agent/state/telegram.db` | SQLite state database path |
| `OBS_TELEGRAM_TRANSCRIPTION_SCRIPT` | — | Path to voice transcription script |
| `OBS_TELEGRAM_LOG_POLLING` | `0` | Enable/disable Telegram polling logs |
| `OBS_RUNTIME_LOG_FILE` / `OBS_TELEGRAM_LOG_FILE` | — | File-based logging path |
| `OBS_EVAL_ENABLE_CLI` | — | Enable CLI eval scenarios |
| `OBS_EVAL_PROFILE` | `smoke` | Eval profile (smoke/feature/full) |
| `OBS_EVAL_PROMPT` | `"> "` | CLI prompt string (eval collision avoidance) |
| `OBS_TG_SCENARIOS` | — | Comma-separated Telegram scenario filter |
| `OBS_SIMPLE_INPUT` | — | Force simple input channel in CLI |
| `TELEGRAM_API_ID` | — | Telethon API ID (for evals) |
| `TELEGRAM_API_HASH` | — | Telethon API hash (for evals) |
| `TELEGRAM_SESSION` | — | Telethon string session (for evals) |
| `TELEGRAM_TEST_BOT_USERNAME` | — | Test bot username (for evals) |
| `TELEGRAM_TEST_USER_ID` | — | Test user ID (for evals) |
| `OBS_TELEGRAM_TEST_BOT_TOKEN` | — | Test bot token (for evals) |
| `OBS_TELEGRAM_TEST_NOTIFY_USERNAME` | — | Test notification username |

### 5.2 Config Constants (in `config.py`)

| Constant | Value | Description |
|----------|-------|-------------|
| `max_queue_continuations` | `3` | Max queued message batches per turn |
| `bg_fork_timeout` | `600.0` (10 min) | Background fork task timeout |
| `max_buffer_size` | `10_000_000` (10 MB) | SDK JSON buffer limit |
| `context_window_estimate_tokens` | `200_000` | Context window size estimate |

### 5.3 Config Files
- **`.env`** — Environment variables (loaded by `telegram_main.py`)
- **`.claude/settings.json`** — Scheduling defaults, retry policies (read by Telegram adapter)
- **`.claude/agents/eval-guardian.md`** — Eval guardian agent definition
- **`fixture_vault/`** — Ephemeral vault clone for eval testing

### 5.4 Runtime State
- **`.obs-agent/state/telegram.db`** — SQLite state database (route states, bindings, schedules, team workers)
- **`.obs-agent/logs/`** — Runtime log files
- **`/tmp/obs-agent/`** — Temp directory for Telegram file downloads (purged on boot)
- **`~/.claude/projects/`** — Claude CLI session JSONL files
- **`~/.claude/teams/`** — Team config and inbox files

---

## 6. Cross-Module Data Flow

### 6.1 Telegram Message → Agent Response

```
User sends Telegram message
    ↓
python-telegram-bot polling picks it up
    ↓
TelegramBot handler: auth check → FragmentBuffer / MediaGroupBuffer reassembly
    ↓
TelegramInboundNormalizer: download attachments, transcribe voice → NormalizedInbound
    ↓
Acquire per-route asyncio.Lock (serialize per-topic)
    ↓
Resolve TelegramRoute → TelegramSessionState (or create new)
    ↓
ConversationRunner.run(message):
    ├── Phase 1: Prepend pending_messages from previous turns
    ├── Phase 2: SessionManager.get_client() → ClaudeSDKClient (resume or create)
    ├── Phase 3: client.send_message(query) → stream response
    │   ├── SDK hooks fire at each tool boundary:
    │   │   ├── PreToolUse: interrupt check → immutable guard → queue check (inject new messages)
    │   │   ├── PostToolUse: queue check
    │   │   ├── Notification/SubagentStart/SubagentStop: push StatusEvents
    │   │   └── Stop: update session_id, notify stop listeners
    │   ├── For each SDK message content block:
    │   │   ├── TextBlock → yield TextEvent
    │   │   ├── ToolUseBlock → yield StatusEvent (summarized)
    │   │   └── ThinkingBlock → yield StatusEvent
    │   └── After each message → yield TurnEndEvent
    ├── Phase 4: Continuation loop (drain queue, send up to 3 batches)
    ├── Phase 5: Wait for background fork tasks (asyncio.wait FIRST_COMPLETED)
    └── Phase 6: Final queue drain → pending_messages for next turn
    ↓
yield DoneEvent
    ↓
TelegramBot: collect events per turn, flush on TurnEndEvent
    ↓
telegram_format: md_to_telegram_html() → split_message()
    ↓
Transport queue (PriorityQueue): priority-ordered, rate-limited
    ↓
Transport worker: round-robin sender bot selection → bot.send_message()
    ↓
User receives Telegram message(s)
```

### 6.2 Agent Dispatches a Fork (AgentTask)

```
Agent calls AgentTask MCP tool during response
    ↓
tools.py: _launch_task() validates parameters
    ↓
Delegates to hook_state.fork_task_launcher (callback from TelegramBot)
    ↓
TelegramBot._handle_fork_task_launch():
    ├── Create _ForkTaskRecord
    ├── Create new Telegram forum topic (via Telegram API)
    ├── Resolve child TelegramRoute
    ├── Create child TelegramSessionState
    ├── If fork=true: fork_session_jsonl() copies parent chain to new JSONL
    ├── Send initial service message to child topic
    ├── Start child ConversationRunner with prompt
    ├── Monitor child lifecycle (heartbeat, idle detection)
    └── On completion: queue result back to parent's HookState.message_queue
    ↓
Parent ConversationRunner.run() Phase 5 picks up the completed fork
    ↓
Parent agent receives fork results as "(Background fork results arrived...)"
```

### 6.3 Cron Schedule Fires

```
Schedule tick (interval-based poll or SDK Stop hook event)
    ↓
TelegramBot schedule handler:
    ├── Check schedule_mode (interval vs cron vs on_topic_stop)
    ├── Check window (from/until), max_runs, overlap
    ├── Acquire per-route lock
    ├── Set schedule_run_active = True
    ├── If reset_session: create fresh SessionManager
    ├── Else: reuse existing session
    ├── ConversationRunner.run(prompt from schedule)
    ├── Update run count, next_run_at
    ├── Persist via TelegramStateStore
    └── Set schedule_run_active = False
    ↓
Response delivered to topic via transport queue
```

### 6.4 CLI Message → Agent Response

```
User types at CLI REPL
    ↓
InputChannel.read_input() (PromptToolkitChannel or SimpleChannel)
    ↓
parse_slash_command(): /stop → interrupt, /quit → exit, else message
    ↓
stream_with_input(): concurrent SSE consumption + input handling
    ├── POST /chat/stream → daemon
    │   ├── ConversationRunner.run() (same as Telegram flow above)
    │   └── Yields SSE events
    └── Input handler: /stop → POST /chat/interrupt, text → POST /chat/enqueue
    ↓
SSE events rendered:
    ├── TextEvent → print to terminal
    ├── StatusEvent → render dimmed status line
    └── DoneEvent → end stream
```

### 6.5 Module Dependency Graph

```
                        config.py (leaf)
                           │
              ┌────────────┼────────────┐
              │            │            │
          prompt.py    queueing.py   events.py
                           │            │
                      ┌────┴────┐       │
                      │         │       │
                  hooks.py  commands.py │
                      │                 │
                  ┌───┴───┐            │
             _sdk_patch.py │            │
                  │    tools.py ←──────┘
                  │        │
              session.py   ├── context_jsonl.py (leaf)
                  │        ├── context_probe.py (leaf)
                  │        └── context_stats.py
              runner.py
                  │
        ┌────────┴────────┐
        │                 │
    daemon.py        telegram.py
        │                 │
    cli.py           ┌────┼────┐
                     │    │    │
              telegram_  telegram_  telegram_
              format.py  ingest.py  state_store.py
                                    │
                              telegram_main.py

              fork.py ── metrics.py (leaf)
              jsonl_fork.py ── context_jsonl.py
```

---

## 7. Scripts

### 7.1 `scripts/clone_vault.sh`
- Creates `fixture_vault/` by copying the real vault. Skips if already exists.

### 7.2 `scripts/refresh_fixture_vault.sh`
- Refreshes `fixture_vault/` from source vault. Records metadata (timestamp, source git commit) in `fixture_vault.refresh.meta`.

### 7.3 `scripts/setup_fixture_vault.sh`
- [Not read in detail] Initial fixture vault setup.

---

## 8. Testing Infrastructure

### 8.1 Test Layers
- **Unit tests** (`tests/test_*.py`) — mocked logic, fast
- **Integration tests** (`tests/test_integration_live.py`) — real HTTP + SDK
- **Evals** (`tests/evals/`) — real runtime adapters + vault + judge

### 8.2 Eval Architecture
- **Scenarios:** Markdown files in `tests/evals/scenarios/`
- **Lanes:** `deterministic` (assertions) and `judge` (LLM evaluation)
- **Profiles:** `smoke`, `feature`, `full`
- **Platforms:** CLI (pexpect, opt-in) and Telegram (Telethon userbot, default)
- **Judge:** `ClaudeSDKClient` evaluating transcripts with CRITERIA CHECK, INTENT CHECK, NOTES
- **Guardian:** `.claude/agents/eval-guardian.md` — adversarial QA auditor with veto power

### 8.3 Test Markers
- `eval` — eval scenarios
- `integration` — live HTTP + SDK
- `telegram` — Telegram-specific evals
- `telegram_smoke` — dense live Telegram smoke scenarios
- `telegram_soak` — long-running Telegram soak scenarios

---

## 9. Architecture Decisions (Key ADRs)

| ID | Decision | Impact |
|----|----------|--------|
| D001 | Claude Agent SDK over raw Anthropic SDK | Core architecture choice |
| D002 | Knowledge in vault, runtime in separate repo | Two-repo split |
| D004 | Direct filesystem + Obsidian CLI for templates | File operations |
| D018 | Fork as core primitive (not native subagents) | Agent orchestration |
| D019 | Skill injection via fork classification | Replaced by SDK native `setting_sources` |
| D022 | No compaction — flush memories, restart fresh | Memory management |
| D026 | Hook pipeline as extensible middleware | Hook architecture |
| D027 | Message queuing via HookState + additionalContext | Queue injection |
| D031 | LLM-as-judge + pexpect for E2E | Testing strategy |
| D032 | Telegram per-turn chronological messaging | Telegram UX |

---

## 10. Identified Uncertainties

1. **`prompt.py` usage** — [UNCERTAIN] Whether `build_system_prompt()` is called at runtime or is legacy. `SessionManager` uses `setting_sources=["project"]` instead.

2. **`fork.py` / `ForkRunner` usage** — [UNCERTAIN] Whether `ForkRunner` is actively used. `extract_memory()` references old vault paths (`.claude/memory/`, `.claude/topics/`). May be superseded by JSONL fork + AgentTask.

3. **`on_stop()` / `on_pre_compact()` in hooks.py** — [UNCERTAIN] These standalone functions reference `ForkRunner` but are NOT wired into `create_hook_matchers()`. May be legacy from before the pipeline architecture.

4. **`_sdk_patch.py` setattr behavior** — [UNCERTAIN] Whether `setattr` actually works if SDK message classes use `__slots__`. It works in practice, so likely they don't use `__slots__`.

5. **`IMMUTABLE_PATTERNS` mismatch** — Config has `["Misc/Meeting Notes"]` but the vault CLAUDE.md references `Sources/`. The pattern may be outdated after vault restructuring.

6. **`run_code` MCP tool** — Referenced in `docs/specs/agent-self-awareness.md` but NOT present in `tools.py`. [UNCERTAIN] Whether it was implemented and removed, or never implemented.

7. **`self_fork` MCP tool** — Referenced in `CLAUDE.md` and design docs but NOT present in current `tools.py`. Superseded by `AgentTask`/`ForkTask`.

---

*Generated by automated audit agent on 2026-03-11.*
