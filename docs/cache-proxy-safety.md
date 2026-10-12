# Cache proxy safety: why it breaks and the checklist for changing it

**Read this before you change the cache proxy or any code that builds or replays
conversation history.** It is easy to break, and when it breaks nothing fails:
the agents keep working while the bill quietly multiplies.

## Why it is fragile (two-minute version)

A few terms, used throughout:

- **Prefix.** Every API request is read in a fixed order: tool list, then system
  prompt, then the conversation messages. The *prefix* is everything that an
  earlier request and a later request have in common from the very start.
- **Cache read.** The API keeps recent prefixes for a while (5 minutes for Haiku,
  1 hour for the fleet's main models). A later request that starts with exactly
  the same bytes is charged a small fraction for that part. That cheap part is
  the *cache read*.
- **Breakpoint.** A `cache_control` marker in the request that tells the API where
  to save a prefix. A request with no breakpoint in its messages saves nothing new.
- **Re-billed / a break.** If even one byte of earlier history, the system prompt,
  the tool list or a request setting differs from the previous request, the cache
  read stops at that byte and everything after it is paid again at full price.
  We call that a *break*. A long agent conversation is 100k-400k tokens, so one
  break costs as much as dozens of normal turns.

The cache proxy (`src/cache_proxy.py`, port 28925 in production) sits between
every agent and the API. It rewrites requests so that the same conversation
produces the same bytes whether it is running live, resumed after a wake or a
restart, or forked into a child agent. Those three paths build the request in
different ways (live state vs. the session's saved JSONL file), so the proxy's
rewrites are what keeps them identical. Any change to those rewrites, or to the
code that saves, picks or copies JSONL history, can silently break that.

**Unit tests are not enough.** Between 2026-10-08 and 2026-10-12 we found these
real break mechanisms in live traffic; none was caught by a unit test:

1. A reminder-only text block that carried the only message breakpoint was
   dropped, so requests saved nothing (fixed: the breakpoint now moves).
2. The CLI's billing placeholder (pattern `cch=0{5}`) inside a message got a
   new random value every request (fixed: `restore_cch_placeholder`).
3. A boundary marker line the CLI sometimes inserts into the system prompt
   (fixed: removed by `normalize_dynamic_boundary`).
4. Recoveries forked from the middle of a message (fixed in `jsonl_health`).
5. Parallel tool results lost when forking (fixed in `jsonl_fork`).
6. On resume, a tool result with leading spaces differed from the live form,
   because the CLI trims it live but the JSONL keeps the spaces (fixed
   2026-10-12: always trim).
7. A server-side feature refresh flips one system-prompt section and the
   `context_management` setting in the middle of conversations (fixed
   2026-10-12: kept as first seen per conversation, `pin_volatile_config`).
8. On the GPT route our own metadata rewrite gave the whole fleet ONE provider
   cache key (fixed 2026-10-12: one key per conversation,
   `assign_conversation_cache_key`).

Still open: a "Continue from where you left off." block the CLI adds at the
end of some recovered conversations costs a little cache on Claude and caused
some large GPT fork misses (bead `vault-1r7n.9`).

## When this checklist applies

Any change to one of these (each carries a `CACHE-PREFIX WARNING` comment;
`grep -rn "CACHE-PREFIX" src scripts deploy` lists them all):

- `src/cache_proxy.py` — anything in the request pipeline: the normalizers in
  `normalize_request`, `pin_volatile_config`, `assign_conversation_cache_key`,
  `pin_effort`, `restore_cch_placeholder`, tool-schema edits, the final
  re-serialization in the request handler.
- `src/obs_agent/jsonl_fork.py`, `jsonl_health.py`, `jsonl_replay.py` — how
  forks and recoveries pick and copy history.
- `src/obs_agent/session.py` (resume/reconnect, poisoned-session recovery),
  `telegram.py` (route recovery, wakes, AgentTask fork children),
  `runner.py` (queued messages), `prompt.py`, `hooks.py`, `lineage.py`,
  `effort.py`, `config.py`, `runtime_env.py`, `fork.py`.
- Upgrading the bundled Claude Code CLI (mechanisms 3 and 7 came from the CLI).
- Upgrading or reconfiguring CLIProxyAPI (`/workspace/runtime/cliproxy`).
- `deploy/obs-telegram-prod-wrapper.sh` and `cache_proxy_lifecycle.py` (how the
  proxy is started and restarted).

## The checklist

0. **Do not poison your own session.** Never put the billing placeholder
   (`cch=` followed by five zeros) into your context, a file, a prompt or a
   commit message. Read proxy source, logs and reports through
   `sed -E 's/cch=0{5}/cch=<PH>/g'`. If it shows up unredacted in your context,
   start a fresh session.
1. **Know the mechanism from real bytes.** If you are fixing a miss, find it in
   the request log (`/workspace/runtime/logs/cache-proxy/requests`) and run
   `.venv/bin/python scripts/prefix_diff.py <earlier req id> <later req id>`
   (or `--prev-any <req id>` for forks and recoveries). It prints the first
   byte where the two requests stop matching. Fix that, not a guess.
2. **Make a live scenario fail first.** Add or extend a scenario in
   `scripts/prefix_repro.py` that reproduces the mechanism and FAILS on the
   current code.
3. **Test on a scratch proxy, never first on production.** Start one from your
   working tree on a spare port with its own log dir:
   ```text
   PYTHONPATH=/workspace/obs/src CACHE_PROXY_LOG_DIR=/workspace/runtime/cache-proxy-scratch/new \
   CACHE_PROXY_REQUEST_LOG=1 .venv/bin/python src/cache_proxy.py 28942
   .venv/bin/python scripts/cache_regression.py --base-url http://127.0.0.1:28942 \
       --log-dir /workspace/runtime/cache-proxy-scratch/new/requests
   ```
   (`--log-dir` must be the `requests` subdirectory, or the runner finds no
   rows.) To compare with the old code, start the committed version on another
   port: `git show HEAD:src/cache_proxy.py > /workspace/runtime/cache-proxy-scratch/old.py`.
   Required result: `RESULT: PASS` on both routes, and your new scenario FAILs
   on the old code and PASSes on the new.
4. **Commit, push, deploy with the maintenance restart only:**
   `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart`
   (running agents resume by themselves). Then confirm the live proxy loaded
   your code: `curl -s 127.0.0.1:28925/health` → `source_sha256` must equal
   `git show HEAD:src/cache_proxy.py | sha256sum`.
5. **Run the live check on production:** `.venv/bin/python scripts/cache_regression.py`.
   It must say `RESULT: PASS`. Write down the output directory (it keeps every
   request id).
6. **Watch real traffic for at least 1-2 hours.**
   ```text
   .venv/bin/python scripts/cache_tally.py --since <deploy time UTC> \
       --exclude-sessions <runner out dir>/harness_sessions.txt --breaks-out breaks.jsonl
   ```
   Compare with the same window before the change, separately for Claude
   ("anthropic") and GPT ("cli-proxy"). Explain every break in `breaks.jsonl`
   with `prefix_diff.py`. Light traffic proves little — say so rather than
   claiming an improvement.
7. **Record it**: commit message or note with the runner output dir, the request
   ids of any FAIL you fixed, and the tally numbers.

**Rollback:** `git revert <commit>`, push, maintenance restart, re-run step 5.

### How to read the runner's result

- `PASS` — every request pair was a pure append with at least 95% cache read.
- `FAIL` — a cache regression (or a scenario crashed without any provider
  error). The line shows the bad pairs; run `prefix_diff.py` on them.
- `PROVIDER-ERROR` — the scenario failed or timed out while the upstream API
  returned 5xx errors. Usually provider overload, not our code. It is not a
  pass: rerun that route later.
- `SKIPPED` — after two provider errors in a row on one route the runner stops
  that route instead of wasting time (`--max-provider-errors`).
- Exit code 0 = all PASS, 1 = some FAIL, 2 = inconclusive (provider errors or
  skips, no FAIL).

## Special cases

- **After any CLIProxyAPI upgrade or config change** run
  `.venv/bin/python scripts/cliproxy_cache_key_probe.py`. It checks that each
  conversation still gets its own GPT cache key (mechanism 8). Then run
  `scripts/cache_regression.py --providers codex`.
- **A new system-prompt section starts flipping mid-conversation** (look for
  `system-changed` breaks in the tally plus `prefix_diff.py`): add its heading to
  `VOLATILE_SYSTEM_SECTIONS` in `src/cache_proxy.py` and extend the
  `volatile_flip` scenario.
- **Claude Code CLI upgrade:** run the full checklist from step 3; CLI changes
  caused two of the mechanisms above.
- **The wake test with a long gap** (`resume_new_proc --gap 330`) cannot pass on
  Haiku, whose cache entries last 5 minutes; use a 1-hour model
  (`--model claude-opus-5-5`) or judge the pair class (append) instead of the
  ratio.
- **Every request is logged** with what the proxy did to it (`norm.*` fields in
  the request index: `ws_trimmed`, `volatile_pin`, `cache_key`,
  `dynamic_boundary`, plus `proxy_source`, the hash of the loaded code). The
  request log is capped at 60 GB, about 20 days of fleet traffic at the
  2026-10-12 rate of about 3 GB a day.

## More detail

- The vault write-up of the 2026-10-12 investigation and the current
  architecture: `Projects/Personal Projects/OBS Platform/Cache Prefix Stability/Caching Long-Term Fix.md`
  in the OBS vault.
- `docs/configuration.md` "Request log"; `docs/inflight-message-delivery.md` (why
  live-only hook context is stripped rather than kept).
