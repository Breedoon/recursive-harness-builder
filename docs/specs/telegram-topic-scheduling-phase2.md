# Telegram Topic Scheduling Phase 2 Specification

**Status**: Approved for implementation  
**Date**: 2026-03-10

## Objectives

1. Keep scheduling minimal but reliable for Telegram topic routes.
2. Support both interval and real cron wall-clock schedules.
3. Keep hybrid reliability: persisted scheduler + hook-based re-anchoring + execution guards.
4. Keep scheduling state server-side (topic-bound), while allowing repo-level defaults in `settings.json`.
5. Normalize completion summaries so only one upcoming schedule line is shown.

## Explicit Scope

1. Add `schedule_mode`: `interval | cron`.
2. Keep `interval_seconds=0` as valid `on_topic_stop` semantics.
3. Replace agent-facing `run_mode` string with `reset_session: bool`.
4. Add inheritance policy: `inherit = none | fork | all`.
5. Add optional schedule window start using `from` (RFC3339), paired with `until` using `[from, until)` logic.
6. Keep optional `max_runs` cap; schedule stops when either `max_runs` or `until` condition is met.
7. Add `/unschedule` command for topic-local schedule removal.
8. `/clear` keeps schedule; response must explicitly mention persistence + `/unschedule`.
9. Fully deprecate legacy 50-minute idle reminder path.
10. Keep one "next upcoming" line in completion summary (never a list).
11. Emit `schedule_triggered: ...` as a system marker at schedule-start time (before `working`), not in completion summary.
12. Render schedule timestamps as absolute server-local time (`today` / `tomorrow` aliases, otherwise full date), with seconds only for second-level interval schedules.

## Out of Scope

1. Multi-day soak (>1 hour) in this phase.
2. Airflow-style catch-up replay for offline time.
3. Multiple schedule notifications in completion summary.
4. UI-level schedule dashboard.

## Runtime Model

### Canonical Hybrid Scheduler

1. Poller remains authoritative for due execution.
2. Stop-hook updates interval schedules to `next_run_at = stop_time + interval_seconds`.
3. Stop-hook does not trigger schedule execution for routes currently marked `schedule_run_active`.
4. Start guard is introduced (internal execution-active guard) so no schedule fires during active turn.
5. Persisted state remains source of truth across restarts.

### Reliability Guardrails

1. Per-route lock and `busy` gate block concurrent schedule launches.
2. `schedule_run_active` gate prevents self-trigger loops.
3. Stop-event suppression window prevents immediate recursive triggers after a schedule run.
4. Stop events received while execution is active are deferred (not dropped) and replayed once idle.
5. Deferred stop events are bounded by max defer count to avoid infinite replay loops.
6. Idempotent due-execution path: no duplicate run for same due slot.

## Schedule Semantics

### Mode: `interval`

- `interval_seconds > 0`: periodic by inactivity cadence (re-anchored on stop).
- `interval_seconds = 0`: `on_topic_stop` trigger.

### Mode: `cron`

- `cron` is standard 5-field expression (`min hour dom mon dow`).
- Wall-clock semantics (e.g. `*/3 * * * *` means minutes divisible by 3).
- No catch-up replay while offline; on resume run only for currently due slot and continue.

### Start/End Window

- `from` optional RFC3339 start timestamp.
- `until` optional RFC3339 end timestamp.
- Active interval is `[from, until)`.
- `from == until` is valid and yields a zero-length window.

### End Conditions

- Schedule disables when **either**:
  - `run_count >= max_runs` (if set), or
  - `now >= until` (if set).

## Topic Cardinality and Overlap

1. Multiple schedules may exist for one topic only when windows do not overlap.
2. Overlap is rejected on create/update.
3. Touching windows are allowed (`end == next_start`).
4. Completion summary shows only nearest upcoming schedule across active schedules.

## Inheritance

`inherit` behavior at child topic creation:

1. `none`: no inheritance.
2. `fork`: only fork-created topics inherit (`/fork`, `ForkTask` path).
3. `all`: any child topic route creation inherits.

Inheritance copies config template, but child receives independent schedule record and counters.

## MCP Tool Contract

### `CronCreate`

Required:

- `prompt`
- `schedule_mode`: `interval | cron`

By mode:

- `interval`: `interval_seconds` required (`>=0`, where `0` => on-stop)
- `cron`: `cron` required (5-field)

Optional:

- `description`
- `reset_session` (bool, default `false`)
- `max_runs` (positive int, default `1`)
- `from` (RFC3339)
- `until` (RFC3339)
- `inherit`: `none | fork | all` (default `none`)

Backward compatibility accepted during phase 2:

- `run_mode` input tolerated and mapped to `reset_session`.

### `CronList`

- Lists schedules for current topic route.
- Includes `next_run_at`, `enabled`, `run_count`, `max_runs`, `from`, `until`, `inherit`, `schedule_mode`.

### `CronDelete`

- Deletes one schedule by id in current route.

### `/unschedule`

- Topic command.
- Default behavior: delete all schedules attached to current topic route.
- Returns concise confirmation with count deleted.

## `settings.json` Model

Use `obs` namespace.

```json
{
  "obs": {
    "scheduling": {
      "defaults": {
        "auto_create_on_session_start": false,
        "schedule": {
          "schedule_mode": "interval",
          "interval_seconds": 3000,
          "prompt": "Save current session state.",
          "reset_session": false,
          "max_runs": 1,
          "inherit": "none"
        }
      },
      "retry": {
        "max_attempts": 0,
        "delay_seconds": 30
      }
    }
  }
}
```

Compatibility:

1. Read legacy `obs_agent.schedule_defaults` when `obs.scheduling` absent.
2. Emit deprecation warning in logs.
3. Retry policy is configuration-only (not agent-exposed).

## Completion Message Contract

Always send completion summary with:

1. `context: ...`
2. optional `subtask: ...`
3. optional `return_to_parent: ...`
4. optional single `next_schedule: ...` (nearest only)

No multiple-schedule list in completion summary.

Schedule-trigger visibility:

1. When a schedule actually starts executing, emit one system marker:
   - `schedule_triggered: <name> (<trigger>; remaining=...; from=...; until=...)`
2. This marker must appear before the turn `working` marker for the scheduled run.

## Error and Deletion Behavior

1. If topic route is dropped/deleted, attached schedules are removed.
2. If chat/topic no longer exists, schedule execution should fail gracefully and disable according to policy.
3. Failures should be observable in system message; no silent repeated loops.

## Test Matrix

### A. Unit / Deterministic

1. Tool validation: mode-specific required params and legacy compatibility mapping.
2. RFC3339 parsing for `from`/`until`, including `from == until`.
3. End conditions with OR semantics (`max_runs` vs `until`).
4. Overlap detection: reject overlap, allow touching boundaries.
5. Inheritance copy rules for `none/fork/all`.
6. Completion summary includes only nearest upcoming schedule.
7. Stop-hook re-anchor updates interval `next_run_at`.
8. `schedule_run_active` gate blocks accidental trigger while executing.
9. `/clear` text includes schedule persistence note and `/unschedule` hint.
10. `/unschedule` removes only current route schedules.

### B. Integration / Concurrency Stress

1. Multi-topic concurrent schedules with mixed interval and cron.
2. Busy-route contention where due schedule collides with long running turn.
3. Fork storm: parent + multiple forks + nested fork, mixed inheritance modes.
4. Stop-event duplication/drop simulation (chaos): no double-run, no deadlock.
5. Restart chaos: restart before due/at due/mid-run/after completion.
6. Transport transient failures + retries: no schedule corruption.

### C. Live Telegram Smoke (End-to-End)

1. Interval schedule runs at expected cadence with ± few-second jitter.
2. On-stop schedule fires once per stop and does not self-loop.
3. Cron schedule fires on wall-clock boundary (`*/N`).
4. `/clear` retains schedule and informs user.
5. `/unschedule` clears schedule and stops further runs.
6. Two active topics under load remain isolated.

### D. Soak (Phase 2 Timebox)

1. Required: one 1-hour soak with random user chatter, occasional forks, and at least one induced restart.
2. Optional extension: a second 1-hour soak (for example interval-only or mixed mode) when additional burn-in time is available.

Success criteria:

1. No duplicate schedule execution for same due slot.
2. No missed execution beyond tolerance when bot is available.
3. No cross-topic schedule bleed.
4. No infinite trigger loops.

## Migration Notes

1. Keep DB columns backward compatible where possible.
2. Add new fields with defaults and migration-safe reads.
3. Preserve existing schedule records; infer missing new fields sensibly.

## Phase 2 Completion Criteria

1. Spec-compliant API and runtime behavior merged.
2. Full unit/integration/live matrix green (except intentionally deferred >1h soaks).
3. Legacy idle reminder path fully disabled.
4. Documentation updated for new parameters and commands.

## Validation Log (2026-03-11)

1. `uv run pytest -q tests/test_tools.py tests/test_hooks.py tests/test_telegram_state_store.py tests/test_telegram.py` -> `208 passed`
2. `uv run pytest -q tests/test_telegram_live_schedule.py -m "integration and telegram_smoke"` -> `5 passed` (includes interval/on-stop/cron/clear+unschedule/concurrency)
3. `uv run pytest -q tests/test_telegram_live_forum_topics.py -k "test_live_stop_pauses_auto_resume_until_new_message"` -> `1 passed`
