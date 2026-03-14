# Telegram Lineage-First Multi-Level Teams

**Status**: In progress  
**Date**: 2026-03-13

## Executive Summary

This change makes multi-level teams a core platform feature by making
`lineage` the canonical agent identity and projecting Claude's flat native team
model onto the root of each lineage tree.

The platform, not Claude Code, owns the real hierarchy.

Claude still receives team awareness, but the durable source of truth is one
bootstrap XML envelope injected into the first real prompt of every new head.
That same envelope is later recoverable from the session transcript by scanning
for the latest `<obs-bootstrap>` block.

## Original Intent, Distilled

The user requirements that remain in scope after research and design review are:

1. Multi-level teams are a first-class runtime capability.
2. An agent can be both:
   - a member of a larger tree
   - the lead of its own subtree
3. `lineage` is the canonical identity term, not `path`.
4. One topic corresponds to one agent identity.
5. Inline forks do not create a new agent identity. They only change the
   current session head of the same topic/agent.
6. The agent should know it is part of a team and know its lineage.
7. The platform should re-architect as little as possible and reuse Claude SDK
   capabilities where they fit cleanly.
8. The hierarchy and identity data should be recoverable from transcript data,
   not only daemon memory.
9. `/fork` should append the supplied title to the parent topic name instead of
   replacing it.
10. `AgentTask.description` should become `alias`, with compatibility preserved
    during migration.
11. Testing must be live, overlapping, and intentionally fragile: long chains,
    concurrency, messaging, forks, schedules, restart, and edge conditions in
    the same scenario.

## Definitions

### Agent

An agent is defined by its `lineage`.

An agent lives in exactly one Telegram topic and may have multiple sessions over
time.

### Lineage

`lineage` is the stable sequence of names from the trunk agent to the current
agent.

It is the canonical identity for addressing, naming, tree membership, and
future notifications.

### Session Head

The `session head` is the current Claude session that represents the agent.

Inline reply-forking changes the session head but does not create a new
lineage.

### Trunk

The trunk is the root agent for a tree. In a forum group, this is typically the
topic where the user started the work.

### Native Team Projection

Claude's native team model is treated as a flat projection:

1. one native team per trunk tree
2. one native agent member per lineage
3. the real hierarchy stays in OBS lineage data

## Non-Goals

1. Native Claude task tool parity
2. Nested native teams
3. Multiple native inboxes per lineage
4. Replacing inline forking semantics
5. Solving every future summarization detail in this change

## Core Architectural Decision

### The platform owns the tree

OBS owns:

1. lineage construction
2. lineage persistence
3. lineage recovery
4. lineage-aware naming
5. lineage-aware messaging and future notifications

Claude native teams are used only as a helpful flat substrate.

### One canonical bootstrap envelope

There must be one canonical XML envelope and one canonical serialization
function.

The settled envelope root is:

```xml
<obs-bootstrap version="1">
  <obs-lineage>
    <obs-node name="Root Topic" />
    <obs-node name="Research Fork" />
    <obs-node name="task-researcher" />
  </obs-lineage>
  <fork_context>
    <origin>agent_task_fork</origin>
    <is_fork>true</is_fork>
    <agent_id>...</agent_id>
    <session_id>...</session_id>
    <parent_session_id>...</parent_session_id>
  </fork_context>
  <team_context>
    <root_team_key>...</root_team_key>
    <native_agent_name>...</native_agent_name>
  </team_context>
</obs-bootstrap>
```

Notes:

1. The root tag uses `obs-`, not `obs-agent`.
2. Existing `fork_context` and `team_context` concepts are retained inside the
   new envelope so current model behavior stays intuitive.
3. `agent_id` remains as the short runtime handle. It is not the canonical
   identity.
4. `lineage` remains the canonical identity.

## Why Prompt Injection Wins

The canonical bootstrap envelope is injected into the first real prompt for each
new head.

This is the durable and model-visible source of truth because:

1. normal prompt content is part of conversational history
2. Claude sees it on resume because it is ordinary transcript content
3. the JSONL can be searched for `<obs-bootstrap>`
4. we avoid a second custom transcript-only metadata channel

We explicitly do **not** make `SessionStart additionalContext` the canonical
source because its transcript representation is not stable enough to parse.

We also do **not** rely on custom appended JSONL metadata records as the main
source of model awareness.

## Session Prompt Appendix

Every session also appends one OBS runtime appendix to the base Claude Code
system prompt.

This appendix is not the canonical identity payload. The bootstrap XML remains
the canonical identity payload.

The appendix exists so every agent is reminded of the platform differences that
matter operationally:

1. native `Task` and `ForkTask` are disabled here
2. `AgentTask`, `AgentTaskOutput`, and `AgentTaskStop` are the correct task
   tools
3. teams are enabled by default for the whole lineage tree
4. `SendInboxMessage` and `ReadInbox` are the correct messaging tools
5. lineage defines the agent's location in the team tree
6. when resumed or woken, the agent should check inbox before assuming it has
   no work

This keeps the model's working assumptions aligned with OBS instead of the
native harness defaults.

## Transcript Recovery Rule

Recovery rule:

1. scan the transcript for `<obs-bootstrap>`
2. read the latest valid block
3. that block defines the current lineage identity for that session head

Parser rules:

1. the bootstrap block must be injected at the start of the first outbound
   prompt for that head
2. the parser only trusts well-formed `<obs-bootstrap version="1">...</obs-bootstrap>`
3. latest valid bootstrap wins

## Creation Flows

All head-creation flows use the same bootstrap serializer.

### 1. Trunk start

When a topic gets its first Claude session, inject a bootstrap block with:

1. lineage = `[topic title]`
2. `origin = trunk_start`
3. `is_fork = false`
4. no parent session id

Implementation note:

1. The SDK/CLI path currently does not safely support preallocating a fresh
   root session id before the first live query.
2. The first trunk bootstrap may therefore omit `<session_id>`.
3. The `session_lineage` tool should fall back to the live current session id
   when the bootstrap omits it.

### 2. AgentTask with `fork=true`

Create child topic and child session from the parent head, then inject a
bootstrap block into the child's first prompt with:

1. lineage = parent lineage + child alias
2. `origin = agent_task_fork`
3. `is_fork = true`
4. `agent_id = task_id`
5. `session_id = child session id`
6. `parent_session_id = parent session id at launch`

### 3. AgentTask with `fork=false`

Create child topic with a fresh session, then inject a bootstrap block into the
child's first prompt with:

1. lineage = parent lineage + child alias
2. `origin = agent_task_fresh`
3. `is_fork = false`
4. `agent_id = task_id`
5. `session_id = child session id`
6. `parent_session_id = parent session id at launch`

### 4. User `/fork`

Create child topic and fork the source session JSONL as today.

The child lineage node is:

1. the explicit `/fork` title when provided
2. otherwise `F<n>`

The child topic title is:

1. `"<parent topic title> - <child alias>"`

The child's first future prompt receives:

1. lineage = parent lineage + child alias
2. `origin = user_fork`
3. `is_fork = true`
4. `session_id = child session id`
5. `parent_session_id = parent session id at fork time`

### 5. Inline reply-fork in the same topic

Inline reply-fork keeps the same lineage because the same topic still
represents the same agent.

When the route switches to a new forked session head:

1. keep the same lineage
2. inject a new bootstrap block into the next prompt only
3. set `origin = inline_fork`
4. set `is_fork = true`
5. set the new `session_id`
6. set `parent_session_id` to the fork source session

This gives the model awareness that the head changed while preserving the same
agent identity.

### 6. Session reset in the same topic

If a topic session is cleared or reset, the next fresh session in that same
topic reuses the same lineage.

The next first prompt should inject:

1. the same lineage
2. `origin = session_reset`
3. `is_fork = false`

### 7. User `/clear`

`/clear` is the soft reset command.

It must:

1. preserve the current lineage
2. preserve the current root-team projection
3. preserve inbox reachability for that lineage
4. preserve schedules unless explicitly unscheduled
5. forget the conversation state by resetting the active Claude session

The next first prompt in that same topic must inject:

1. the same lineage
2. `origin = session_reset`
3. `is_fork = false`

Operationally, `/clear` means:

1. same agent identity
2. same topic
3. same team member
4. fresh memory

### 8. User `/new [emoji] [topic name]`

`/new` is the hard reset command for the current topic.

It must:

1. terminate the current lineage identity for that topic
2. remove the old lineage from live inbox routing
3. reset the topic into a fresh trunk agent of a new tree
4. optionally apply a new visible topic emoji and title

Name and emoji rules:

1. if the user provides a leading emoji token, use it when it is a valid forum
   topic icon
2. if no valid emoji is provided, pick a random valid topic emoji
3. the chosen random emoji should differ from the current topic emoji when
   possible
4. if no title is provided, generate a title from two random words
5. emoji is display-only and is not part of lineage identity

The next first prompt in that same topic must inject:

1. lineage = `[new visible topic title]`
2. `origin = new_trunk`
3. `is_fork = false`

Operationally, `/new` means:

1. same visible Telegram topic container
2. different agent identity
3. different lineage
4. different root-team projection

### 9. User `/delete`

`/delete` terminates the current topic and its lineage identity.

It must:

1. delete the Telegram topic when possible
2. remove the lineage from live inbox routing
3. make later inbox sends to that lineage explicitly undeliverable

Unlike `/new`, `/delete` does not create a replacement lineage in the same
topic.

## Naming Rules

### Public lineage name

The lineage node name is human-readable and stable.

### Topic title

Topic titles are display-oriented and may include the parent prefix:

1. agent task child topic: `"<parent> - <alias>"`
2. `/fork` child topic: `"<parent> - <title-or-Fn>"`

### Native team projection names

Native Claude team and agent names must be filesystem-safe because current inbox
compat paths use them directly.

Therefore:

1. raw human lineage names stay in bootstrap XML and user-visible topic names
2. native team/agent projection uses sanitized stable keys
3. slashes are not used in native projection names

Projection:

1. `root_team_key` is derived from the trunk lineage only
2. `native_agent_name` is derived from the full lineage

This preserves one inbox per lineage and avoids multi-inbox explosion.

## Team Model

### Root-team projection

Every subagent is a team member by default.

The flat projection is:

1. native team = root team for the trunk tree
2. native agent = the current lineage member handle

This means a deep descendant is:

1. a member of the trunk root team
2. also the logical lead of its own subtree according to lineage

Claude sees both:

1. `team_context` from the bootstrap
2. native team env variables when applicable

Implementation note:

1. The canonical bootstrap path also materializes the projected native team
   config and inbox files before the first routed query for that head.
2. This makes the flat native team substrate available to trunks and
   descendants by default, not only explicit worker launches.

## Messaging Model

### Unified tree scope

Any lineage in the same tree should be able to message any other lineage in the
same tree.

Phase 1 implementation keeps the existing inbox substrate but projects all
workers into the same root team by default.

Implications:

1. one inbox per lineage projection
2. no multi-team inbox aggregation layer required yet
3. `ReadInbox` and `SendInboxMessage` may infer the current team/agent identity
   from the current bootstrap context
4. every lineage-backed route is a valid inbox target, not only task-backed
   workers

### Dead-recipient contract

If a sender targets an agent identity that no longer exists because that topic
was deleted or replaced via `/new`, the sender must be told that the message was
not delivered.

Rules:

1. dead recipients must not silently accept inbox writes
2. `SendInboxMessage` should fail with an explicit undelivered result
3. the sender-facing error should mention that the recipient may have been
   replaced or deleted
4. the runtime must not wake any route for that undelivered send

### Replacement with the same lineage name

A parent may later spawn a replacement child with the same lineage name as a
dead child.

In that case:

1. the replacement resolves to the same native inbox projection
2. unread inbox items that existed before the death remain available
3. messages attempted while the recipient was dead remain undelivered and do not
   magically appear later
4. future sends after the replacement exists should deliver normally again

### Wake guarantee

Operational requirement:

1. an inbox message to a valid lineage target should wake that agent whenever
   the runtime is healthy
2. task-backed workers and plain lineage routes must both wake
3. if immediate notifier wake does not happen because a route is already busy,
   the unread inbox item must remain and the background poller must wake it
   later
4. the only accepted non-wake causes are serious runtime corruption such as
   state DB corruption, missing transcript, or process failure

Future-friendly rule:

1. the platform should eventually accept lineage-based recipients directly
2. the flat native projection stays an internal transport detail

## Tool Changes

### `AgentTask`

Public schema change:

1. add `alias`
2. keep `description` as a deprecated alias for compatibility
3. `alias` is the short stable child name used for lineage and topic naming

### `ForkTask`

Same compatibility treatment as `AgentTask`.

### `session_lineage`

Add a lightweight tool that returns the current resolved bootstrap metadata:

1. lineage node list
2. lineage length
3. current session id
4. current root team key
5. current native agent name
6. optional raw bootstrap XML only when explicitly requested

This is both a debugging tool and a future messaging helper.

## Accepted Edge Cases

### Concurrent inline forks in the same topic

Accepted behavior:

1. the topic is still one agent identity
2. the active head can switch as messages arrive
3. two concurrent inline-fork sessions can stomp on each other
4. messaging or inbox delivery may land on whichever head is current

This is misuse, but it is accepted and should be tested.

### Name collisions

Two siblings can resolve to the same alias.

Accepted for now:

1. lineage remains the logical identity
2. native projection keys add a stable hash suffix
3. janitor or future repair tooling can surface collisions

### Topic rename after creation

Accepted:

1. current topic title may drift
2. lineage name is the name captured at creation time
3. later children may observe the newer visible topic title for display, while
   the lineage remains stable

## Live Test Strategy

The live tests for this feature must be intentionally overlapping and fragile.

The goal is not one test per function. The goal is to prove the system still
works when many features interact at once.

### Required scenario dimensions

Every major scenario should combine several of:

1. deep lineage
2. mixed `fork=true` and `fork=false`
3. `/fork` plus inline reply-fork in the same tree
4. team messaging in multiple directions
5. schedules and background callbacks
6. daemon restart and recovery
7. concurrent launches
8. topic rename
9. stop or interruption
10. delayed child completion

### Live smoke matrix

#### Scenario A: Deep tree, mixed creation, cross-level messaging

1. create trunk topic
2. create level-1 child with `AgentTask fork=true`
3. from child create level-2 child with `AgentTask fork=false`
4. from level-2 create level-3 child with `AgentTask fork=true`
5. issue `/fork` from level-1 in parallel
6. have trunk, level-3, and `/fork` child all exchange inbox messages
7. assert every receiver wakes and processes the message
8. assert root team projection is identical across all members
9. assert lineage differs correctly across all topics

#### Scenario B: Deep lineage plus inline fork race

1. create a 5+ deep tree
2. in one mid-level topic, send two reply-forks from different historical
   points nearly simultaneously
3. while the head is switching, send inbox messages from ancestors and
   descendants into that same lineage
4. assert the route remains one logical lineage
5. assert the latest bootstrap in the active transcript keeps the same lineage
6. accept head switching races, but verify no crash and no cross-topic bleed

#### Scenario C: Messaging stress fan-out and fan-in

1. trunk launches 6-10 children
2. each child launches one grandchild
3. children send messages upward to trunk
4. trunk sends messages sideways to cousins
5. grandchildren send messages to ancestors and cousins
6. overlap this with at least one long-running child task and one resumed task
7. verify wake behavior, delivery, and no inbox partitioning by subtree

#### Scenario D: Schedules plus hierarchy plus restart

1. create a tree with at least 3 depth levels
2. install interval and cron schedules on multiple levels
3. let a scheduled turn launch a child
4. exchange inbox messages while schedules are due
5. restart the daemon under test
6. verify lineage recovery, schedule recovery, and inbox wake recovery
7. verify that post-restart children still resolve to the same root team

#### Scenario E: Rename, stop, resume, and continued messaging

1. create a child topic
2. rename parent and child topics
3. stop one running child
4. resume a different child handle
5. send inbox messages across old and new visible topic titles
6. verify lineage identity remains stable and derived from the original capture
   point, not the rename

#### Scenario F: Very deep lineage

1. create a deterministic chain at least 8-12 levels deep
2. at multiple depths, exchange messages both upward and downward
3. insert a fresh-session child in the middle of the chain
4. insert a user `/fork` child in the lower half of the chain
5. verify the deepest descendant still resolves:
   - correct lineage depth
   - same root team key
   - stable native agent projection

#### Scenario G: Many-to-one fan-in delivery

1. create a tree with at least 10 agents spread across several depths
2. choose one receiver that is not actively being prompted
3. have the other 9-10 agents send distinct deterministic inbox messages to the
   same receiver
4. require that the receiver wakes
5. later ask the receiver to report all tokens using `ReadInbox include_read=true`
6. verify every sent token is present with no silent drops

#### Scenario H: Unrun `/fork` child wake and self-discovery

1. create a user `/fork` child topic but do not prompt it directly
2. from a different agent in the same tree, send a deterministic inbox
   instruction to that child
3. require the child to wake without direct prompting
4. verify the child discovers the inbox message on its own
5. verify the child follows the inbox instruction exactly
6. treat failure here as evidence that wake reminders or default team
   instructions are insufficient

#### Scenario I: `/clear` preserves identity and inbox reachability

1. create a trunk and at least one descendant
2. record the target agent's lineage facts
3. issue `/clear` in that target topic
4. verify the next session forgets earlier conversational facts
5. verify the target still resolves to the same root team key and native agent
   name
6. send an inbox message from another agent after `/clear`
7. require the cleared target to wake and process the message

#### Scenario J: `/new` replaces the agent and makes old sends undeliverable

1. create a parent and child in one tree
2. record the child's lineage facts and inbox projection
3. issue `/new` in the child's topic with an explicit visible emoji and title
4. verify the topic becomes a fresh trunk with a different lineage and different
   root team projection
5. from another agent, try to send to the old child projection
6. require an explicit undelivered result
7. verify the new trunk does not receive that dead-period message

#### Scenario K: Delete or replace during traffic, then respawn same alias

1. create a parent and child
2. stage unread inbox backlog for the child
3. delete the child or replace it with `/new` while other agents are also
   attempting sends
4. verify dead-period sends produce explicit undelivered results
5. respawn a new child from the parent using the same alias
6. require the replacement child to read the staged unread backlog
7. require the replacement child not to see messages that were sent during the
   dead period
8. require future sends after respawn to deliver normally again

#### Scenario L: Concurrent fan-in followed by terminal identity change

1. create a tree with at least 10 agents and one shared receiver
2. start a large fan-in burst into the receiver
3. terminate or replace the receiver during the traffic window
4. verify that messages delivered before the terminal event remain visible
5. verify that later messages become explicitly undelivered
6. verify that senders do not falsely think the dead receiver accepted them

### Deterministic unit coverage

1. bootstrap XML serialization and parsing
2. latest-bootstrap-wins transcript scan
3. inline fork preserving lineage while changing session id
4. root-team projection stability
5. topic naming for `/fork` and `AgentTask alias`
6. compatibility parsing of `description` and `alias`
7. `ReadInbox` defaults from current bootstrap context
8. `session_lineage` tool output
9. root bootstrap team/env priming before first client connect
10. plain lineage route wake from direct notifier
11. plain lineage route wake from unread inbox poll
12. restored lineage route wake after restart
13. `/clear` preserves lineage and route inbox projection
14. `/new` reseeds the topic as a new trunk identity
15. dead recipients return explicit undelivered results
16. a respawned same-lineage replacement becomes deliverable again

## Implementation Phasing

### Phase 1 in this branch

1. spec document
2. canonical bootstrap serializer/parser
3. runtime first-prompt bootstrap injection
4. `/fork` append naming
5. `AgentTask.alias` compatibility layer
6. root-team projection defaults for child workers
7. `session_lineage` tool
8. deterministic unit tests
9. first overlapping live forum smoke scenario additions
10. `/clear` and `/new` lifecycle semantics
11. dead-recipient undelivered messaging

### Later phases

1. lineage-native recipient addressing for inbox messaging
2. lineage persistence in route state store if transcript scan proves too slow
3. fuller UI/debug affordances
4. broader live soak and concurrency sweeps

### Live environment prerequisite

1. Claude SDK/CLI authentication must be valid before Telegram live runs start.
2. Expired Claude auth manifests as opaque subprocess failures during live
   Telegram scenarios and is not specific to lineage/team logic.

## Current Risks

1. Prompt bootstrap size adds some context overhead on every new head.
2. The current team-worker state store is still keyed by native team and native
   agent names, not lineage.
3. Existing live tests assume explicit `description` and explicit team fields in
   several places and will need migration.
4. Queue-operation transcript shape is currently the easiest prompt-search
   target, but parser logic should also support ordinary user-message content.

## Decision Log

### Decided

1. `lineage` is canonical identity.
2. Prompt-injected `<obs-bootstrap>` is the canonical durable bootstrap.
3. Native teams are a flat root-team projection.
4. `/fork` appends to topic names.
5. `alias` replaces `description` at the tool schema.

### Rejected

1. Using multiple native teams to represent the real hierarchy
2. Making `SessionStart additionalContext` the canonical parse source
3. Relying on transcript-only custom system records as the main model-awareness
   mechanism
