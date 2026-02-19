# Telegram Message Flow Simplification

**Date:** 2026-02-19
**Status:** Approved, pending implementation

## Original User Request (verbatim)

> i want to scope out a few small to medium changes. i have them drafted below but i want you to brainstorm and interview me to understand better what i mean. they should be all related:
> - make text output of agent mix with commands in chronological order (can be same message, that's fine, now they're sent separately)
> - we might just simplify it a lot and eg we can just send a message for every turn/tool use/ anything that agent does and only send notification on the very last message (or potentially for simpler solution, make an end hook send a "(done)" message that has that notification if tracking which one is last is harder. im happy to drop notification requiremet if that also adds unnecessary complexity
> - issue: queued messages from eg background commands / subagents / forks don't get delivered until user sends a message
> - occasional out of sync in messages (user sends a message 2 - agent replies to message 1 - user sends message 3 - agent replies to message 2) - no need to fix just yet, it might inform simplification process of the telegram loop that it can get solved

Additional context discovered during brainstorming:

> 429 Too Many Requests (maybe typing... all the time?) - polling

Logs showed `sendChatAction` being called multiple times per second (10+ calls in 5 seconds) instead of the intended 4-second interval, caused by multiple concurrent `_typing_loop` instances from `concurrent_updates(True)`.

## User Intent and Vision

### What the user cares about (in their words)

**Chronological observability is the primary goal.** The user wants to see what the agent is doing as it happens, in order. Tool uses and text output should not be separated into different message types — they should flow together chronologically.

> "i want to see chronology so feels like most granular would probably handle that"

**Simplicity over sophistication.** The user consistently chose simpler approaches over architecturally "correct" ones:

> "which of these is simpler to implement without adding complexity and handling edge case scenarios"

When asked about OpenClaw's event-driven wake pattern vs simple polling:
> "if the openclaw version is indeed more complicated, i don't mind polling that much"

**High tool observability.** The user wants verbose tool summaries, not terse ones:

> "i want bash to be more verbose and not truncate long commands - i want to see them so let's do 200 for everything"
> "tool use maybe summary for basic tools like reading file but generally i want to see all tools at least as json or sth. i want a lot of observability so they can also be long like idk 100-200 characters per tool (so i see more full bash command)"

**Unreliable message editing must go.** The user observed real problems with the editable StatusMessageManager:

> "I noticed working with telegram that sometimes things would go out of sync and two messages would be getting edited simultaneously (not sure how or why) so i think it's unreliable"

**Background results must auto-deliver.** The user does not want to manually trigger delivery:

> "definitely not [notify-only], i dont want agent waiting for me to keep acting"

**Future vision: subagent/fork observability.** The user wants to eventually see what background tasks are doing, but as a separate concern from main agent output:

> "I will want to have a similar thing later on for eg seeing summaries of what subagents / forks are working on (so those are NOT chronological injected in the middle of what the main agent is doing). Maybe we could unify those two now so that later when we do implement that observability for subagents / forks / background commands."

This is deferred — the current design focuses on main agent output. The separation (main = separate messages, background = editable dashboard) is noted for future work.

### Approaches the user considered (even contradictory ones)

The user explored several directions during brainstorming:

1. **Editable message per turn** — "i don't mind having one long message per agent's work and edit that but idk how to handle edge cases like user sending something in the middle of agent working"

2. **Separate messages** — "that's why i thought separate messages"

3. **Rolling window for subagents** — "for subagents/forks/commands it would just not create new messages but keep last 4000 characters of content or sth"

4. **OpenClaw-inspired event-driven delivery** — The user asked to explore OpenClaw's patterns for background task delivery. After seeing the complexity (heartbeat wake, coalescing, priority queues), chose simple polling instead.

5. **Notification approaches** — Considered "all silent + (done)", "last message notifies", and "drop notifications entirely". Chose "(done)" sentinel as a simple addition over fully silent.

The final direction resolved these: separate messages for reliability, simple polling for background delivery, editable dashboard deferred to later.

## Design

### 1. Per-turn message sending

**Current behavior:** Runner yields `TextEvent`/`StatusEvent` per content block. Telegram accumulates ALL text into `text_parts`, sends one big message at the very end via `_send_response`. Tool status goes to a separate editable `StatusMessageManager` message.

**New behavior:** Runner adds a `TurnEndEvent` yielded after processing all blocks from one SDK `message` in `receive_response()`. Telegram bot accumulates events in a buffer. On `TurnEndEvent`: formats and sends as one Telegram message.

Message formatting per turn:
- `TextBlock` content: normal text, HTML-formatted via `md_to_telegram_html`
- `ToolUseBlock` summary: italic line like `<i>Read: Agent/context.md</i>`
- `ThinkingBlock`: italic `<i>thinking...</i>`
- All interleaved in the order they appeared in the SDK message
- Split with existing `split_message()` if >4000 chars (safety margin below Telegram's 4096 limit)
- All messages sent with `disable_notification=True`
- After `DoneEvent`: send `"(done)"` with notification enabled

### 2. Verbose tool summaries

Bump `summarize_tool_use` truncation from 80 to 200 characters for all tools. For unknown tools, show more args (5 instead of 3). The user wants to see full bash commands, full fork task descriptions, etc.

### 3. Background queue auto-delivery (polling)

A background `asyncio.Task` in `TelegramBot` that polls every 2-3 seconds:
1. Check if `hook_state.message_queue` has items
2. Check if the bot is not currently running a conversation (`_busy` flag)
3. If both: drain the queue, auto-trigger a new `ConversationRunner` turn with the queued messages as input, send results to chat

Uses `_last_chat_id` and `_bot` stored on the `TelegramBot` instance from the most recent `_process_message` call.

**SINGLE-USER ASSUMPTION:** This stores one chat_id. When adding multi-user support, this must be changed to a per-user/per-chat mapping. Document this in code comments.

### 4. Deletions

| Component | Reason |
|-----------|--------|
| `StatusMessageManager` | Replaced by inline tool summaries in per-turn messages |
| `_typing_loop` | Per-turn messages serve as activity indicators. Eliminates 429 rate limit errors from concurrent typing loops. |
| `_send_response` | Replaced by inline sending on `TurnEndEvent` |
| `_run_conversation` (current form) | Replaced by simpler loop that sends per-turn |

### 5. Notifications

All content messages: `disable_notification=True`. Final `"(done)"` message: notification enabled.

## File Changes

| File | Change |
|------|--------|
| `runner.py` | Add `TurnEndEvent` dataclass. Yield it after processing each SDK message's blocks in `receive_response()` loop. Add to `RunnerEvent` union. |
| `events.py` | Bump `summarize_tool_use` truncation from 80 to 200 for all tools. Unknown tools: show 5 args instead of 3. |
| `telegram.py` | Rewrite `_process_message` to send per-turn. Delete `StatusMessageManager`, `_typing_loop`, `_send_response`. Add `_busy` flag, `_last_chat_id`/`_bot` storage (documented single-user assumption). Add background poller task. Add "(done)" sentinel. |
| `telegram_format.py` | Update `split_message` threshold from 4096 to 4000. |

## Not In Scope (documented for later)

- **Out-of-sync message ordering:** User noted "user sends message 2 - agent replies to message 1" pattern. Caused by `concurrent_updates(True)` allowing parallel `_process_message` calls. Fix: serialize with a lock. User said "no need to fix just yet, it might inform simplification process."

- **Subagent/fork observability dashboard:** Editable message showing what background tasks are working on. The user's vision is "NOT chronological injected in the middle of what the main agent is doing" — a separate status board. Deferred.

- **Multi-user support:** Remove single-user assumption for `_last_chat_id` storage.

## Testing Plan

### Unit tests (mocked, fast)

- `TurnEndEvent` yielded at correct boundaries in runner (after each SDK message)
- `summarize_tool_use` at 200-char truncation limit
- `split_message` at 4000 threshold
- Background poller logic (triggers when queue non-empty + not busy, skips when busy)
- Per-turn message formatting (text + italic tool summaries interleaved correctly)
- No `sendChatAction` / `_typing_loop` in any code path

### Existing Telegram evals to update

- **`tg_tool_visibility`** — Remove criterion about "one status message showing tool activity, one with the actual response" (no separate status message). Replace with: tool summaries appear inline within response messages as italic text.

### Existing Telegram evals expected to pass as-is

- **`tg_queue_while_busy`** — Queue delivery still works, just output format changes
- **`tg_message_split`** — Splitting still works (4000 threshold, was 4096)

### New Telegram evals

#### `tg_chronological_output`

Basic chronology test. Send a message that triggers tool use + text output. Verify:
- Tool summaries and text appear interleaved chronologically in the messages
- "(done)" is the last message from the bot
- Messages are coherent and in order

#### `tg_background_auto_delivery`

Background fork auto-delivery test. Ask agent to fork a background task. Don't send anything else. Verify:
- Bot sends fork results automatically without user prompting
- Results appear within a reasonable window (~10s)
- "(done)" appears after the auto-delivered response

#### `tg_stress_chronology`

Stress test combining chronology, queue delivery, and the done sentinel. Steps:
1. Send a complex task (e.g., "Read my CLAUDE.md, summarize all skills, and list the directory structure of the vault")
2. After a few seconds, send "how is it going?"
3. After a few more seconds, send "ping - reply that you saw this message"

Criteria:
- All three interactions produce responses in the output
- Chronological order holds (pings are addressed after the tool work that preceded them)
- "(done)" appears at the end
- No messages are lost or swallowed
- The agent addresses the skill summary task AND acknowledges both pings

## OpenClaw Research Summary

Explored `/Users/breedoon/Documents/JetBrainsProjects/PyCharm/P/OSS-watch/openclaw` for patterns. Key findings:

- **Heartbeat wake mechanism** (`heartbeat-wake.ts`): Background tasks queue wake requests via `requestHeartbeatNow()`. Requests are coalesced by priority (RETRY < INTERVAL < DEFAULT < ACTION). When main lane is idle, coalesced batch triggers heartbeat runs.

- **System events queue** (`system-events.ts`): Session-scoped, in-memory, max 20 events. Drained on next turn and prefixed to system prompt. Similar to our `hook_state.message_queue`.

- **Block reply pipeline** (`block-reply-pipeline.ts`): Queues payloads from agent run (text, media, tool results). Supports coalescing with configurable char limit and delay. More sophisticated than what we need.

- **Decision:** The event-driven wake pattern is more infrastructure than needed for a single-user bot. Simple polling achieves the same result with less complexity. Can upgrade later if needed.
