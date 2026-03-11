# Telegram Topic Scheduling Phase 1 Scope

**Status**: Proposed  
**Date**: 2026-03-10

## Goal

Add reliable per-topic scheduling for Telegram routes, with a design that stays minimal now but cleanly extends to phase-2 `settings.json` profiles.

Phase 1 remains focused on scheduling (not full idle-preservation implementation).

## Design Direction (Updated)

1. `Cron*` tools will look native (`CronCreate/CronList/CronDelete`, `cron + prompt + recurring`) for compatibility.
2. Runtime engine is still interval/event based in phase 1.
3. `CronCreate` returns a clear compatibility warning that full cron semantics are not yet implemented.
4. `interval_seconds=0` is valid and maps to `on_topic_stop` trigger semantics.
5. `on_topic_stop` is powered primarily by Claude SDK `Stop` hook callbacks.
6. Stop events are resolved to Telegram routes via route-bound `HookState` callbacks and/or `session_id -> route` mapping.
7. The legacy 50-minute idle warning is deprecated for all routes.
8. Completion output is unified into one completion message shape across user turns, sub-agents, and scheduled runs.

## Why Stop Hook Works Here

For topic scheduling we need:

1. strict route ownership,
2. no cross-topic bleed,
3. no duplicate trigger on active/busy routes.

This is achievable with Stop hooks because each Telegram route has its own `SessionManager + HookState`. We can attach a route-bound stop callback in `HookState` and use `hook_input.session_id` as a secondary guard.

Idle reminder reliability concerns are addressed by removing the legacy idle warning path.

## Phase 1 Scope

1. Topic-attached schedules with prompt injection.
2. Trigger kinds:
   - `interval` (minutes/hours/days represented as seconds),
   - `on_topic_stop` (`interval_seconds=0`).
3. Run controls:
   - `run_mode`: `continue` or `reset_session`.
4. End conditions:
   - optional `max_runs`,
   - optional `until` timestamp.
5. No catch-up replay while runtime is down; on resume, run once if due, then continue cadence.
6. Completion summary includes next scheduled run ETA.
7. Legacy 50-minute idle warning is not emitted.
8. Sub-agent completion messages include context and optional return-to-parent link.

## Non-Goals (Phase 1)

1. Full cron parser/calendar semantics.
2. Native CLI cron parity beyond tool names/shape.
3. Shell-command schedules.
4. Full inheritance policy implementation.
5. Phase-2 idle-preservation profile activation logic.

## Unified Model (Minimal, Future-Proof)

Each schedule record has four parts:

1. `trigger`:
   - `kind`: `interval` | `on_topic_stop`
   - `interval_seconds` (for `interval`; `0` means `on_topic_stop` at API layer)
2. `action`:
   - `prompt`
   - `run_mode`
3. `lifecycle`:
   - `enabled`
   - `run_count`
   - `max_runs` (optional)
   - `until` (optional)
4. `scope`:
   - `route` (chat + topic thread)
   - `inherit` (stored now, default `none`; phase-2 behavior extension)

This keeps moving parts low while leaving room for settings-driven profiles.

## MCP Tool API (Native-Shaped + Phase-1 Extensions)

### `CronCreate`

Parameters (native-shaped):

- `cron` (string, required): cron-like expression.
- `prompt` (string, required): prompt text (multiline allowed).
- `recurring` (boolean, optional, default `true`).

Phase-1 extensions:

- `interval_seconds` (integer, optional): override derived interval.  
  `0` means `on_topic_stop`.
- `run_mode` (string, optional): `continue` (default) | `reset_session`.
- `description` (string, optional).
- `max_runs` (integer, optional).
- `until` (RFC3339 timestamp, optional).
- `inherit` (string, optional): `none` (default), future `children`.

Behavior:

1. Always returns a warning text: full cron semantics are not yet implemented; interval/event scheduler is used.
2. Conversion priority:
   - if `interval_seconds` provided, use it (`0` => `on_topic_stop`);
   - else parse limited cron subset to interval seconds;
   - else return error with supported subset guidance.
3. If `recurring=false`, set `max_runs=1` unless caller set a stricter value.

Supported cron subset in phase 1:

- `*/N * * * *` => every `N` minutes
- `0 */N * * *` => every `N` hours
- `0 0 */N * *` => every `N` days
- `@hourly`, `@daily` convenience aliases

### `CronList`

Lists schedules for current topic route only.

Returns:

- schedule entries ordered by next-due semantics
- trigger kind (`interval` / `on_topic_stop`)
- lifecycle fields (`enabled`, `run_count`, `max_runs`, `until`)

### `CronDelete`

Deletes one schedule by ID in current route.

Returns:

- `deleted: true|false`
- `id`

## Trigger Semantics

### `interval`

Runs when due on poll ticks and route is runnable.

### `on_topic_stop` (`interval_seconds=0`)

Runs when a topic turn completes, but only for allowed source kinds.

Phase-1 source gating:

1. fire on stop events for that topic route,
2. do not re-trigger from runs that were themselves schedule-originated.

This prevents self-looping stop schedules.

## Data Model

Persisted schedule entity:

```python
@dataclass(frozen=True)
class PersistedTopicSchedule:
    schedule_id: str
    chat_id: int
    thread_id: int | None
    description: str | None
    cron_expr: str | None
    trigger_kind: str  # "interval" | "on_topic_stop"
    interval_seconds: int | None
    prompt: str
    run_mode: str  # "continue" | "reset_session"
    enabled: bool
    run_count: int
    max_runs: int | None
    until_ts: float | None
    inherit_mode: str  # "none" (phase 1)
    next_run_at: float | None  # null for on_topic_stop
    last_run_at: float | None
    last_success_at: float | None
    last_error: str | None
```

SQLite table: `topic_schedule` with indexes on route and due scans.

## Runtime Architecture

### Core State

`TelegramBot` adds:

- `_schedules_by_id`
- `_schedule_ids_by_route`
- `_schedule_running_by_route`

### Scheduler Inputs

Two input channels:

1. poll tick (`_background_poller_loop`) for interval schedules,
2. SDK `Stop` hook callback for `on_topic_stop` (route-bound via `HookState`, validated by `session_id`).

### Execution Path

All schedule triggers call a common launcher:

1. verify route state and lock/busy gates,
2. enforce `enabled`, `max_runs`, `until`,
3. run prompt via `_run_and_send` synthetic invocation,
4. persist success/failure and next state.

### Session Mode

- `continue`: run on current session head.
- `reset_session`: call `_reset_route_state` before run.

## Inheritance

Phase-1 default: no inheritance (`inherit=none`).

`inherit` is stored now for forward compatibility but not acted on yet unless explicitly implemented in phase 2.

## Notifications and UX

1. `CronCreate`/`CronDelete`: system confirmations (silent notification).
2. Scheduled run markers: optional silent marker.
3. Unified completion message for all agent finishes:
   - always include context snapshot,
   - include `subtask` status when applicable,
   - include `return_to_parent` when applicable,
   - include schedule trigger and next-run info when applicable.
4. Do not append notify-username handles to completion messages.
5. Do not emit legacy 50-minute idle warning.

## Reliability Gates

1. Never launch schedule while route lock held or `state.busy`.
2. At most one schedule launch in-flight per route.
3. Stop-trigger source gating prevents self-loops.
4. Persist state transition atomically around run attempt.
5. Poller exception isolation (one failure does not stop scheduler loop).
6. Stop-hook delivery is treated as primary; optional `_run_and_send` completion fallback may be added if soak tests show missed Stop events.

## Phase 2: `settings.json` Integration Model

To keep repo/server separation clean:

1. **Repo-level (`settings.json`) stores schedule profiles**, not topic runtime state.
2. **Server-side state stores topic bindings + runtime counters/timestamps.**

Proposed `settings.json` namespace:

```json
{
  "obs_agent": {
    "schedule_profiles": {
      "session_preserve": {
        "trigger": {"kind": "on_topic_stop"},
        "action": {"prompt_file": ".claude/prompts/session-preserve.md", "run_mode": "continue"},
        "lifecycle": {"max_runs": 1}
      }
    },
    "schedule_defaults": {
      "root_topic_profiles": ["session_preserve"],
      "child_topic_profiles": []
    }
  }
}
```

Phase-2 flow:

1. tool creates topic binding from profile, or
2. runtime auto-binds profile on topic creation based on defaults.

This keeps per-topic IDs and execution state out of the repo while allowing vault-level behavior authoring.

## Minimal Moving Parts Recommendation

Implement now:

1. `Cron*` tool compatibility surface + warning.
2. Trigger kinds `interval` and `on_topic_stop` only.
3. Lifecycle options `max_runs` and `until`.
4. Route-bound SDK `Stop` hook handling for `on_topic_stop`.
5. `inherit` field stored but behavior deferred.
6. Unified completion message contract.

Do not implement now:

1. full cron parser,
2. profile auto-binding from settings,
3. inheritance copy behavior,
4. profile auto-enable/disable orchestration logic beyond schedule primitives.

## Implementation Touch Points

1. `src/obs_agent/tools.py`
   - add `CronCreate`, `CronList`, `CronDelete`.
   - return compatibility warnings in tool payloads.
2. `src/obs_agent/hooks.py`
   - add schedule callback slots to `HookState`.
   - register `Stop` matcher that invokes route-bound schedule stop notifier.
3. `src/obs_agent/telegram.py`
   - bind callbacks in `_build_session_state`.
   - add schedule create/list/delete handlers.
   - add interval scheduler in `_background_poller_loop`.
   - resolve stop hook events to route/session and launch `on_topic_stop` schedules.
   - remove legacy route-idle warning emission.
   - implement unified completion message builder and apply it to root + sub-agent completion paths.
   - append schedule metadata (triggered + next run) in completion messages.
4. `src/obs_agent/telegram_state_store.py`
   - add `topic_schedule` persistence and snapshot wiring.
5. `tests/`
   - update tool registration assertions.
   - add parser/trigger/lifecycle tests.
   - add stop-trigger anti-loop tests.

## Smoke Test Matrix (Reliability)

### Deterministic Local

1. `CronCreate` returns compatibility warning + creates runnable schedule.
2. limited cron subset conversion works; unsupported cron fails hard.
3. `interval_seconds=0` creates `on_topic_stop`.
4. `on_topic_stop` fires after non-schedule-origin Stop events.
5. `on_topic_stop` does not self-loop after schedule-origin turn completion.
6. `max_runs=1` schedule auto-disables after first run.
7. `until` cutoff stops future runs.
8. route lock/busy gating defers without duplicate launches.
9. restart semantics: overdue interval triggers once, no backlog burst.
10. unified completion message includes context for normal and sub-agent completions.
11. sub-agent completion includes return-to-parent link when available.
12. completion includes schedule name/trigger/next-run details when schedules exist.
13. no 50-minute idle warning is emitted.
14. route drop deletes schedules.

### Layered Stress (Deterministic Harness)

1. One topic, `2m` interval schedule, continuous user messages; verify cadence and no duplicate runs.
2. Same topic, launch multiple inline forks concurrently; verify last-finishing branch becomes head and completion summary aligns with latest head.
3. Parent topic with running sub-agents + active schedule; verify parent and child completion messages remain correctly scoped.
4. Two topics, each with schedules and concurrent user traffic; verify strict route isolation.
5. Stop-trigger schedule + interval schedule in same route; verify no infinite loop and stable trigger ordering.
6. High-contention lock test: long-running turn overlaps due schedule; verify deferred single execution after unlock.
7. Deletion/clear while schedules pending; verify no orphan schedule executions.

### Live Telegram

1. create interval schedule in topic and observe reliable cadence.
2. create stop-trigger schedule and verify one fire per non-schedule-origin stop.
3. verify no stop-trigger loop after schedule-origin run.
4. verify restart behavior (single catch-up run).
5. verify delete immediately stops future triggers.
6. run concurrent forks/subagents under active schedules and verify completion-message coherence.

## Exit Criteria

Phase 1 is complete when:

1. local deterministic smoke matrix passes,
2. live smoke matrix passes in staging forum chat,
3. 24-hour soak shows no duplicate/self-loop schedule incidents.
