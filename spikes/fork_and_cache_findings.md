# Cross-SDK Fork & Cache Spike Findings

Date: 2026-02-24
Spikes directory: `obs/spikes/`

## Table of Contents

- [Session JSONL Structure](#session-jsonl-structure)
- [Forking Mechanics](#forking-mechanics)
- [Cross-SDK Interop (Python ↔ TypeScript)](#cross-sdk-interop-python--typescript)
- [Cache Behavior](#cache-behavior)
- [Compaction](#compaction)
- [Bare Fork Attempts (No Message)](#bare-fork-attempts-no-message)
- [JSONL Copy Approach](#jsonl-copy-approach)
- [Recommended Approach: Fork + Wait + Truncate](#recommended-approach-fork--wait--truncate)
- [Spike Index](#spike-index)

---

## Session JSONL Structure

Session files live at `~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl`.
Each line is an independent JSON object. The file is append-only during normal operation.

### Entry Types

| Type | Has UUID | In parent chain | Purpose |
|------|----------|----------------|---------|
| `user` | Yes | Yes | Real user messages AND tool_result blocks |
| `assistant` | Yes | Yes | Text, thinking, tool_use blocks (each is a separate entry) |
| `system` | Yes | Yes | Turn boundaries, compaction markers |
| `progress` | Yes | Yes | Progress indicators (appear between tool calls) |
| `queue-operation` | No | No | Message queue metadata |
| `file-history-snapshot` | No | No | File state snapshots |
| `summary` | Sometimes | No | Compaction-related |

### Parent Chain (DAG)

Every entry with a UUID has a `parentUuid` field forming a directed acyclic graph.
In a linear conversation, the chain is sequential:

```
user(msg1) → assistant(thinking) → assistant(text) → assistant(tool_use)
  → user(tool_result) → assistant(text) → system(turn_end)
  → user(msg2) → assistant(text) → ...
```

Key details:
- Multiple `assistant` entries per turn (thinking, text, tool_use are **separate** entries)
- `tool_result` blocks are stored as `type=user` (not assistant)
- `progress` entries appear in the chain between tool calls
- `system` entries mark turn boundaries
- `queue-operation` and `file-history-snapshot` have no UUID and are NOT in the chain

### Cache Stats

Cache statistics are stored in `message.usage` on `assistant` entries only:

```json
{
  "type": "assistant",
  "uuid": "...",
  "message": {
    "usage": {
      "cache_read_input_tokens": 22754,
      "cache_creation_input_tokens": 1395,
      "input_tokens": 3,
      "output_tokens": 842
    },
    "content": [{"type": "text", "text": "..."}]
  }
}
```

- `cache_read_input_tokens`: tokens read from API prefix cache (the reused portion)
- `cache_creation_input_tokens`: tokens newly cached in this request
- `input_tokens`: tokens not cached at all (the tail)
- Total input = cache_read + cache_creation + input_tokens
- Cache is only recorded on assistant entries (user/system entries have no usage field)

### Other Entry Fields

Every chained entry also has:
- `sessionId`: the session UUID (matches the filename)
- `timestamp`: ISO 8601
- `cwd`: working directory
- `version`: CLI version
- `gitBranch`: current branch
- `slug`: human-readable session name
- `permissionMode`: on user entries
- `requestId`: on assistant entries (Anthropic API request ID)

---

## Forking Mechanics

### Python SDK Fork (`fork_session=True`)

```python
result = await query(
    prompt="...",
    options=ClaudeAgentOptions(
        resume=session_id,
        fork_session=True,
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    ),
)
```

What happens under the hood:
1. SDK reads the parent session's JSONL
2. Creates a **new JSONL file** with a new session UUID
3. Copies all message entries from the parent (same UUIDs, same content, same baked-in cache stats)
4. **Strips `queue-operation` entries** between turns (cleaner structure)
5. Appends one `queue-operation` at the start
6. Sends the prompt and appends new user + assistant entries

**Python can only fork from the END of a session.** There is no `resume_session_at` parameter in the Python SDK (as of v0.1.35).

### TypeScript SDK Fork (`resumeSessionAt`)

```typescript
const conversation = query({
    prompt: "...",
    options: {
        resume: sessionId,
        resumeSessionAt: messageUuid,  // Fork from a specific message
        forkSession: true,
        model: "claude-haiku-4-5-20251001",
        permissionMode: "bypassPermissions",
        cwd: "/path/to/project",       // REQUIRED
        maxTurns: 1,
    },
});
```

TypeScript can fork from **any message UUID** in the conversation, not just the end.
The forked JSONL contains entries up to (and including) the specified message, plus the new turn.

**Critical: `cwd` must match the project directory that owns the session JSONL.**

### Fork JSONL Structure (compared to original)

Given an original 5-turn session:

```
Original (15 entries):
[0]  queue-operation              ← metadata
[1]  user (turn 1)
[2]  assistant (turn 1)
[3]  queue-operation              ← between turns
[4]  user (turn 2)
[5]  assistant (turn 2)
[6]  queue-operation
[7]  user (turn 3)
[8]  assistant (turn 3)
[9]  queue-operation
[10] user (turn 4)
[11] assistant (turn 4)
[12] queue-operation
[13] user (turn 5)
[14] assistant (turn 5)

Python fork from end (13 entries):
[0]  queue-operation              ← only one at the start
[1]  user (turn 1)                ← same UUID as original
[2]  assistant (turn 1)           ← same UUID, same cache stats
[3]  user (turn 2)                ← inter-turn queue-ops stripped
[4]  assistant (turn 2)
[5]  user (turn 3)
[6]  assistant (turn 3)
[7]  user (turn 4)
[8]  assistant (turn 4)
[9]  user (turn 5)
[10] assistant (turn 5)
[11] user (NEW — fork prompt)     ← new UUID
[12] assistant (NEW — response)   ← new UUID

TS fork from turn 3 (10 entries):
[0]  queue-operation
[1]  queue-operation              ← two queue-ops at the start
[2]  user (turn 1)                ← same UUID as original
[3]  assistant (turn 1)
[4]  user (turn 2)
[5]  assistant (turn 2)
[6]  user (turn 3)
[7]  assistant (turn 3)           ← fork point
[8]  user (NEW — fork prompt)     ← new UUID
[9]  assistant (NEW — response)   ← new UUID (sometimes 2 assistant entries)
```

Key differences:
- **Message UUIDs for inherited entries are identical** across original and all forks
- **Python fork**: strips inter-turn `queue-operation` entries, adds 1 at start
- **TS fork**: keeps 2 `queue-operation` entries at start, no inter-turn ones
- **Both add 2+ new entries**: user (prompt) + assistant (response) — unavoidable

### What Makes a Fork a Fork

A fork is nothing more than:
1. A new JSONL file with a new UUID as filename
2. Conversation entries copied from the parent up to the fork point (same UUIDs)
3. New entries appended after the fork point

There is no special metadata, no "forked_from" field, no registration beyond the file existing.
The session UUID IS the filename (minus `.jsonl`).

---

## Cross-SDK Interop (Python ↔ TypeScript)

### Verified Working (spike: `roundtrip_fork_cache.py`)

| Flow | Works? | Notes |
|------|--------|-------|
| Python session → TS fork from message → TS responds | Yes | TS reads Python's JSONL, creates fork |
| Python session → TS fork → Python resumes TS fork | Yes | Same JSONL format, Python reads it fine |
| Python session → TS fork → Python forks the TS fork | Yes | Double fork works |
| Raw JSONL copy with made-up UUID → Python resumes | Yes | No SDK fork needed at all |

### Critical Requirements

1. **Strip `CLAUDECODE` env var** when spawning TS from inside a Claude Code process:
   ```python
   env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
   ```
   Without this, the nested `claude` CLI refuses to start.

2. **Set `cwd`** on the TS SDK options to match the project directory owning the session.

3. **Both SDKs use the same JSONL format** — full interop confirmed.

---

## Cache Behavior

### API Prefix Cache Basics

The Anthropic API maintains a server-side prefix cache (~1 hour TTL, content-hash based).
Each API request has a token prefix (system prompt + conversation history + new message).
The API caches this prefix and reuses it for subsequent requests with the same prefix.

- `cache_read`: tokens at the start of the prefix that matched a previously cached prefix
- `cache_creation`: tokens after the cache_read portion that are being newly cached
- `input_tokens`: uncached tail tokens

### System Prompt Overhead

The system prompt (including SDK-injected instructions, tool definitions, permissions) is
approximately **17,000-23,000 tokens**. This is the baseline that always appears in cache stats.
The first turn of any session shows high `cache_read` (~22K) because the system prompt
prefix is shared across sessions.

### `maxTurns` Affects the System Prompt

**Finding**: The SDK embeds `maxTurns` into the system prompt. Different `maxTurns` values
produce different system prompt prefixes, which breaks API cache alignment.

Evidence from spike `fork_only_handoff.py`:
- Original session used `max_turns=1` → all turns had consistent cache behavior
- TS fork with `maxTurns=0` → cache_read was **6,300 tokens lower** than forks with `maxTurns=1`
- TS forks with `maxTurns=1` → cache_read matched the original session

The ~6,300 token gap corresponds to the portion of the system prompt AFTER the `maxTurns`
injection point. Everything before it hits cache; everything after is re-created.

**Implication**: When forking, match the `maxTurns` of the original session to preserve cache.
Or if the dummy turn will be deleted anyway, the mismatch doesn't matter.

### Fork Cache Rates (empirically measured)

| Scenario | First-turn cache_read | Notes |
|----------|----------------------|-------|
| Resume (same session) | 93-100% | Best case — in-process KV may help |
| Fork from end (Python) | 89-95% | Prefix cache from parent session |
| Fork from message (TS) | 64-95% | First turn varies; 99%+ after |
| Fork later (after more turns cached) | 95-99% | Benefits from parent's later turns caching the prefix |

### Why Forks Run Later Get Higher cache_read

**This was initially confusing but has a clear explanation.**

When the original session runs turn N, it caches the prefix through turn N's user message.
Turn N's assistant response is the OUTPUT — it's NOT part of the cached prefix yet.
Turn N+1 sends a prefix that includes turn N's response, and cache_creates those tokens.

When a fork runs AFTER turn N+1 has completed:
- Turn N+1 already cached the prefix INCLUDING turn N's response
- The fork's prefix matches this cached prefix → higher cache_read

Example from `python_mid_fork.py`:
```
Original turn 4:     cache_read=24,035  cache_create=1,620  (turn 3 response not yet cached)
Fork after turn 5:   cache_read=25,660  cache_create=0      (turn 3 response was cached by turn 4)
                     24,035 + 1,620 ≈ 25,660  ← the math checks out
```

**This is expected, not a bug.** In production, if the fork runs immediately after the fork
point (before later turns cache the prefix), the numbers would match.

---

## Compaction

### What Compaction Is

When a conversation approaches the context limit, the SDK compresses the conversation
history into a summary. This happens automatically (via the PreCompact hook).

### How Compaction Appears in JSONL

**Compaction stays in the SAME file with the SAME session UUID.**

Structure at the compaction boundary:

```
[391] user      uuid=3091b1bb  parent=f842c8a9  cr=-       ← last pre-compaction msg
[392] assistant uuid=eebd2283  parent=3091b1bb  cr=166357  ← last pre-compaction response
[393] system    uuid=27798027  parent=N/A        cr=-       ← CHAIN BREAK (parent=N/A)
[394] user      uuid=e1abc5a8  parent=27798027   cr=-       ← COMPACTION SUMMARY
[395] progress  uuid=331e56c7  parent=e1abc5a8
[396] assistant uuid=53f8e966  parent=331e56c7   cr=26815   ← first post-compaction response
```

Key observations:
1. **`parent=N/A` on the system entry breaks the parent chain.** Pre-compaction messages are
   orphaned — the parent chain from post-compaction entries traces back to line 393 and stops.
2. **The summary is a `user` type message** (line 394) containing a condensed version of the
   entire pre-compaction conversation. It starts with:
   `"This session is being continued from a previous conversation that ran out of context."`
3. **Cache resets dramatically** — from 166K before to 26K after. The compressed summary is
   much smaller than the full conversation.
4. **Pre-compaction entries remain in the file** but are effectively dead.

### Implications for Forking

- **Forking from BEFORE compaction**: The pre-compaction entries are the real conversation.
  Sequential copy or parent-chain traversal both work (the chain links back normally).
- **Forking from AFTER compaction**: Must follow the parent chain backward.
  It will traverse through the summary (line 394) → system chain-break (line 393, parent=N/A)
  → stop. This correctly gives you only the compacted summary + post-compaction messages.
  A naive sequential copy would incorrectly include dead pre-compaction entries.

---

## Bare Fork Attempts (No Message)

### The Question

Can TS (or Python) create a fork session without sending any message — just a session ID
that can be immediately resumed from Python?

### Answer: No

Tested three approaches (spike: `fork_only_handoff.py`):

| Mode | What it does | Result |
|------|-------------|--------|
| `maxTurns: 0` | TS SDK with zero allowed turns | **Ignored** — SDK sent a full turn anyway. Events: system, assistant, assistant, result. Response: 842 chars. |
| `abort` | TS SDK with maxTurns=1, break out of iterator after first event | Node process exited, but **CLI subprocess continued** running. JSONL has the completed turn. |
| `control` | Normal maxTurns=1 (baseline) | Works as expected — full turn completed. |

**All three modes resulted in user + assistant entries appended to the fork JSONL.**

The TS worker (spike: `ts_fork_only_worker.mjs`) detected new session files on disk even in
abort mode, confirming the CLI writes the JSONL regardless of whether the Node process
consumes the output.

### Why `maxTurns: 0` Doesn't Work

The SDK appears to treat `maxTurns: 0` as "no limit" or ignores it. The `query()` function
requires a prompt and always sends at least one API request. There is no SDK-level mechanism
to "create a fork without sending a message."

### Why Abort Doesn't Work

The `query()` function spawns a `claude` CLI subprocess. Breaking out of the async iterator
in Node.js does not kill the subprocess. The CLI continues processing, completes the turn,
and writes the result to the JSONL. The Node process exiting is just the consumer
disconnecting — the producer (CLI) doesn't notice or care.

---

## JSONL Copy Approach

### Raw Copy Works

Tested in spike `python_mid_fork.py`:
1. Copied original JSONL entries through turn 3 to a new file with `uuid.uuid4()` as filename
2. Resumed from Python with `resume=new_uuid`
3. **It worked.** Context preserved, code word recalled, cache efficient.

### Limitations

The raw sequential copy approach has a critical limitation with compacted conversations.
If the target message is post-compaction, a sequential copy includes dead pre-compaction
entries that should be excluded. The parent-chain traversal approach handles this correctly.

### Parent-Chain Traversal Algorithm

```
1. Parse all JSONL entries, index by UUID
2. Starting from target message UUID, follow parentUuid links backward
3. Collect entries until parentUuid is None/missing (natural start or compaction break)
4. Reverse the collected entries (they're in reverse order)
5. Optionally prepend metadata entries (queue-operation, file-history-snapshot)
6. Write to new file
```

This handles:
- Linear conversations (follows the single chain)
- Compacted conversations (stops at the chain break)
- Branched conversations (follows the correct branch to the target)

---

## Recommended Approach: Fork + Wait + Truncate

### Why This Over Raw Copy

- SDK handles session registration, metadata, and edge cases
- No race conditions (we wait for the dummy turn to finish)
- Compaction-safe (parent-chain traversal for truncation)
- Reuses the SDK-created session UUID

### The Flow

```
1. Python: query(
       prompt="Reply only: ok",
       options=ClaudeAgentOptions(
           resume=session_id,
           fork_session=True,
           max_turns=1,
       ),
   )
   → Consume async iterator fully (blocks until CLI exits)
   → Fork JSONL is stable, no more writes

2. Read fork JSONL into memory

3. Parent-chain traversal from TARGET message UUID backward:
   - Follow parentUuid links
   - Stop at None (handles compaction chain breaks)
   - Collect entries in reverse, then reverse them

4. Overwrite fork JSONL with only the traversed entries

5. Resume from Python using the fork session ID
```

### Safe Fork Points

| Message type | Block type | Safe to fork from? |
|-------------|-----------|-------------------|
| `assistant` | `text` | Yes — clean boundary, next is user |
| `user` | real user message | Yes — next is assistant response |
| `user` | `tool_result` | Probably — assistant continues tool chain |
| `assistant` | `thinking` | Risky — mid-turn |
| `assistant` | `tool_use` | Risky — expects tool_result next |
| `progress` | — | No — metadata |
| `system` | — | No — metadata |

Recommendation: Only allow forking from the last `assistant` entry (with `text` block type)
before a `user` entry. This guarantees a clean conversational boundary.

### Race Conditions: Non-Issue

The `query()` async iterator blocks until the CLI subprocess exits. Once the iterator is
exhausted, the JSONL file is stable. There are no concurrent writes during steps 2-4.

The only timing concern would be if another process (the original session) is ALSO writing
to the PARENT session's JSONL while we're forking. But since the fork creates a NEW file,
the parent's concurrent writes don't affect us.

---

## TS Fork from Various Message Types (compacted session)

### Experiment Setup

Used real compacted session `5f13b535` (1312 entries, 2 compaction boundaries at indices 393 and 1251,
original model: `claude-opus-4-6`). Tested TS `resumeSessionAt` from every entry type.

### Model Requirement

**The fork MUST use the same model as the original session.** Using Haiku on an Opus session causes
the CLI to exit with code 1, JSONL not written. This is true regardless of which message you fork from.

### Results by Position

| Location | Status | Notes |
|----------|--------|-------|
| Pre-compaction entries (0-392) | **FAIL** | CLI exits code 1, empty JSONL |
| First compaction boundary (393-394) | **FAIL** | Even system/summary entries fail |
| Between compactions (395-1250) | **FAIL** | All entries in "dead" zone fail |
| Last compaction system entry (1251) | **OK** | parent=N/A, the chain break |
| Post-last-compaction entries (1252+) | **OK** | All types work |
| Last assistant (1311) | **OK** | Works fine |

### Results by Message Type (post-last-compaction only)

| Entry type | Block type | Works? |
|-----------|-----------|--------|
| `system` | (compaction marker) | Yes |
| `user` | (compaction summary) | Yes |
| `assistant` | `text` | Yes |
| `assistant` | `thinking` | Yes |
| `assistant` | `tool_use` | Yes |
| `user` | (real message) | Yes |
| `user` | `tool_result` | Yes |
| `progress` | — | Not tested (metadata) |

### Key Finding

**TS `resumeSessionAt` only works within the active parent chain from the last compaction.**
Pre-compaction and between-compaction entries are "dead" — the CLI refuses to fork from them.
Within the active chain, ALL message types work (text, thinking, tool_use, tool_result, system, user).

---

## Python Fork with Compaction

### Experiment

Python `fork_session=True` from the end of the same 1312-entry compacted session.

### Results

```
Source: 1312 entries, 2 compaction boundaries at [393, 1251]
Fork:   51 entries, 1 compaction boundary at [1]
```

- Fork starts from the **last** compaction system entry (index 1251 in source → index 1 in fork)
- Then carries the compaction summary and all post-compaction messages
- **Pre-compaction entries (362 unique UUIDs): 0 found in fork**
- **Post-compaction entries (852 unique UUIDs): 48 found in fork**

### Key Finding

**Python fork correctly performs parent-chain traversal.** It follows the chain backward from the
latest message, stops at the compaction break (`parent=N/A`), and only copies entries in the active
chain. Dead pre-compaction entries are excluded. The resulting fork is compact (51 entries vs 1312).

---

## In-Session Branching (Multi-Head JSONL)

### What Is Branching?

Claude Code CLI supports branching within a single session file. Two messages can share the same
`parentUuid`, creating a Y-shaped DAG:

```
user(1) → assistant(1) → user(A) → assistant(A) [SUNNY]
                        ↘ user(B) → assistant(B) [RAINY]
```

### Experiment

1. Created a 1-turn trunk session
2. Forked twice from the trunk (Branch A: "SUNNY", Branch B: "RAINY")
3. Merged all entries into one JSONL file — both branch entries share the same parent UUID
4. Tested SDK resume behavior

### Results: Which Head Does the SDK Pick?

| Test | File order | Result |
|------|-----------|--------|
| A first, B last | trunk + A entries + B entries | **SDK picked B (RAINY)** |
| B first, A last | trunk + B entries + A entries | **SDK picked B (RAINY)** |
| Only trunk + A | No B at all | **SDK picked A (SUNNY)** |

### Why Branch B Always Wins

Branch B's assistant has a later **timestamp** (`21:29:53.780Z`) than Branch A's (`21:29:50.966Z`).
**The SDK picks the branch tip with the latest timestamp**, not the last entry in the file.
File order doesn't matter — the SDK traverses the DAG and selects the head with the most recent timestamp.

### TS Fork from Each Head

Both heads can be forked independently with TS `resumeSessionAt`:
- Fork from Branch A's assistant → creates JSONL with trunk + branch A + fork turn
- Fork from Branch B's assistant → creates JSONL with trunk + branch B + fork turn

### Implications

1. **In-session branching is a viable alternative to cross-file forking.** Instead of creating
   a new JSONL file, you can append branch entries to the same file with the same parent UUID.
2. **To control which branch is "active" on resume:** You need to control the timestamp. The SDK
   doesn't have a flag for "resume from this specific head" — it always picks the latest timestamp head.
3. **Isolating a branch:** Copy only the trunk + desired branch entries to a new JSONL file (raw copy).
   The SDK will then pick the only available head.
4. **Practical use:** If you want to "branch and switch," the simplest approach is raw JSONL copy
   of the desired chain, not in-session branching. In-session branching is useful for Claude Code UI
   (the user can navigate between branches), but for programmatic control, separate files are cleaner.

---

## Updated Recommendations

### For Mid-Conversation Forking (Python Only)

The "fork + wait + truncate" approach from the previous findings still works, but with new insights:

1. **Don't bother with TS fork from before compaction** — it will fail.
2. **Python fork from end + parent-chain traversal** is the most robust approach:
   - Fork from end (creates clean copy of active chain)
   - Read fork JSONL
   - Traverse parent chain from target message backward
   - Overwrite fork JSONL with traversed entries
3. **Alternative: Raw JSONL copy** also works and is simpler:
   - Read source JSONL
   - Traverse parent chain from target message backward
   - Write to new file with `uuid.uuid4()` filename
   - No SDK fork needed at all
4. **For in-session branching:** Append new entries sharing the same parent UUID. SDK will pick
   the head with the latest timestamp on resume. But for programmatic use, separate files are cleaner.

### Model Requirements

- TS fork requires the **same model** as the original session
- Python fork from end works with any model (it's just a file copy)
- Raw JSONL copy + Python resume works with any model (tested: Haiku resuming Haiku sessions)
- **Untested:** Whether raw JSONL copy from an Opus session can be resumed with Haiku. The session
  metadata in the JSONL might encode the original model. Further testing needed.

---

## Spike Index

| File | What it tests |
|------|--------------|
| `python_fork_cache.py` | Python-only fork cache baseline (3 turns + fork from end) |
| `cross_sdk_fork_cache.py` | Python session → TS message-level fork, cache comparison |
| `roundtrip_fork_cache.py` | Full round-trip: Python → TS fork → Python resume → Python double-fork |
| `cache_direction_test.py` | Minimal system prompt to isolate conversation cache behavior |
| `fork_only_handoff.py` | **Bare fork attempts**: maxTurns=0, abort, control — can TS fork without sending? |
| `python_mid_fork.py` | **Python-only mid-fork**: fork from end + truncate, raw JSONL copy |
| `ts_fork_worker.mjs` | TS fork worker (sends a message, used by cross_sdk and roundtrip spikes) |
| `ts_fork_only_worker.mjs` | TS fork-only worker (three modes: zero, abort, control) |
| `cache_utils.py` | Shared utilities: JSONL parsing, cache stat extraction, reporting |
| `compaction_fork_test.py` | **Compaction + branching**: TS/Python fork from compacted session, in-session multi-head |
| `ts_fork_message_types.mjs` | TS fork worker for testing various message types |
| `quick_ts_fork_types.py` | Quick TS fork from all message types in compacted session |

All Python spikes log to `/tmp/<spike_name>.log`.
Run with: `.venv/bin/python spikes/<name>.py`
