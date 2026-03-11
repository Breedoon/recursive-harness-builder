# Telegram ForkTask Phase 2 Scope

**Status**: Proposed
**Date**: 2026-03-03

## Goal

Add an agent MCP tool, `ForkTask`, that:

- forks the caller's current session head
- creates a new Telegram topic for that fork
- runs the child in that topic
- reports completion back to the parent topic

This replaces the current `self_fork` model for Telegram orchestration and is shaped
to converge toward native Claude task-tool semantics, while staying fork-only for now.

## Scope

This phase includes:

- `ForkTask` as the only agent self-delegation MCP tool
- always-background execution
- always fork-from-current-head semantics
- always create-a-new-topic semantics
- child completion routed back to the parent topic
- visible child input and output in the child topic
- user interaction in the child topic before, during, and after child execution

This phase does not include:

- fresh-session non-fork subagents
- parent-to-child follow-up messaging API
- resume/reopen by `task_id`
- persistence across daemon restarts
- recursion depth beyond 1
- multi-group overflow
- pinned messages or tree UI

## North Star

The target mental model is the native task tool:

- parent launches a child asynchronously
- parent and child have separate context
- child result is collected later
- the parent consumes the result as task output, not as a user-facing service dump

For now, we implement that model over Telegram topics and JSONL-copy forking.

## Public MCP API

Tool name: `ForkTask`

Parameters:

- `prompt: string`
- `description?: string`
- `timeout_ms?: integer`

Rules:

- `prompt` is the full child task prompt
- `description` is the short user-facing label
- `description` is used for topic naming and service-message summaries
- no `title` field in this phase
- no `max_turns`
- no `fork_from_message_uuid` yet
- no `continue_task_id` yet

Return payload:

- `status: "launched"`
- `task_id: string`
- `topic_name: string`
- `topic_message_link?: string`

## Behavior

`ForkTask` is always background in this phase.

When called:

1. Resolve the caller's current Telegram route.
2. Resolve the current head UUID for that route.
3. Fork JSONL from that head into a new child session.
4. Create a new Telegram topic.
5. Post an initial child service message.
6. Post a parent launch service message linking to the child topic.
7. Start the child run in the new topic using `prompt` as the first real user turn.

When the child completes:

1. Post a child terminal service message.
2. Queue a callback payload to the parent topic.
3. Deliver that callback to the current head of the parent topic.

The parent callback target is the current head of the original parent topic at the
time of completion, not the exact branch state from launch time.

## Child Topic UX

The child topic must make both the input and the output visible.

Initial child service message should contain:

- "fork task launched by agent"
- parent link
- source link if available
- `task_id`
- `description`
- full `prompt`

After that, the child run should also receive the same `prompt` as a real first user
turn so the transcript is structurally correct.

Child assistant output appears as normal topic messages.

Terminal child service message should contain:

- final status: completed, timed out, interrupted, deleted, or failed
- note that result was returned to parent when applicable
- backlink to the exact parent callback message if delivery succeeded

Child topics remain open after completion.

Child topics created by the agent should not ping the human on completion.

## Parent Topic UX

Parent launch behavior can stay close to current `/fork` behavior:

- a service message is acceptable
- the message should link to the child topic's initial service message
- it should include the short `description`

Parent completion behavior is different:

- do not dump the child answer as a visible Telegram service message by default
- instead queue a callback payload for the parent agent to consume

The callback payload should include:

- `task_id`
- terminal status
- child topic link
- child session or transcript path if available
- raw child final output text

No extra synthesized summary is required in this phase.

## Internal Model

Add an internal `ForkTaskRecord` keyed by `task_id`.

Minimum fields:

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

This can remain in-memory for this phase.

## Architecture

Implementation should:

- replace `self_fork` with `ForkTask`
- extract reusable topic-launch logic from `/fork`
- keep JSONL-copy forking as the child-session primitive
- reuse the existing per-route Telegram session state model

The major seam is:

- slash-command `/fork` remains user-initiated topic creation
- `ForkTask` becomes the agent-initiated path that uses the same transport primitive

## Timeout

`timeout_ms` is in scope if it can be implemented locally without major refactoring.

Preferred behavior:

- timer starts when child execution begins
- on timeout, child is interrupted
- child gets terminal status `timed_out`
- parent receives timed-out callback

If timeout handling requires a deeper runtime change, this phase may keep the field in
the API but back it with an internal default and defer strict enforcement.

## Done Notifications

Preferred MVP behavior:

- parent topic should not emit its normal final done/context notification while it has
  active `ForkTask`s launched from that user-initiated engagement
- once all child tasks have settled and callbacks are delivered or dropped, normal
  idle/done behavior can resume

Fallback MVP if this becomes risky:

- keep existing done/context behavior unchanged
- allow the parent completion marker to be temporarily redundant relative to later
  child completion

Reliability of the existing done pipeline is more important than perfect non-redundancy.

## Naming

Child topic naming should use the current visible parent topic name, not stale cached
names from earlier topic states.

Rules:

- if the user renamed a topic, future child names must derive from the new name
- hidden old names should not affect visible numbering
- `description` should drive the suffix when present
- otherwise use a simple fallback fork index

Example:

- parent topic: `Database Audit`
- child description: `Indexes`
- child topic: `Database Audit - Indexes`

## User Interaction Semantics

User messages in the child topic are allowed:

- while the child is running
- after the child has completed
- before the parent has consumed the callback

The system should not add special restrictions here.

The callback reflects the child state at the terminal event that produced it.
Later user interaction does not retroactively rewrite an already delivered callback.

## Cancellation and Deletion

Child-topic lifecycle events should produce parent-facing terminal status when possible:

- `/stop` in child topic -> `interrupted`
- `/clear` in child topic -> `cleared` or `interrupted`
- external child-topic deletion -> `deleted`

If the parent topic is deleted:

- child continues running
- callback is dropped
- do not redirect callback elsewhere

Do not aggressively clean up failed child topics. Visible artifacts are acceptable.

## Testing Strategy

Primary truth source:

- Telegram in / Telegram out

Secondary confirmation:

- JSONL inspection for fork boundaries, session separation, and callback effects

Key live scenarios:

- parent launches child and child gets distinct session
- child sees pre-fork context and not post-fork parent history
- child input is visible in child topic
- child output is visible in child topic
- parent receives callback only after child completion
- parent callback lands in the same parent topic even if that topic's head changed
- child topic never pings the human on completion
- user sends secret to child and later parent can report it only if child saw it
- two children run concurrently without cross-talk
- parent inline-forks while child runs and callback still lands on current parent head
- renamed topics produce correct future child names
- child interruption, clear, and deletion all produce deterministic parent-observable outcomes

Useful JSONL assertions:

- child JSONL starts from the correct fork UUID
- parent JSONL does not show child-only file reads unless callback delivered them
- parent and child session IDs remain distinct
- child transcript includes the real first user task turn

## Future Compatibility

This phase should leave room for a later general task launcher that can do either:

- fork existing session into new topic
- start fresh session in new topic

That future tool may be renamed to `Task`, but this phase keeps the agent-facing name
`ForkTask` to avoid claiming unsupported fresh-subagent semantics too early.
