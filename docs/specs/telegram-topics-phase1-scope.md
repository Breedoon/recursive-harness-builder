# Telegram Topics Phase 1 Scope

**Status**: Implemented in worktree and live-validated on a real forum supergroup
**Date**: 2026-03-02

## Goal

Scope the next architectural step after Phase 0 single-chat forking:

- one Telegram **forum group chat**
- one **trunk/main session** in General
- additional **sessions in topics** for user-created forks and later agent-created forks
- keep existing single-chat behavior working during migration

This note does **not** propose implementing the full orchestration tree or multi-group
overflow yet. It scopes the minimum safe move to "multiple concurrent sessions inside
one group chat" and the main risks around it.

Important semantic constraint for this phase:

- **reply-to branching stays local to the current topic**
- **only explicit `/fork` creates a new topic**

## Implemented result

The current implementation now does the following:

- routes runtime state by `(chat_id, thread_id)`
- keeps per-route `SessionManager`, `HookState`, pending queue, busy state, and reminder state
- keeps inline reply-forks local to the current topic
- creates new topics only via `/fork`
- supports topic-local `/clear`, `/stop`, `/context`, `/delete`
- supports `/clear all`, `/stop all`, `/delete all` scoped to one group chat
- keeps General as the trunk route

This was live-tested against a real Telegram forum supergroup with a real bot and
real Telethon user session, not only by unit tests.

Validated live behaviors included:

- General and topic isolation
- `/fork` creating child topics from current head
- inline reply-forks remaining in the same topic
- plain follow-up continuing on the active inline fork inside that topic
- `/delete all` from a topic deleting all non-General topics and confirming in General
- `/stop` pausing queued auto-resume until a fresh user message
- daemon survival when a topic is deleted externally while work is in flight

## Executive take

This is a **medium-high refactor**, not a greenfield rewrite.

The Telegram API side is relatively low risk:

- `python-telegram-bot` already exposes `create_forum_topic`, `close_forum_topic`,
  `edit_forum_topic`, `pin_chat_message`, `forward_message`, and `message_thread_id`
  on sends.
- The major uncertainty is **not** topic creation.

The hard part is runtime isolation:

- the current adapter still centralizes active session state, queue state, idle delivery,
  and interrupt behavior around one effective runtime
- the next phase needs those objects to become **per-topic session state**

If the refactor is done around a `SessionRegistry` plus a `TelegramSessionState`
bundle, the change is manageable. If topics are added by threading `message_thread_id`
through the current global state, it will be fragile and likely wrong.

## What exists now

Current Phase 0 code already proves a few important things:

- reply-driven JSONL forking works
- Telegram message ID to JSONL UUID mapping exists in memory
- queued reply-to-old-message behavior works
- `received` / `working` / completion reply threading works
- deterministic and live Telegram fork tests now exist

These are good building blocks. The next phase should preserve them, not replace them.

## Main blockers in current architecture

The current Telegram adapter in `src/obs_agent/telegram.py` still has these singleton
or effectively-singleton assumptions:

1. `self._session_manager`
2. `self._hook_state`
3. `self._pending_messages`
4. `self._last_chat_id` / `self._last_bot`
5. `self._busy_chats`
6. `self._main_session_id` reminder tracking
7. global `_message_map`
8. global `_session_heads`

For a one-group-many-topics model, these need to stop being global runtime state.

### Why this matters

Without isolation, these failures become likely:

- `/stop` in topic A interrupts topic B
- background fork result from topic A is auto-delivered into topic B
- idle poller sends queued output into the last active topic rather than the owning one
- a non-reply message in one topic continues the wrong session because "active session"
  was last updated elsewhere
- reminder or completion summary attaches to the wrong topic
- fork lookup resolves a Telegram message ID from one topic but routes execution through
  the currently active session from another topic

## Recommended runtime shape

### 1. Introduce a routing key

Use an explicit Telegram route key everywhere:

`TelegramRoute = (chat_id, thread_id)`

Where:

- `chat_id`: Telegram supergroup ID
- `thread_id`: `message_thread_id` for forum topics
- for General, use Telegram's General thread ID if present, otherwise a stable sentinel

Do **not** keep routing on `chat_id` alone.

### 2. Introduce per-topic session state

Replace the current singleton state with a bundle:

```python
@dataclass
class TelegramSessionState:
    route: TelegramRoute
    session_manager: SessionManager
    hook_state: HookState
    pending_messages: list[QueuedMessage]
    latest_head_uuid: str | None
    root_session_id: str | None
    parent_session_id: str | None
    parent_message_uuid: str | None
    busy: bool
    last_bot: telegram.Bot | None
    last_activity_at: float | None
    warning_sent: bool
    pinned_message_id: int | None   # later phase
```

The exact fields can vary, but the important point is that:

- queue state
- interrupt state
- background task ownership
- active SDK client
- idle-delivery state

must live with the session/topic, not on the bot singleton.

### 3. Add a registry

Introduce:

```python
class SessionRegistry:
    by_route: dict[TelegramRoute, TelegramSessionState]
    by_session_id: dict[str, TelegramSessionState]
```

This should become the one place that answers:

- which topic/session owns this inbound Telegram message?
- where should outbound assistant/system messages be sent?
- which topic owns this background fork result?
- which topic is the trunk / General session?

### 4. Keep message mapping transport-global, but route-aware

The current in-memory message mapping should evolve to include route metadata:

```python
telegram_message_id -> {
    jsonl_uuid,
    session_id,
    role,
    chat_id,
    thread_id,
}
```

Message IDs are per-chat, not globally meaningful enough for the future shape.
For one group this may appear fine, but once topics share a chat, route-aware mapping
is safer and more explicit.

If you later persist this in SQLite, the normalized shape should likely be:

- Telegram message binding table keyed by `(chat_id, message_id)`
- session registry keyed by `session_id`
- route binding keyed by `(chat_id, thread_id)`

### 5. Keep inline forking mechanics unchanged

The JSONL-copy fork itself is already the right primitive.

Inline reply-forking should remain exactly as Phase 0:

- reply to latest mapped message -> continue in same topic
- reply to older mapped message -> fork inline in same topic
- no topic is created by reply-forking

Topic creation is a separate user action via `/fork`.

What changes for topics is the explicit fork-to-topic path:

- `/fork` without reply -> fork current active head of current topic into a new topic
- `/fork` replying to a mapped message -> fork that exact point into a new topic

So the underlying fork engine should stay mostly unchanged:

1. resolve reply target UUID
2. copy parent chain into a new session JSONL
3. create new `SessionManager` bound to that session
4. either:
   - bind the new session as an inline fork within the same topic
   - or bind the new session to a newly created topic route
5. continue there

## Migration path I would recommend

### Phase 1A: Internal refactor without changing UX much

Refactor Telegram runtime to use a registry and per-topic session state, but still run
everything in one route first.

Why this step matters:

- it de-risks topic support
- existing Phase 0 tests can be adapted with minimal behavior change
- it proves queue/interrupt/background isolation before adding forum mechanics

This is the most important structural step.

### Phase 1B: Forum group support with trunk in General

Once the registry exists:

- inbound routing uses `(chat_id, thread_id)`
- all outbound sends include `message_thread_id`
- General topic owns the trunk session
- `/context`, `/stop`, `/clear`, etc. operate on the current topic's session

At this point you get:

- multiple concurrent sessions in one group chat
- no auto-fork-to-topic yet
- no deep tree UX yet

### Phase 1C: Fork-to-topic

Only after the above is stable:

- `/fork` replying to a mapped message creates a topic
- forked session binds to that topic
- original topic remains unchanged

This should be the first user-facing topic workflow.

### Phase 1D: Agent-initiated fork-to-topic

Only after user fork-to-topic is proven:

- `self_fork` background or explicit tool-initiated fork creates a topic
- child topic receives the work directly
- parent topic gets a status link / breadcrumb

This is a bigger semantic shift than user `/fork`, because current `self_fork` is not
really a routed child agent yet. It is just another SDK fork whose result comes back as
text or a queue message.

## Biggest architectural complications

### 1. `self_fork` is not topic-aware today

Current `self_fork` in `src/obs_agent/tools.py`:

- foreground fork returns plain text into the current run
- background fork enqueues plain text into `hook_state.message_queue`

That is incompatible with a real topic-per-child-agent model.

To support child topics, `self_fork` needs a richer result channel:

- parent session id
- parent route
- child session id
- requested child task
- whether child should stream in its own topic

That likely means `self_fork` cannot remain "just enqueue a string" for the topic
version. It needs either:

- a typed queue event
- or a callback/service layer that the Telegram adapter can subscribe to

This is one of the biggest real refactors.

## Product semantics to lock before implementation

These are the user-facing rules that should be treated as requirements, not implementation details.

### Core concepts

- **Inline fork**: created by replying to an older message; stays inside the current topic.
- **Topic fork**: created only by `/fork`; gets its own topic.
- **General**: the trunk topic for the group chat.

### `/fork` command semantics

`/fork` has exactly two targeting modes:

1. **Reply mode**
   - user replies to a mapped message with `/fork`
   - result: create a new topic fork from that exact message

2. **Head mode**
   - user sends `/fork` without replying
   - result: create a new topic fork from the current active head of the current topic

`/fork` argument text is **not** the first user prompt for the child topic.
It is treated as the requested topic title / label input only.

So:

- `/fork`
  - create topic, create service message, do not start agent work
- `/fork refactor parser`
  - create topic titled from `refactor parser`, create service message, do not start agent work

Every new topic created this way waits for the first actual user message in that topic
before agent work begins.

### Source-topic confirmation behavior

After `/fork` succeeds, the source topic should get exactly one system confirmation:

- reply to the `/fork` command message
- state that a fork topic was created
- include a deep link to the new topic's first service message

No further source-topic chatter is needed unless later phases add pinned indexes or tree summaries.

### New-topic first message

The first message in every `/fork`-created topic should be a service message that acts
as the navigation/provenance anchor.

Recommended minimum contents:

- `forked from:` deep link to the exact source message
- current `session_id`
- compact context snapshot in the same style as the completion summary

The exact source-message deep link is sufficient; a separate parent-topic link is not necessary,
because the message link already lands in the parent topic.

This service message should later become the natural pin target.

### Topic naming convention

Defer fancy summarization. Use deterministic recursive names derived from the parent topic name.

Proposed temporary naming scheme:

- `General`
- `General - F1`
- `General - F2`
- `General - F1 - F1`
- `General - F1 - F2`

Rules:

- numbering is relative to the parent topic
- if parent topic was manually renamed, use the current visible parent topic name
- do not retroactively rename descendants if the parent topic is renamed later
- truncate only if Telegram title length forces it

### Active head semantics

Within a topic, current behavior should remain unchanged:

- plain non-reply message continues the most recently activated branch in that topic
- reply to an old mapped message creates or switches to the inline branch in that topic
- `/fork` without reply uses that same current active head as its source

### Expiry/reminder semantics

For this phase:

- topic sessions should carry their own expiry/reminder state
- General/trunk keeps the current "main session" reminder behavior
- topic forks should also get topic-level reminder behavior when their topic session approaches expiry
- inline forks inside a topic do **not** get independent reminders
- reminder timing is based on **50 minutes since last activity in that topic session**

No topic should be auto-closed or deleted just because its session expired.
Expiry is about session/cache lifecycle, not UI cleanup.

### 2. Background poller must become per-session or registry-driven

Current background idle delivery uses:

- `_last_chat_id`
- `_last_bot`
- one global queue drain

That must be replaced by one of:

1. one poller per `TelegramSessionState`
2. one registry poller that iterates all session states

I would prefer one registry poller over one task per topic, at least initially.
It is simpler to observe and less likely to spin out extra concurrency bugs.

### 3. Commands become topic-scoped

Commands that are currently "current bot runtime" commands need clear semantics in a forum:

- `/clear`
- `/stop`
- `/context`
- `/delete`

Recommended topic semantics:

- `/context`: report this topic's session
- `/stop`: interrupt only this topic's active work
- `/clear`: reset only this topic's session
- `/delete`: delete this topic (not General)

Whole-workspace forms should also exist, but only behind an explicit `all` suffix:

- `/stop all`: stop all topics in the group chat
- `/clear all`: clear all topic sessions in the group chat
- `/delete all`: delete all topics except General

Command descriptions registered with Telegram should mention this explicitly so the user
can discover the scope without trial and error.

### 4. Reminder semantics need to be redefined

The current reminder shape is not yet correct for topics.

Desired semantics:

- reminder is tied to the owning topic session
- reminder uses last-activity time, not session creation time
- General/trunk gets reminder behavior
- explicit topic forks also get reminder behavior
- inline forks in a topic do not get independent reminders

Reminder ownership should be stored in registry/session metadata, not the current bot-level fields.

### 5. Stop semantics need to change

Current stop behavior is too continuation-friendly for the topic model.

Desired semantics:

- `/stop` stops active work in the current topic
- if multiple inline branches in that topic are active, `/stop` stops all of them
- queued messages in that topic remain queued
- queued messages do **not** auto-run immediately after stop
- queued messages resume only after a new user message arrives in that topic
- when that happens, older queued messages are delivered first and the new user message is appended after them

Whole-workspace semantics:

- `/stop all` stops active work in all topics in the group chat
- queued messages across topics remain queued until fresh user activity resumes them in their respective topics

### 6. Crash resistance is a first-class requirement

The daemon should not crash because Telegram/topic state becomes invalid mid-run.

Acceptable degraded outcomes:

- a send fails and the user sees no message or an error message
- a message is dropped in pathological topic-state situations
- a topic operation partially fails and the bot reports the failure

Unacceptable outcome:

- daemon process dies
- unrelated topics stop working because one topic hit a transport or topic-state failure

This suggests the runtime should tolerate missing live SDK client objects and recreate
them lazily from session metadata when needed.

### 7. Topic route discovery in tests and userbot tooling

The Bot API can create and send to topics, but:

- listing existing topics is not available in Bot API
- creating groups / toggling forum mode also needs userbot/Telethon

That means production Phase 1 user flows are fine with Bot API, but test harness and
environment setup likely want a small Telethon helper layer for:

- creating or resetting the test group
- enabling forum mode
- discovering topic IDs after creation
- optionally cleaning topics or recreating the group

## Forwarding and odd cross-topic reply policy

Forwarded content should be treated as ordinary inbound user content once it is consumed
by a topic session.

That means:

- forwarding content into a topic is allowed
- if that forwarded content becomes a real user JSONL entry in that topic, it should be a valid fork point there

The only thing to avoid is confusing a forwarded copy with the original native message
identity from some other topic.

For weird cross-topic reply cases, prefer permissive behavior if it is cheap and safe.
If clean handling adds a lot of complexity, it is acceptable for Phase 1 to leave those
cases loosely specified rather than aggressively restricting the user.

## Edge cases to plan for

These are the scenarios most likely to break once multiple topics exist.

## Deterministic Telegram-first test plan

These tests are meant to prove that a topic behaves like today's single-session bot,
except now several such sessions can coexist safely in one forum group.

Primary oracle set:

1. Telethon-observed message metadata
   - `chat_id`
   - `message_id`
   - reply target
   - topic/thread identifier

2. `/context` output
   - session ID
   - JSONL file path
   - compact context snapshot

3. `session_info` / `context_info`
   - confirm tool-visible session ID matches topic-visible session ID

4. JSONL filesystem assertions
   - new session files created when expected
   - copied UUID ancestry is correct
   - unrelated branch entries are absent

### Harness changes required

The current Telethon harness is DM-shaped. For topic testing it should gain a forum-group mode that can:

- connect to a specific test supergroup
- send to a specific topic
- reply within a specific topic
- observe topic/thread IDs on bot replies
- create/reset topics for tests
- optionally recreate the whole test group when a clean slate is needed

Bot API is enough for production topic creation, but Telethon/userbot support is still
useful for deterministic test setup and teardown.

### Baseline topic parity tests

1. **General topic behaves like current single-chat bot**
   - warm up General
   - confirm `/context`, `session_info`, and `context_info` agree on session ID
   - confirm completion summary and reminder formatting still work there

2. **Per-topic command scoping**
   - create topic A and topic B
   - confirm `/context` in each reports different session IDs
   - confirm `/clear` in A does not reset B
   - confirm `/stop` in A does not interrupt B

3. **Topic-local continuity**
   - seed distinct tokens in General, A, and B
   - ask each topic a deterministic recall question
   - confirm each sees only its own topic history

### Inline fork behavior inside a topic

4. **Reply to latest mapped message continues same session in topic**
   - no new JSONL file
   - same topic session ID

5. **Reply to older mapped message forks inline in same topic**
   - same topic
   - different session ID
   - JSONL ancestry truncated to the correct UUID

6. **Plain follow-up after inline fork stays on that inline branch**
   - no topic creation
   - same inline session ID

7. **Repeated inline forks from same anchor produce distinct sessions in same topic**
   - several distinct session IDs
   - no topic creation

8. **Fork-from-inline-fork stays local**
   - build fork-from-fork chain within one topic
   - verify session routing and ancestry correctness

### `/fork` topic-creation flows

9. **`/fork` without reply creates topic from current active head**
   - create new topic
   - source topic gets one confirmation reply with deep link
   - child topic gets one service message
   - child topic session matches the fork source head ancestry

10. **`/fork` replying to an older mapped message creates topic from that point**
   - same assertions as above
   - copied JSONL excludes later parent-topic history

11. **`/fork some label` uses label as topic naming input only**
   - topic title reflects the label
   - no agent work starts in child topic
   - child topic waits for actual user message

12. **First user message in child topic starts work there**
   - service message exists first
   - first actual user prompt generates `received` / `working` / response inside the child topic

13. **Promoting an inline fork to a topic works**
   - create inline fork in a topic
   - run `/fork` replying to a message from that inline branch
   - child topic session matches that inline branch ancestry

14. **Current topic remains unchanged after `/fork`**
   - send plain follow-up in source topic
   - confirm source topic still continues its own active head
   - child topic continues independently

15. **Forwarded content becomes normal topic history once consumed**
   - forward content into a topic
   - confirm it becomes part of that topic's history
   - if it yields a mapped user entry, confirm it is forkable like other user entries

### Topic naming tests

16. **Recursive default naming**
   - `General -> General - F1`
   - second child from General -> `General - F2`
   - child from `General - F1` -> `General - F1 - F1`

17. **Manual rename only affects future children**
   - rename parent topic manually
   - new child inherits renamed parent title prefix
   - existing child topic names remain unchanged

### Queueing and concurrency isolation

18. **Topic A busy, queued follow-up in A, topic B idle**
   - queued message drains back into A only
   - B receives nothing

19. **Topic A busy, plain conversation continues in B**
   - B remains responsive while A works
   - no session or queue contamination

20. **Topic A busy, reply-to-old-message queued in A**
   - when drained, it forks inline in A
   - B unaffected

21. **Topic A busy, `/fork` issued in A while B active**
   - new topic created from A's intended source
   - B unaffected

22. **Busy in A and B simultaneously**
   - both can run concurrently
   - each topic’s `working` marker replies to the right user message in that topic
   - no cross-topic completion summaries

23. **Queued messages in multiple topics at once**
   - A and B each queue follow-ups while busy
   - each topic later drains only its own queue

24. **Stress: 5 topics active concurrently**
   - seed five topics
   - interleave messages and `/context` checks
   - verify all session IDs remain stable and distinct

25. **Stress: 10 repeated topic forks from same parent head**
   - verify topic creation, deep-link confirmations, and distinct JSONL files

26. **`/stop` leaves queued messages queued**
   - queue several messages in a busy topic
   - issue `/stop`
   - confirm active work stops
   - confirm queued messages do not auto-run
   - send one fresh user message
   - confirm queued messages resume only then, with the new message appended after them

27. **`/stop all` freezes all topics**
   - have multiple active or busy topics
   - issue `/stop all`
   - confirm all active work stops
   - confirm no topic auto-resumes queued work until fresh user activity arrives there

28. **`/clear all` resets all topic sessions without deleting topics**
   - verify each topic gets fresh session state

29. **`/delete all` removes all non-General topics without crashing**
   - verify General survives
   - verify daemon remains healthy after deletion sweep

### Attachments and media

30. **File upload in topic A while topic B is active**
   - `received` is immediate in A
   - normalization and agent work happen in A
   - B unaffected

31. **Media group in topic A while topic B receives text**
   - only one receipt marker for the album in A
   - B continues normally

32. **Attachment-driven `/context` parity**
   - after attachment processing in child topic, `/context` and tool session IDs still agree

### Reminder/expiry behavior

33. **Reminder fires from last activity, not topic birth**
   - shorten timing in test config
   - confirm reminder timing follows last activity updates

34. **General reminder fires only for General session**
   - simulate or shorten window
   - confirm General gets reminder
   - sibling topic does not get General’s reminder

35. **Topic reminder fires for topic session, not inline forks**
   - create topic with multiple inline forks
   - only topic-level session reminder appears

36. **Reminder does not cross topics**
   - topic A nearing expiry
   - reminder appears only in A
   - B unaffected

37. **Expiry resets only the owning topic session**
   - expire A
   - B remains resumable/unchanged

### Restart and persistence boundaries

38. **Restart preserves per-topic ordinary continuation where possible**
   - after restart, send plain follow-up in topic A and B
   - confirm the selected restoration policy works

39. **Restart loses in-memory reply-fork mapping unless persistence exists**
   - reply to old message after restart
   - expect explicit failure if mapping is unavailable

40. **Topic registry rebuild does not mix sessions**
   - after restart, `/context` in different topics still resolves to the correct topic session or explicit fallback behavior

### Negative / edge-case tests

41. **`/fork` on unmapped/system-only message fails cleanly**
   - no topic created
   - clear error reply in source topic

42. **Odd cross-topic replies do not crash the daemon**
   - reply in topic B to a message originating from topic A or a forwarded copy
   - whatever semantics are chosen, daemon stays healthy and routing remains usable

43. **Topic creation failure does not orphan runtime state**
   - if Bot API topic creation fails, no half-bound child session remains active

44. **Service message send failure after topic creation**
   - confirm recovery path is explicit and source topic reports failure clearly

45. **Deep link points to correct service message**
   - click/resolve target and confirm it lands in the child topic

46. **Long split assistant output in one topic while another topic completes**
   - message splitting and completion detection remain topic-local

47. **Topic deleted while actively writing does not crash daemon**
   - delete a topic during an active run
   - confirm send failures are contained
   - confirm daemon and other topics continue working

48. **Topic deleted while queued work exists does not crash daemon**
   - delete topic with pending queue
   - confirm daemon survives and other topics remain healthy

49. **Writing to deleted or invalid topic degrades cleanly**
   - no daemon crash
   - no registry corruption

50. **Permissions loss in one topic does not kill whole runtime**
   - if the bot loses the ability to write there, failure is scoped and surfaced

51. **Repeated topic create/delete churn does not obviously leak runtime state**
   - create and delete many topics
   - confirm routing and registry remain consistent

### Stress and soak tests

52. **Soak: 5 topics, 5 minutes, interleaved deterministic prompts**
   - repeated `/context`
   - repeated deterministic recall questions
   - verify no drift or leakage

53. **Soak: attachments + forks + `/fork` + queueing mixed**
   - one topic does file ingestion
   - one topic does repeated inline forks
   - one topic creates new topics via `/fork`
   - verify all routes stay isolated

54. **Crash-resistance soak with abnormal chat mutations**
   - mix topic deletion, stop-all, clear-all, attachment sends, and active runs
   - primary success criterion is daemon survival plus continued operability of unaffected topics

### Logging requirements on failure

For every Telegram topic test failure, capture:

- recent bot messages with topic/thread metadata
- relevant user messages with topic/thread metadata
- bot log tail
- session IDs by topic from `/context`
- created topic IDs and names
- created JSONL paths

This is necessary because some failures will be behavioral rather than purely structural.

## Edge cases to plan for

### Routing / isolation

1. message in topic A while topic B is busy
2. queued message in topic A while topic B finishes and auto-delivers
3. `/stop` in topic A while topic B is in a long tool run
4. `/clear` in topic A while topic B still has queued work

### Forking semantics

5. reply in topic A to an old message from topic A -> inline fork in A
6. `/fork` in topic A with no reply -> create topic C from A's active head
7. `/fork` in topic A replying to old mapped message -> create topic C from that point
8. reply in topic A to a forwarded message from topic B
9. reply in topic A to a system marker mapped to topic A
10. reply after daemon restart where mapping is missing

Forwarded messages are subtle because their visible origin is not the same thing as a
native message identity in the destination topic. The desired behavior is:

- once forwarded content is consumed as user input in the destination topic, it behaves
  like that topic's own history
- but the runtime should not confuse the forwarded copy with the original native message
  object from the source topic

### Topic lifecycle

10. topic created but first agent send fails
11. topic exists but session creation fails
12. parent topic closes while child topic is still running
13. topic rename/close races with active sends

### Failure tolerance

14. topic deleted while session active
15. topic deleted while queued work exists
16. topic service message creation partially fails
17. topic send path temporarily errors or loses permissions
18. one topic hits transport errors while other topics remain healthy

### Background forks

19. background fork launched from topic A completes while topic A is idle
20. background fork launched from topic A completes while topic B is active
21. multiple background forks from different topics complete close together

### Telegram transport specifics

22. attachment/media group in topic A while text messages arrive in topic B
23. long split assistant output in topic A while topic B sends completion summary
24. reply target exists in same chat but different topic

That last one matters: reply resolution should not assume "same chat means same session".
The binding must carry topic route and session identity.

## Difficulty assessment by feature

### Low uncertainty

- pass `message_thread_id` through send paths
- create topics via Bot API
- route inbound PTB messages by `message_thread_id`
- keep General as trunk

### Medium uncertainty

- refactor Telegram adapter around a registry without destabilizing current tests
- define `/clear`, `/stop`, `/context`, `/delete` semantics per-topic
- keep reply-driven forking behavior intuitive when topic routing exists

### High uncertainty

- agent-initiated `self_fork` into child topics
- background fork result routing as structured events instead of plain queue text
- crash recovery and restart behavior before SQLite persistence
- any attempt to support deep tree navigation in one group before the basic topic model is stable

## What I would explicitly defer

These should not be bundled into the first topic milestone:

1. multi-group recursive overflow
2. Codex or multi-SDK backends
3. evaluator-agent permission flows
4. per-topic pinned service message updates
5. topic list management / pinning / group creation automation
6. persistent SQLite registry
7. background commands each getting their own topic by default

All of these are compatible with the registry design, but they are not needed to prove
the core multi-topic runtime.

## Deterministic test plan

The new topic phase should be validated primarily with deterministic Telegram tests plus
JSONL assertions.

### Test harness changes needed

`tests/evals/platform_telegram.py` currently talks to a direct bot chat. For topic
testing it should grow a group/forum mode:

- connect to a specific supergroup, not only the bot DM
- send with `reply_to` inside a chosen topic
- collect bot messages with:
  - `chat_id`
  - `message_id`
  - `reply_to_message_id`
  - topic identifier (`message_thread_id` or Telethon top message/topic metadata)
- optionally create / reset the test group through Telethon helper code

I would not discard the existing DM-style tests. Keep them as a backward-compatibility lane.

### Core deterministic scenarios

1. **General topic continuity**
   - send in General
   - confirm `session_info` and `/context` agree
   - confirm no topic cross-talk

2. **Two-topic isolation**
   - create topic A and topic B sessions
   - ask each for session ID only
   - confirm IDs differ
   - send follow-ups in both
   - confirm each remembers only its own topic history

3. **Topic-local `/stop`**
   - start long run in topic A
   - start or keep idle topic B
   - `/stop` in topic A
   - confirm A stops, B remains unaffected

4. **Topic-local `/clear`**
   - clear topic A
   - confirm topic B still remembers its prior context

5. **Fork-to-topic from old message**
   - build trunk conversation in General
   - `/fork` reply to older mapped message
   - assert new topic exists
   - assert child session JSONL is copied to the right UUID
   - assert subsequent plain message in child continues there
   - assert General still continues trunk

6. **Reply to latest in-topic message is not a fork**
   - reply to latest message in a topic
   - assert no new session file
   - assert same topic/session continues

7. **Fork-from-fork to another topic**
   - child topic forks again
   - assert grandchild topic session chain is correct

8. **Queued while busy in one topic**
   - long run in topic A
   - queued message in topic A
   - activity in topic B
   - assert queued delivery returns to topic A only

9. **Background fork routing**
   - launch background fork from topic A
   - assert result or child topic appears under A, not B

10. **Attachment isolation**
    - file in topic A
    - text in topic B
    - assert `received`/`working`/result route correctly

11. **Restart boundary**
    - restart daemon
    - confirm route/session registry can be rebuilt enough for normal continuation
    - confirm old fork-via-reply limitations are explicit if mapping remains in-memory

### JSONL-level assertions

For all fork scenarios, verify:

- new session file count
- copied UUID chain exactly matches fork target ancestry
- excluded later turns are absent
- child and parent sessions diverge after fork

### Logs on failure

Because these will remain partially nondeterministic, failures should capture:

- recent Telegram messages with topic metadata
- bot log tail
- current session IDs by topic
- created JSONL paths

## Suggested implementation order

If this were the next execution plan, I would do it in this order:

1. internal registry refactor while preserving one-route behavior
2. topic-aware test harness support
3. forum group trunk session in General
4. per-topic command scoping
5. user `/fork` to topic
6. only then revisit `self_fork` into topic

This order keeps the highest-risk refactor separate from the most novel UX feature.

## Bottom line

This is feasible without a rewrite, but only if the next step is framed as:

**"replace singleton Telegram runtime state with a route-keyed session registry"**

not:

**"sprinkle `message_thread_id` over the existing bot."**

The first version should focus on:

- General topic trunk
- per-topic isolation
- user-initiated fork-to-topic

and explicitly defer:

- deep recursion
- multi-SDK
- background command topics
- persistent recovery

Those later features fit naturally once the registry exists. They do not fit naturally
into the current singleton shape.
