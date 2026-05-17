# Live Test Suite Organization

This document is the working map for OBS live Telegram tests. It complements the general testing policy in `CLAUDE.md` and `docs/testing-philosophy.md`.

## Goals

- Keep a small, frequent live smoke surface that catches core user-journey regressions.
- Keep focused live tests available for diagnosis without making every fix run the full suite.
- Keep special-condition tests separate when they require unusual resources, daemon restarts, wall-clock waits, provider/model settings, cache behavior, multiple bots, provisioning, or soak time.
- Provide a practical parallel live smoke path that runs separate pytest tests with isolated Telegram resources.

## Markers

Use these markers in addition to `integration` and `telegram`:

- `telegram_core_smoke` — frequent core smoke tests. These should cover multiple user-visible journeys and be safe to run often.
- `telegram_focused` — focused live diagnostics for one feature or regression.
- `telegram_special` — live tests that need special resources or setup.
- `telegram_parallel` — tests or orchestrators intended to participate in isolated parallel execution.
- `telegram_soak` — long soak tests.
- `serial` — tests that must run alone because they stop/restart services, use wall-clock scheduling, or mutate shared external state.

The legacy `telegram_smoke` marker remains for backward compatibility. Do not assume every `telegram_smoke` test is frequent core smoke; several historical smoke tests are provider-specific, restart-heavy, schedule-heavy, or stress-oriented and should be treated as special/focused unless also marked `telegram_core_smoke`.

## Frequent core smoke candidates

Prefer a small set of concentrated runs that cover multiple user journeys:

- route/session isolation and basic replies;
- reply handling and inline/head forking;
- `/fork` child topic/session behavior;
- `/stop` responsiveness and post-stop continuation;
- basic AgentTask/fork/fresh launch;
- parent-child messaging and inbox wakeups;
- completed/idle agent wakeups;
- team or lineage messaging round trips.

Good existing examples to include in core smoke or parallel smoke batches:

- `tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_multi_chat_concurrent_isolation`
- `tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_fork_command_creates_child_topic_and_keeps_parent_session`
- `tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_inline_reply_fork_stays_in_same_topic_and_plain_followup_uses_it`
- `tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_stop_interrupts_and_topic_stays_responsive`
- `tests/test_telegram_live_stress.py::TestIdleWakeWithoutSleep::test_live_idle_agent_wakes_on_direct_message_without_sleep`
- `tests/test_telegram_live_stress.py::TestCompletedAgentMessageable::test_live_message_to_completed_agent_succeeds`

## Focused live tests

Focused tests diagnose one feature or regression. Run them when touching that subsystem or when a smoke test points to that subsystem:

- media upload/download/transcription;
- detailed ForkTask handle/stop/output/resume behavior;
- tree rendering and `search_team` output details;
- race regressions;
- topic deletion edge cases;
- must-reply backend behavior and wake exhaustion;
- deep lineage routing stress;
- exact naming/hash-prefix formatting.

## Special-condition tests

Keep these separate from frequent smoke and from default parallel batches unless the test explicitly provides isolated resources for the condition:

- daemon restart and persistence;
- schedule/cron/wall-clock behavior;
- one-hour soak tests;
- provisioning tests that create bots or groups;
- multi-bot sender-pool tests;
- cache proxy behavior;
- non-Claude/Gemini/provider/model inheritance;
- thinking/signature breakage tests;
- CLI proxy down or external outage tests.

These may need serial runs, separate daemon instances, multiple bots, or explicit operator setup.

## Basic parallel live smoke path

Use `scripts/run_parallel_live_smoke.py` for the default proof path. It launches separate pytest subprocesses concurrently and forces safe isolated live settings:

```bash
OBS_TEST_TELEGRAM_BOT_USERNAMES=botA,botB \
OBS_TEST_TELEGRAM_BOT_TOKENS=tokenA,tokenB \
python scripts/run_parallel_live_smoke.py \
  --output-dir /tmp/obs-live-parallel-smoke \
  --max-workers 2
```

Default selectors:

- `tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_multi_chat_concurrent_isolation`
- `tests/test_telegram_live_stress.py::TestIdleWakeWithoutSleep::test_live_idle_agent_wakes_on_direct_message_without_sleep`

Proof runs should use one bot token per worker. The runner refuses a shared bot by default because separate polling processes can collide on the Bot API. `--allow-shared-bot` is diagnostic only and should not be used as proof of safe parallel suite execution.

The runner writes `parallel-live-summary.json` containing worker commands, timing overlap, bot identity, logs, and discovered `live-forum-resources.json` metadata. A successful live proof must show:

- at least two workers overlapped in wall-clock time;
- all worker subprocesses returned zero;
- each worker used isolated resources;
- chats/state/vault paths are distinct;
- no production daemon was killed or restarted.

## Runtime-fix rule

When fixing runtime behavior, do both:

1. Add or run the bespoke reproducer that proves the bug before the fix.
2. Run the relevant existing live smoke or focused live tests for the affected subsystem.

Future runtime fixes must include both relevant existing live tests and bespoke reproducers in their verification plan.

Examples:

- `/stop` or interrupt changes: run the bespoke reproducer and `test_live_stop_interrupts_and_topic_stays_responsive`.
- Forking changes: run the bespoke reproducer and the relevant `/fork` or inline-fork live tests.
- Inbox/wakeup changes: run the bespoke reproducer and the idle/completed-agent wake tests.
- Schedule changes: run the schedule-focused live tests, but keep restart/wall-clock/soak cases in special serial lanes.

Do not claim a runtime fix is verified from a bespoke test alone when an existing live test covers the same boundary.
