# Codex SDK Exploration: Session Structure, Caching, and Forking

Date: 2026-02-26
Spikes directory: `obs/spikes/sdk-comparison/codex/`

---

## Table of Contents

- [Session JSONL Structure](#session-jsonl-structure)
- [Entry Types](#entry-types)
- [Cache Behavior](#cache-behavior)
- [Forking Mechanics](#forking-mechanics)
- [Compaction](#compaction)
- [Comparison with Claude Code](#comparison-with-claude-code)
- [Spike Index](#spike-index)

---

## Session JSONL Structure

Session files live at `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-timestamp>-<session-uuid>.jsonl`.
Each line is an independent JSON object. The file is append-only during normal operation.
Resume appends to the **same file** (no new file created).

### Key Structural Difference from Claude

**Codex has NO parent-chain DAG.** Entries have no UUID or parentUuid fields.
The JSONL is purely sequential — entries are ordered by timestamp.
This is the single most fundamental difference from Claude's architecture.

Claude:
```
user(uuid=A, parent=null) → assistant(uuid=B, parent=A) → user(uuid=C, parent=B)
```

Codex:
```
[0] session_meta
[1] response_item (developer/message)
[2] response_item (user/message)
...sequential, no linkage...
```

### Session ID

- Format: UUID v7 (e.g., `019c9a9a-100c-78a2-bd31-597acc197708`)
- Stored in `session_meta.payload.id`
- Also embedded in the filename: `rollout-<timestamp>-<uuid>.jsonl`
- In the SDK: accessible via `thread._id` or `thread.id` (NOT via events — `thread.started` returns undefined for threadId)

### Top-Level Entry Schema

Every entry has exactly three fields:
```json
{
  "timestamp": "2026-02-26T10:38:33.123Z",
  "type": "<entry_type>",
  "payload": { ... }
}
```

---

## Entry Types

### `session_meta` (1 per file)

First entry. Contains session ID, cwd, CLI version, source (`exec`/`vscode`), model provider,
and the full base instructions (system prompt text).

```json
{
  "type": "session_meta",
  "payload": {
    "id": "019c9a9a-100c-78a2-bd31-597acc197708",
    "timestamp": "2026-02-26T10:38:33.441Z",
    "cwd": "/tmp/codex-spike",
    "originator": "codex_exec",
    "cli_version": "0.104.0",
    "source": "exec",
    "model_provider": "openai",
    "base_instructions": { "text": "You are Codex, a coding agent..." }
  }
}
```

### `response_item`

The workhorse entry. Contains actual conversation messages (user, assistant, developer, tool calls).

| Subtype (payload.type) | Role | Description |
|------------------------|------|-------------|
| `message` | `developer` | Permissions/sandbox instructions, collaboration mode |
| `message` | `user` | User messages, environment context, AGENTS.md instructions |
| `message` | `assistant` | Agent's text responses (has `phase` field) |
| `reasoning` | (none) | Model reasoning summaries (may have `encrypted_content`) |
| `function_call` | (none) | Tool invocations (has `name`, `arguments`, `call_id`) |
| `function_call_output` | (none) | Tool results (has `call_id`, `output`) |
| `custom_tool_call` | (none) | MCP tool calls (has `name`, `input`, `call_id`, `status`) |
| `custom_tool_call_output` | (none) | MCP tool results |

**No UUIDs on response items.** Only `call_id` on function calls (for matching call→output).

### `event_msg`

Events and metadata. Subtypes:

| Subtype (payload.type) | Description |
|------------------------|-------------|
| `task_started` | Turn boundary marker. Has `turn_id`, `model_context_window`, `collaboration_mode_kind` |
| `task_complete` | Turn end marker. Has `turn_id`, `last_agent_message` |
| `user_message` | Duplicate of user's prompt text (for event streaming) |
| `agent_message` | Duplicate of agent's response text |
| `agent_reasoning` | Model reasoning text |
| `token_count` | **Token usage and cache stats** (see Cache section) |
| `context_compacted` | Compaction boundary marker (see Compaction section) |

### `turn_context`

Per-API-call context. Appears before each API request within a turn.
Has `turn_id`, `cwd`, `approval_policy`, `sandbox_policy`, `model`, `personality`,
`collaboration_mode`, `effort`, `summary`, `user_instructions`.

**Codex uses one `turn_id` per user turn** (not per API call). Within a single turn,
the agentic loop makes multiple API calls, each preceded by a `turn_context`.

### `compacted`

Compaction snapshot (see Compaction section).

---

## Cache Behavior

### Token Usage Reporting

Cache stats are in `event_msg` entries with `payload.type = "token_count"`.
Two usage objects per entry:

```json
{
  "type": "token_count",
  "info": {
    "total_token_usage": {
      "input_tokens": 30197,
      "cached_input_tokens": 28928,
      "output_tokens": 134,
      "reasoning_output_tokens": 10,
      "total_tokens": 30331
    },
    "last_token_usage": {
      "input_tokens": 7584,
      "cached_input_tokens": 7424,
      "output_tokens": 17,
      "reasoning_output_tokens": 10,
      "total_tokens": 7601
    },
    "model_context_window": 258400
  },
  "rate_limits": {
    "limit_id": "codex",
    "primary": { "used_percent": 4.0, "window_minutes": 300, "resets_at": 1772065428 },
    "secondary": { "used_percent": 12.0, "window_minutes": 10080, "resets_at": 1772492278 },
    "credits": { "has_credits": false, "unlimited": false, "balance": null }
  }
}
```

Key fields:
- `total_token_usage`: Cumulative across the entire session
- `last_token_usage`: For the most recent API call only
- `cached_input_tokens`: Tokens served from OpenAI's prompt cache
- `reasoning_output_tokens`: Reasoning/thinking tokens (counted separately)
- `rate_limits`: Rate limit usage percentages (primary=5hr window, secondary=weekly)

### Comparison with Claude Cache Reporting

| Aspect | Claude | Codex |
|--------|--------|-------|
| Cache read | `cache_read_input_tokens` | `cached_input_tokens` |
| Cache creation | `cache_creation_input_tokens` | Not reported |
| Uncached input | `input_tokens` (only the uncached tail) | `input_tokens` (TOTAL input, includes cached) |
| Total input formula | `cache_read + cache_creation + input` | `input_tokens` (already total); `cached = input - uncached` |
| Reasoning tokens | (included in output_tokens) | `reasoning_output_tokens` (separate) |
| Rate limits | Not reported in JSONL | Reported per `token_count` entry |
| Cumulative tracking | Not in JSONL (per-turn only) | Both `total_token_usage` and `last_token_usage` |
| Location in JSONL | `assistant` entry `message.usage` | `event_msg/token_count` entries |

**Critical difference**: Claude tells you how many tokens were NEWLY cached (`cache_creation`).
Codex only tells you how many were HIT (`cached_input_tokens`). You cannot determine from
Codex's data how many tokens were newly written to cache vs. already cached.

### SDK-Level Cache Reporting

The SDK's `TurnCompleted` event exposes:
```typescript
event.usage = {
  input_tokens: number,
  cached_input_tokens: number,
  output_tokens: number
}
```

(The `reasoning_output_tokens` from the JSONL is folded into `output_tokens` at the SDK level.)

### Cache Hit Rates (Empirically Measured)

| Scenario | Cache Rate | Notes |
|----------|-----------|-------|
| Turn 1 (new session) | 85-89% | System prompt prefix already cached from other sessions |
| Turn 2 (same session) | 93-99% | Previous turn's context cached |
| Turn 3+ (same session) | 95-99% | Increasing as more prefix is cached |
| Resume (new process, 0s delay) | 90-96% | API-level cache only (no process-bound advantage) |
| JSONL fork (0s delay) | 90-91% | Nearly identical to resume |

### Cache TTL

Per OpenAI documentation:
- **Default (`in_memory`)**: 5-10 minutes of inactivity, max 1 hour
- **Extended (`prompt_cache_retention: "24h"`)**: Up to 24 hours (GPU-local storage)
- **Minimum prompt size for caching**: 1024 tokens

**Empirical measurement (spike `09_cache_ttl_controlled.ts`):**

Built a 3-turn baseline session, then created independent JSONL-copy forks at increasing
delays. Each fork has the exact same context (31,403 input tokens), so `cached_input_tokens`
is directly comparable across measurements.

```
Delay     | Input    | Cached   | Uncached | Rate
----------|----------|----------|----------|------
0s        | 31403    | 28544    | 2859     | 90.9%
1m        | 31403    | 29440    | 1963     | 93.7%
4m        | 31403    | 28544    | 2859     | 90.9%
9m        | 31403    | 28544    | 2859     | 90.9%
15m       | 31403    | 28544    | 2859     | 90.9%
45m       | 31403    | 28544    | 2859     | 90.9%
65m       | 31403    | 28544    | 2859     | 90.9%
~8.2h     | 31403    | 28544    | 2859     | 90.9%
~33.5h    | 31403    | 28544    | 2859     | 90.9%
```

**Zero cache degradation observed at any interval, including 33.5 hours.**
The 8.2h and 33.5h measurements (spike `10_cache_after_hours.ts`) were cold forks with NO
intermediate API calls to this content — true idle gaps. The 33.5h result exceeds even
OpenAI's documented "24h extended" cache tier, suggesting that `gpt-5.1-codex` models
use effectively permanent content-hash prefix caching with no practical TTL.

Caveats:
1. **Each measurement refreshes the cache** for the next one (content-hash-based prefix caching).
   The longest gap between successive API calls was 65 min (45m→65m marks), which still showed
   no degradation. This means in practice, periodic usage (~1/hour) keeps the cache warm.
2. **The 2,859 uncached tokens are the conversation tail** that was never cached to begin with —
   the new question + end of the prefix that doesn't align to OpenAI's 128-token cache blocks.
3. **~28,544 cached tokens ≈ system prompt (~6.9K) + conversation content (~21.6K)**. The system
   prompt portion stays warm from global Codex usage. The conversation portion persists from the
   baseline session's API calls and gets refreshed by each measurement.
4. A true "cold start" test (single measurement after N hours of total silence) would be needed
   to test absolute TTL. This test measures the practical scenario: "does cache survive between
   periodic uses?"

### Cache Alignment: Resume vs Fork

Resume and JSONL-copy fork show nearly identical cache rates (~90% vs ~90%). This confirms
the cache is purely API-level (server-side prefix matching), with no process-bound KV cache
advantage like Claude sometimes shows.

---

## Forking Mechanics

### SDK API

The SDK provides only:
- `codex.startThread()` — new thread
- `codex.resumeThread(threadId)` — resume existing thread

**No `forkThread()` method exists.** No `resumeSessionAt` equivalent (no message-level forking).

### CLI Commands

| Command | Availability | Description |
|---------|-------------|-------------|
| `codex fork [SESSION_ID] [PROMPT]` | TUI only | Forks into an interactive session |
| `codex fork --last` | TUI only | Forks most recent session |
| `codex exec fork` | Does NOT exist | No non-interactive fork command |
| `codex exec resume [SESSION_ID]` | Non-interactive | Resume, not fork |

`codex fork` requires a terminal (`stdin is not a terminal` error in headless mode).

### App-Server API

The app-server exposes `thread/fork` via JSON-RPC:
```json
{
  "method": "thread/fork",
  "params": { "threadId": "thr_123" }
}
```
Returns a new thread ID. However, this requires running the app-server process.

### JSONL Copy Approach (Verified Working)

**The simplest programmatic fork is to copy the JSONL file.**

Tested and confirmed in spike `05_jsonl_fork.ts`:

1. Read the source JSONL file
2. Copy entries up to the desired fork point
3. Patch `session_meta.payload.id` with a new UUID
4. Write to a new file following the naming convention:
   `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-timestamp>-<new-uuid>.jsonl`
5. Resume from the new UUID using `codex.resumeThread(newUuid)`

**This works because:**
- The JSONL is sequential (no parent chain to maintain)
- The CLI locates sessions by searching `~/.codex/sessions/` for files containing the UUID
- No registration or metadata beyond the file existing is needed
- Cache is purely API-level, so the fork benefits from the same prefix cache

### Fork from Specific Point

Since there's no parent-chain DAG, forking from a specific point is simple:
1. Find the entry index corresponding to the desired fork point
2. Copy entries `[0..forkPoint]` to a new file
3. Resume from the new UUID

Recommended fork points:
- After `event_msg/task_complete` entries (clean turn boundaries)
- After `response_item assistant/message` entries (end of agent response)

No need for parent-chain traversal (unlike Claude).

### Implications for Mid-Conversation Forking

| Aspect | Claude | Codex |
|--------|--------|-------|
| Fork point selection | `resumeSessionAt` with message UUID (TS SDK) | Sequential index in JSONL |
| SDK support | `fork_session=True` (Python), `forkSession` (TS) | None — manual JSONL copy |
| Parent chain | Must traverse DAG | No DAG — just slice array |
| Compaction handling | Must follow chain to avoid dead entries | Must handle `compacted` entry (see below) |
| Model requirement | TS fork requires same model | No restriction (fork is just a file copy) |
| Bare fork (no message) | Not possible (always sends 1 turn) | Possible! Just copy the file. |

**Codex's biggest advantage**: You can create a fork WITHOUT sending any message.
In Claude, even a fork requires at least one API call. In Codex, you just copy the file.

---

## Compaction

### How Compaction Works

When context approaches the model's token limit, Codex compresses the conversation.
Configured via `model_auto_compact_token_limit` in `~/.codex/config.toml`.

### JSONL Structure

Compaction adds two entries:

```
[N-1] event_msg/token_count     ← last pre-compaction token stats
[N]   compacted                  ← THE COMPACTION ENTRY
[N+1] event_msg/context_compacted  ← marker
[N+2] turn_context               ← first post-compaction API call
```

### The `compacted` Entry

```json
{
  "type": "compacted",
  "payload": {
    "message": "",
    "replacement_history": [
      { "type": "message", "role": "developer", "content": [...] },
      { "type": "message", "role": "user", "content": [...] },
      ...
      { "type": "compaction", "encrypted_content": "gAAAAA..." }
    ]
  }
}
```

Key details:
- `replacement_history`: The NEW conversation context that replaces everything before
- Contains original user messages + a `compaction` entry with `encrypted_content`
- **The compaction summary is ENCRYPTED** — you cannot read it
- Pre-compaction entries remain in the file but are effectively dead

### Comparison with Claude Compaction

| Aspect | Claude | Codex |
|--------|--------|-------|
| Marker type | `system` entry with `parent=N/A` | `compacted` entry + `context_compacted` event |
| Summary format | Plaintext `user` message | Encrypted (`encrypted_content`) |
| Chain break | `parentUuid = N/A` breaks the DAG | No DAG — `replacement_history` IS the new context |
| Pre-compaction entries | Dead but remain in file | Dead but remain in file |
| Multiple compactions | Stacks (chain from latest compaction) | Stacks (each `compacted` entry replaces context) |
| Compaction trigger | Automatic (PreCompact hook) | Automatic (`model_auto_compact_token_limit`) or manual (`/compact`, `thread/compact/start`) |

### Implications for Forking Post-Compaction

For JSONL copy from a post-compaction point:
1. The `compacted` entry's `replacement_history` is the active context
2. Everything before the `compacted` entry is dead
3. A correct fork should include: `session_meta` + `compacted` entry + entries after it up to fork point
4. A naive sequential copy from entry 0 would include dead entries (same problem as Claude)

However, unlike Claude, Codex resume appears to handle this automatically — the CLI reads
the `compacted` entry and uses `replacement_history` as the context, ignoring earlier entries.
So a naive copy might still work for resume (the CLI would just read from the last `compacted`
entry), though it would result in a larger file than necessary.

---

## Additional Findings

### Rollback

The CLI supports `thread/rollback` to drop the last N turns from context.
This writes a rollback marker to the JSONL. Not exposed in the SDK.

### Thread ID Format

UUID v7 (time-ordered): `019c9a9a-100c-78a2-bd31-597acc197708`
The timestamp is encoded in the first 48 bits of the UUID.

### Model Context Window

Reported as 258,400 tokens for `gpt-5.3-codex`. Much larger than Claude's typical window.

### Rate Limits in JSONL

Codex uniquely reports rate limit usage in every `token_count` entry:
- `primary`: 5-hour rolling window (used_percent)
- `secondary`: ~7-day window (used_percent)
- `credits`: Whether the account has API credits

Claude does not expose rate limit info in its JSONL.

### Reasoning Tokens

Codex tracks `reasoning_output_tokens` separately from regular output tokens.
These are the model's chain-of-thought tokens (sometimes encrypted as `encrypted_content`
on `reasoning` items). Claude includes thinking tokens in the total output count.

### Developer Messages

Codex uses a `developer` role (not in Claude) for:
- Permission/sandbox instructions
- Collaboration mode settings
- Re-injected at each turn boundary in multi-turn sessions

---

## Comparison with Claude Code (Summary)

| Feature | Claude Code | Codex CLI |
|---------|------------|-----------|
| **Session storage** | `~/.claude/projects/<encoded-path>/<uuid>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` |
| **Entry linkage** | UUID + parentUuid DAG | None — purely sequential |
| **Branching** | In-session branching (shared parentUuid) | Not supported in single file |
| **SDK fork** | `fork_session=True` (Python), `forkSession` (TS) | Not available |
| **CLI fork** | Not available (SDK only) | `codex fork` (TUI only) |
| **Fork from specific message** | `resumeSessionAt` (TS SDK) | Not available (manual JSONL copy) |
| **Bare fork (no message)** | Not possible | Possible (just copy JSONL) |
| **Cache stats** | `cache_read`, `cache_creation`, `input_tokens` | `cached_input_tokens`, `input_tokens` (total) |
| **Cache TTL** | ~1 hour (content-hash based) | Docs say 5-10 min / 1hr max, but empirically survived 33.5h+ |
| **Compaction summary** | Plaintext | Encrypted |
| **Rate limits in JSONL** | No | Yes |
| **Reasoning tokens** | In output count | Separate field |
| **System prompt size** | ~17K-23K tokens | ~6.5K-7.5K tokens |
| **Cross-SDK interop** | Python ↔ TS (same JSONL format) | N/A (single SDK) |
| **Model restriction on fork** | TS fork requires same model | No restriction (JSONL copy) |

---

## Spike Index

| File | What it tests |
|------|--------------|
| `01_basic_thread.ts` | Basic thread creation, event streaming, JSONL file location |
| `02_thread_id_and_resume.ts` | Thread ID capture, resume from SDK, context preservation |
| `03_cache_and_jsonl.ts` | Multi-turn cache progression, JSONL structure analysis |
| `04_resume_from_new_process.ts` | Cross-process resume, cache persistence, JSONL append behavior |
| `05_jsonl_fork.ts` | **JSONL copy fork**: copy + patch UUID + resume from fork |
| `06_cli_fork_and_sdk_fork.ts` | SDK API inspection, CLI fork command availability |
| `07_cache_ttl_test.ts` | Cache TTL comparison: resume vs fork, immediate cache rates |
| `08_cache_ttl_decay.ts` | Sequential resume at increasing delays (uncontrolled — each resume grows context) |
| `09_cache_ttl_controlled.ts` | **Controlled TTL decay**: independent forks at 0s–65m delays, identical context each time |
| `10_cache_after_hours.ts` | Single fork after ~8.2h idle — cache still at full rate |

All spikes run with: `bun run <name>.ts` from the codex exploration directory.
