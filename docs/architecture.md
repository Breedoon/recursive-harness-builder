# OBS Agent Architecture

## Overview

OBS Agent is a local Claude Agent SDK runtime backed by an Obsidian vault.
It has three main interaction surfaces:

1. CLI (`obs_agent.cli`)
2. HTTP daemon (`obs_agent.daemon`)
3. Telegram adapter (`obs_agent.telegram`)

The architecture optimizes for reliability and observability over UI polish.

## Core Runtime Components

| Component | File(s) | Responsibility |
|-----------|---------|----------------|
| Config | `src/obs_agent/config.py` | Paths, daemon settings, Telegram settings, immutable rules |
| Session manager | `src/obs_agent/session.py` | ClaudeSDK client lifecycle, reconnect/reset, session_id tracking |
| Hooks + state | `src/obs_agent/hooks.py` | Immutable guard, interrupt handling, queue injection, shared state |
| MCP tools | `src/obs_agent/tools.py` | `self_fork` foreground/background behavior |
| Runner | `src/obs_agent/runner.py` | Shared conversation orchestration, queue continuation, background fork wake-up |
| Events | `src/obs_agent/events.py` | Status event schema + tool summary formatting |
| Daemon | `src/obs_agent/daemon.py` | HTTP + SSE API over runner |
| CLI | `src/obs_agent/cli.py` | Interactive terminal client |
| Telegram | `src/obs_agent/telegram.py` | Telegram adapter with per-turn message flow + background poller |
| Telegram entrypoint | `src/obs_agent/telegram_main.py` | Loads `.env`, validates config, starts Telegram bot |

## Vault Model

- Primary vault path defaults to `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/T`
- Agent structure is `.claude/` inside the vault (skills, system docs, memory, drafts)
- Runtime code is in this repo; persistent agent memory/context is in vault markdown

## Message / Queue Flow

### Shared Queue Model

`HookState` exposes:

- `message_queue`: queued user/fork messages for continuation/background delivery
- `status_queue`: status events to surface tool/queue activity
- `interrupt_flag`: cooperative interrupt at tool boundaries
- `background_tasks`: active background fork tasks

### Runner Behavior

`ConversationRunner.run()` performs:

1. Optional pending-message injection at turn start
2. Main response streaming
3. Continuation loop for queued user messages
4. Background fork wake-up loop
5. Final queue drain for next turn

Event stream emitted to adapters:

- `TextEvent`
- `StatusEvent`
- `TurnEndEvent` (boundary per SDK assistant message)
- `DoneEvent`

## Telegram Architecture (Current)

The Telegram adapter intentionally uses a simple, robust model:

1. Fragment reassembly for Telegram auto-split user messages
2. Per-chat lock serialization (prevents in-chat out-of-order processing)
3. Per-turn flush: inline tool/status + text in chronological order
4. Final `context: used / window` summary at queue-idle completion (notification enabled)
5. Background queue poller every 3s for idle auto-delivery

Important implementation notes:

- Content chunks are sent with `disable_notification=True`
- The final completion summary is sent with `disable_notification=False`
- Single-user assumption remains in auto-delivery routing (`_last_chat_id` + `_last_bot`)

## Testing Architecture

The test stack is layered, but evals are the primary correctness gate.

- Unit: `tests/test_*.py`
- Integration: `tests/*integration*.py`
- Evals: `tests/evals/`
  - CLI scenarios
  - Telegram scenarios (`tg_*`) via Telethon + real bot process

Judge output now includes:

- `CRITERIA CHECK`
- `INTENT CHECK`
- `NOTES` (suspicious/off behavior even on pass)

## What This Architecture Prioritizes

1. Deterministic behavior over clever orchestration
2. Human-observable chronology for long-running tool/fork work
3. End-to-end evaluation over mocked confidence
4. Simpler control loops (polling + locks) over high-complexity event machinery
