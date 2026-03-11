# Telegram ForkTask Implementation Plan

**Status**: Planning
**Date**: 2026-03-03
**Related spec**: [../specs/telegram-forktask-phase2-scope.md](../specs/telegram-forktask-phase2-scope.md)

## Goal

Implement `ForkTask` as an agent MCP tool that:

- forks the caller's current topic head
- creates a new Telegram topic
- runs the child in that topic
- queues completion back to the parent topic

This plan is intentionally tactical. It maps the phase spec onto the current codebase
without changing scope or claiming implementation details that have not been proven yet.

## Guiding Constraints

- Do not implement fresh-session subagents yet.
- Do not implement parent-to-child follow-up messaging yet.
- Keep recursion depth at 1.
- Prefer Telegram-in / Telegram-out as the main behavioral oracle.
- Use JSONL inspection only as confirmation for fork boundaries and session isolation.
- Keep the existing done/context pipeline reliable even if first-pass behavior is slightly redundant.

## Existing Reusable Primitives

### Telegram runtime isolation

Current route-local runtime state already exists in:

- `TelegramRoute`
- `TelegramSessionState`
- `_states_by_route`
- `_route_locks`
- `_message_map`
- `_session_heads`

These live in `src/obs_agent/telegram.py`.

### Existing topic fork primitive

Current user `/fork` already does the hard Telegram work:

- resolve source session and source UUID
- fork JSONL to a new session
- create a new forum topic
- bind child route state
- send child service message
- send parent confirmation message

This lives in `handle_fork()` in `src/obs_agent/telegram.py`.

### Existing queued callback delivery

Current route-local queues already support:

- in-turn continuations
- deferred idle delivery
- route-local isolation

Key pieces:

- `HookState.message_queue`
- `ConversationRunner` continuation loop
- Telegram background poller

This is the natural first implementation target for parent callback delivery.

## Main Code Seams

Primary files expected to change:

- `src/obs_agent/telegram.py`
- `src/obs_agent/tools.py`
- `src/obs_agent/session.py`
- `src/obs_agent/hooks.py`
- `src/obs_agent/events.py`
- `src/obs_agent/config.py`
- `tests/test_telegram.py`
- `tests/test_telegram_live_forum_topics.py`

Secondary possibility:

- a new helper module if `telegram.py` becomes too crowded, but avoid splitting too early

## Implementation Order

### Step 1: Add transport-owned task records

Add an in-memory `ForkTaskRecord` owned by the Telegram adapter.

Expected fields:

- `task_id`
- `parent_route`
- `parent_session_id_at_launch`
- `parent_source_uuid`
- `child_route`
- `child_session_id`
- `description`
- `prompt`
- `status`
- `launch_parent_message_id`
- `launch_child_message_id`
- `child_completion_message_id`
- `parent_callback_message_id`
- `timeout_ms`
- `created_at`
- `completed_at`
- `error`

Add indexes:

- `task_id -> ForkTaskRecord`
- optionally `child_route -> task_id`
- optionally `parent_route -> set[task_id]`

Why first:

- every later behavior depends on task identity
- later child reuse / continuation depends on stable task identity

### Step 2: Extract reusable child-topic launch helper

Refactor the internals of `/fork` into shared helpers.

Expected helper responsibilities:

- resolve source session and source UUID
- fork JSONL into child session
- create Telegram topic
- bind child route state
- send child launch service message
- send parent launch confirmation message

Do not over-generalize yet. A helper dedicated to "fork current topic head into a child
topic" is enough for this phase.

The slash command should become one caller of the helper. `ForkTask` should become the
second caller.

### Step 3: Add `ForkTask` MCP tool and remove `self_fork`

Replace the current `self_fork` registration with `ForkTask`.

Important design rule:

- the tool definition may live in `src/obs_agent/tools.py`
- the topic-launch implementation should remain Telegram-owned

Recommended shape:

- `create_obs_tools()` receives a launcher callback
- Telegram runtime injects that callback when constructing the session/tool environment
- if the runtime cannot provide a launcher, `ForkTask` returns a clean "not available"
  response

This avoids pushing Telegram internals into `SessionManager` or making the entire tool
layer transport-specific.

### Step 4: Launch child with real first user turn

The child topic must contain both:

- a service message showing prompt and metadata
- a real first user turn using the same prompt

Implementation shape:

- create child topic and send child service message
- use that child launch message id as the synthetic trigger message id
- call the existing `_run_and_send()` path for the child route with a synthetic
  `QueuedMessage`

Reason:

- message binding should attach the child launch message to the child's first real user
  turn UUID
- later user interaction in the child topic should behave normally

### Step 5: Queue completion back to parent route

On child terminal completion:

- send child terminal service message
- build parent callback payload
- enqueue payload into the parent route's queue

First-pass callback payload should remain text-based so it works with the existing
continuation system.

Suggested payload contents:

- task id
- terminal status
- child topic link
- child session/transcript path if available
- raw child final output text

No synthesized summary is required in this phase.

### Step 6: Handle terminal states consistently

Support explicit statuses:

- `completed`
- `timed_out`
- `interrupted`
- `cleared`
- `deleted`
- `failed`

These statuses should be reflected in:

- task record
- child terminal service message
- parent callback payload

Child-topic `/stop`, `/clear`, `/delete`, and external deletion should all route through
this same status model when possible.

### Step 7: Add timeout if implementation stays local

Attempt `timeout_ms` only if it can be handled without a deeper refactor.

Preferred shape:

- task record stores timeout
- child execution wrapper applies timeout
- timeout triggers interruption path
- terminal status becomes `timed_out`

Fallback:

- keep the API field
- use internal default only
- defer strict timeout enforcement

### Step 8: Optionally suppress redundant parent done/context notification

This is the highest-risk step and should be last.

Current completion summary logic is in `_run_and_send()` in `src/obs_agent/telegram.py`.

Minimal suppression strategy:

- track active child task ids per parent route
- only send the normal completion summary when:
  - route queue is idle
  - and no active child tasks remain for that route

Fallback if risky:

- keep current completion summary behavior unchanged
- accept temporary redundancy

## Naming Plan

Current topic naming is too stateful and counter-based for the desired UI behavior.

Required changes:

- derive child naming from the current visible parent topic title
- do not preserve stale invisible names
- use `description` as the main user-facing suffix when present

Expected examples:

- parent `General`, description `Index audit` -> `General - Index audit`
- renamed parent `Database Audit`, description `Backfill` -> `Database Audit - Backfill`

Tests must cover:

- renamed topic spawning a child
- renamed auto-created fork topic spawning another child
- avoiding stale numbering after rename

## Testing Plan

### Layer 1: Unit tests in `tests/test_telegram.py`

Add focused tests for:

- creating and storing `ForkTaskRecord`
- shared launch helper binding child route and session
- child launch service message and parent launch service message
- child first real user turn binding to child launch message id
- parent callback enqueueing to correct parent route
- callback deferral while parent is busy
- callback idle delivery while parent is idle
- child terminal service message containing backlink after callback delivery
- terminal status updates for stop, clear, delete, and failure
- topic naming from current visible topic title
- `ForkTask` summary rendering in `events.py`

### Layer 2: Live Telegram tests in `tests/test_telegram_live_forum_topics.py`

Add deterministic scenarios for:

- parent launches child and later reports exact child token
- child sees pre-fork context but not later parent history
- user sends secret into child and parent later reports it
- two concurrent children complete with no cross-talk
- parent inline-forks while child runs and callback still lands in parent topic
- child interrupted by user produces deterministic parent-observable result
- child externally deleted while active produces deterministic parent-observable result
- child topic visibly contains full prompt text
- child completion links back to the exact parent callback point
- renamed topic generates correct child title

### Layer 3: JSONL confirmation

Use JSONL only to confirm:

- correct fork source UUID
- separate child session id
- child transcript includes first real user turn
- parent did not itself perform child-only reads before callback delivery

## Suggested PR Sequence

### PR 1

- add task records
- extract shared child-topic launch helper
- no MCP changes yet

### PR 2

- add `ForkTask`
- switch MCP registration from `self_fork`
- launch child topic and run child prompt
- parent callback delivery
- leave done/context behavior unchanged

### PR 3

- add terminal status handling
- support stop / clear / delete semantics
- attempt timeout if local

### PR 4

- add optional parent done/context suppression if stable

## Risk Register

### Risk 1: Queue payload complexity

The current queue is mostly text-oriented. Overloading it with rich task objects too early
could destabilize existing continuation behavior.

Mitigation:

- keep callback payload text-based in v1
- keep task records transport-owned

### Risk 2: Tool layer becomes transport-specific

`ForkTask` needs Telegram-aware behavior, unlike the current `self_fork`.

Mitigation:

- inject a launcher callback from Telegram runtime
- keep tool definition separate from topic orchestration implementation

### Risk 3: Done/context summary regressions

Delaying or suppressing route completion summaries could break the current stable end-of-turn marker.

Mitigation:

- make this the final step
- keep fallback behavior unchanged if risky

### Risk 4: Child termination paths diverge

If stop / clear / delete each invent their own callback behavior, parent-side semantics will become inconsistent.

Mitigation:

- centralize terminal status handling around task records

## Decision Points Before Implementation

These are resolved enough to start coding:

- use `description`, not `title`
- include `timeout_ms` if locally feasible
- no synthesized child summary
- include callback/backlink linkage when possible
- delay parent done/context only if implementation remains local and safe

## Definition of First Successful Milestone

The first milestone is achieved when all of the following are true:

- agent can call `ForkTask`
- a new Telegram topic is created
- the full task input is visible in that topic
- the child runs in that topic as its own session
- the child result is queued back to the parent topic
- the parent can answer a deterministic question that depends on the child's result
- user interaction in the child topic does not break routing
- existing topic isolation and `/fork` behavior still pass
