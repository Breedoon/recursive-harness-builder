# Live test suite

The Telegram live suite is split so release verification can run only the relevant existing live tests and add bespoke reproducers for a bug when needed.

- `telegram_core_smoke` covers broad Telegram startup and routing smoke checks.
- `telegram_focused` covers targeted feature and regression checks.
- `telegram_special` covers stress, schedule, and multi-bot scenarios.

Use `scripts/run_parallel_live_smoke.py` when live evidence requires concurrent isolated bot workers. The runner defaults include relevant existing live tests such as `test_live_multi_chat_concurrent_isolation` and `test_live_idle_agent_wakes_on_direct_message_without_sleep`; pass focused pytest node ids for bespoke reproducers when a blocker needs narrower proof.
