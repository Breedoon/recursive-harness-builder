# Telegram /stop Interrupt Reliability

**Status**: Proposed (implementation in this branch)
**Date**: 2026-03-12
**Owner**: Telegram runtime (`src/obs_agent/telegram.py`)

## Context

Current Telegram `/stop` behavior is built around a "pause queued auto-resume"
mode. It acknowledges with `interrupt sent`, but it does not send SDK
`client.interrupt()` from Telegram command handling. This leads to user-visible
cases where `/stop` appears to do nothing or delays interruption until an
incidental hook boundary.

The desired behavior for this change is to prioritize real interruption over the
existing pause model.

## User Requirements (Priority Order)

1. `/stop` must actually interrupt current work in the current topic.
2. Keep normal platform completion behavior:
   - command acknowledgement (`interrupt sent`)
   - normal completion summary flow (`context: ...`, schedule lines, etc.)
3. Do not keep the legacy "pause until next user message" behavior.
4. Natural runtime wake-ups are acceptable after interruption completes
   (schedules, queued poller runs, normal lifecycle behavior).
5. `/stop all` should stop all active work across chat topics, including
   delegated topic tasks (AgentTask/ForkTask children).
6. `/stop` (single topic) should not blanket-stop all delegated background tasks
   launched from that parent topic.
7. Do not add a new `/interrupt` command in this change.
8. Agent should receive lightweight context that the previous run was user-interrupted.
9. Completion message ordering must be deterministic: completion summary should not arrive before delayed assistant output from the same run.
10. Queued user messages should keep lifecycle markers accurate (`received` on enqueue, `working` when actually delivered to model).
11. User-facing `notification: agent task running/idle` noise should be removed.

## Semantics

### `/stop` (current topic)

- Set route interrupt intent and hook interrupt flag.
- Attempt SDK-level interrupt immediately for this route if a connected client
  exists.
- Do not enable long-lived pause mode.
- End the in-flight run promptly and skip in-run continuation/background loops
  for that interrupted run only.
- Emit normal completion summary as usual.
- Set a one-shot interrupt-awareness note for the next model query in this route:
  - lightweight system preface (no required response)
  - purpose: make interruption explicit to the model when it resumes

### `/stop all` (chat scope)

- Apply route interrupt intent + SDK interrupt attempt to all routes in this
  chat.
- Additionally stop delegated topic tasks (AgentTask/ForkTask) across the chat.
- Preserve normal completion signaling per affected route.

### Completion ordering

- Completion summary is sent after turn flush and observability flush, with assistant-delivery ordering semantics.
- Transport priority for completion summary must not leapfrog earlier assistant chunks from the same run.

### Queued message lifecycle

- On enqueue while busy:
  - send `received` marker immediately.
- When queued user messages are actually delivered back into model context:
  - emit `working` marker at delivery time.
- Do not claim immediate interruption at command receipt; allow already-produced pending transport messages to drain.

### User notification noise

- Suppress parent-route lifecycle spam for `agent task running` and `agent task idle`.
- Keep meaningful child lifecycle/service messages (launch/completion/stopped, links).

## Non-Goals

- No new slash command aliases.
- No special schedule suppression logic beyond existing runtime behavior.
- No transport-specific fake terminal messages to simulate interruption.

## Design

### 1. Real interrupt path in Telegram handler

`handle_stop()` will:

- set `interrupt_flag = True` (hook fallback),
- set a transient `interrupt_requested` latch used to short-circuit current run
  continuation loops,
- call SDK `client.interrupt()` when route client is connected.
- set one-shot route-local interrupt-awareness latch for next run context preface.

### 2. Replace pause-based short-circuiting with transient interrupt latch

Add `HookState.interrupt_requested: bool` and use it in `ConversationRunner`:

- continuation loop and background-fork wake loop stop when latch is set,
- remaining queue is preserved as `pending_messages`,
- latch is cleared at the end of `_run_and_send` so normal auto-delivery can
  resume naturally afterward.

### 3. Interrupt-awareness system context

Add `HookState.interrupt_notice_pending`:

- set when `/stop` is accepted for a route,
- consumed once by `_run_and_send` to prepend a light system note to next query.

This avoids silent interruptions where the model does not realize the previous
turn was user-stopped.

### 4. Task stop scope rules

- `/stop`:
  - interrupt only current route execution,
  - only mark child-route task terminal requests for that same route.
- `/stop all`:
  - use existing route cancellation path to stop delegated topic tasks across
    the chat.

This avoids false "stopped" terminalization of unrelated AgentTask workers on
single-topic stop.

### 5. Completion summary ordering fix

- Send completion summary using assistant-priority transport ordering (not
  system-priority preemption), after prior assistant chunks are flushed.
- This preserves architectural ordering: completion marker is terminal for the
  run in user-visible stream order.

### 6. Queued-message working marker

- Detect queue-delivery status events (`queued message delivered`) during
  in-flight run.
- Emit `working` marker at that moment so queued user input has correct visible
  lifecycle.

### 7. Parent lifecycle notification suppression

- Remove Telegram user-facing emissions for `notification: agent task running`
  and `notification: agent task idle`.
- Keep other lifecycle outputs intact.

## Test Strategy

## Unit tests (fast, deterministic)

1. `/stop` calls SDK interrupt on connected route client.
2. `/stop all` calls SDK interrupt for all targeted routes.
3. `/stop` no longer sets pause mode.
4. `/stop` does not mark parent-only AgentTask records as terminal.
5. `/stop all` stops delegated tasks across routes.
6. interrupted-run short-circuit is transient; it does not freeze later
   auto-delivery.
7. next query after `/stop` includes one-shot interrupt-awareness preface.
8. completion summary dispatch ordering does not preempt prior assistant chunks.
9. queued user delivery emits `working` at delivery-time.
10. `agent task running/idle` lifecycle notifications are not sent to user.

## Live Telegram stress scenarios

Use existing forum/smoke harness patterns as templates:

1. Start deep work (reads + bash + long response), issue `/stop` mid-flight,
   assert interruption marker/completion behavior and no stuck run.
2. Queue follow-up while busy, then `/stop`, assert prompt returns and runtime
   can naturally continue without manual unpause protocol.
3. Parallel delegated tasks (including long-running children), `/stop all`,
   assert all children are stopped.
4. Multi-topic overlap: stop one topic and verify other topic remains healthy.
5. Repeated race probe (e.g. 10 runs): verify completion summary remains final
   run marker, with no delayed same-run assistant chunk arriving after it.
6. Queue while busy + stop race: verify queued message gets `received`, then
   `working` exactly when delivered to model (not just on enqueue).

These scenarios are inspired directly from:

- `tests/test_telegram_live_forum_topics.py`
- `tests/test_telegram_live_smoke.py`

to avoid blind spots from isolated synthetic tests.

## 2026-03-13 Reliability Hardening Addendum

### A. Out-of-sync queue/transport ordering root cause

- Root cause: background auto-delivery could feed queued model updates while Telegram
  transport still had unsent assistant operations for the same chat.
- Effect: model-visible state advanced ahead of user-visible delivery under burst/race
  conditions, producing "out of sync" reports.

### B. Out-of-sync fix (implemented)

- Background poller now blocks auto-delivery whenever `chat_pending_ops > 0`.
- Gate is checked both:
  - before entering route lock, and
  - again inside lock right before `_run_and_send`.
- If blocked inside lock, drained queued messages are re-queued (no loss).

### C. Daemon/runtime robustness hardening (implemented)

- Telegram runtime now runs under an internal supervisor loop:
  - one runtime instance is started,
  - unexpected runtime stop/crash triggers bounded backoff restart.
- Fail-fast exceptions (configuration errors like invalid token/allowlist) are not
  retried indefinitely.
- Startup/shutdown now use explicit best-effort teardown for bot, updater, and app so
  partial failures do not strand resources.

### D. Claude subprocess non-essential traffic hardening (implemented)

- Default SDK env now includes:
  - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
- Rationale: disable non-essential side-channels in Claude Code subprocesses to reduce
  fragility from optional background behavior while preserving core query/tool flow.

### E. Regression tests added

- `tests/test_telegram.py`
  - `test_auto_delivery_waits_for_transport_backlog_to_drain`
  - `test_auto_delivery_stress_respects_transport_backlog`
  - `test_restarts_after_runtime_error`
  - `test_fatal_error_is_not_retried`
- `tests/test_session.py`
  - `test_includes_default_sdk_hardening_env`
