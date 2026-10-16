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
   2026-10-12: always trim). Lesson: the old code trimmed only when it had
   also removed a reminder, and reminders exist only in the live form. Any
   rewrite that happens only when live-only content is present makes the live
   and replayed forms differ; make every canonicalization unconditional.
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

0. **Do not poison your own session, and do not leak secrets.**
   - Never put the billing placeholder (`cch=` followed by five zeros) into your
     context, a file, a prompt or a commit message. If it shows up unredacted
     in your context, start a fresh session.
   - Proxy work means reading logs and process environments that also hold
     real credentials: Telegram bot tokens, the userbot session string, API
     keys, OAuth tokens. Never print them into your context or copy them
     anywhere.
   - Pipe everything you read through the ready-made filter, which hides both:
     ```text
     <command> | scripts/redact.sh
     tr '\0' '\n' < /proc/<pid>/environ | scripts/redact.sh    # process env
     ```
     It replaces the placeholder, `KEY=value` pairs whose name looks secret
     (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*SESSION*`, `*AUTH*`, `*PASSWORD*`, ...),
     `Bearer` tokens, secret-named JSON fields, known token shapes and long
     opaque strings. It is best-effort: prefer printing only variable *names*
     (`... | cut -d= -f1`) when names are all you need.
   - **Stored request bodies are NOT free of the billing placeholder.** The
     request log redacts secrets, but a stored `.pre`/`.wire` body keeps the
     placeholder exactly as sent whenever the conversation contained it: every
     `cch_collision` test session does (that is what it tests), and so does any
     agent session that read it unfiltered. So `zcat` a stored body, or quote
     `prefix_diff.py` output, only through `scripts/redact.sh`.
   - **Redact what you read, never the log itself.** Never edit, rewrite or
     "clean" files under `/workspace/runtime/logs/cache-proxy/requests`: they are
     the raw evidence `prefix_diff.py` compares, and an edited body shows a
     break that never happened. Redaction happens only on the way to your
     screen. (On 2026-10-12 the 26 stored files of 13 requests of session
     `2cd7c4a3` in `requests/2026-10-12/` were edited after capture; they no
     longer match what was sent, so do not `prefix_diff` them. The request ids
     are listed in bead `vault-1r7n.14`.)
   - What the tools themselves output: apart from the placeholder case above,
     the stored request log keeps secret-redacted copies, so `prefix_diff.py`
     and the runner's diff snippets come from secret-redacted bodies. `cache_regression.py`
     prints and saves request ids, session ids, statuses, cache ratios and diff
     snippets; it never prints the OAuth token the direct-probe scenarios send.
     Its per-scenario `.txt` files also keep the harness's stderr, which is not
     filtered, so run them through `scripts/redact.sh` before quoting them.
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
   rows.) Always give a scratch or test proxy its own `CACHE_PROXY_LOG_DIR`:
   agent shells inherit the production value from the OBS daemon, and on
   2026-10-12 test proxies that did not override it wrote about 320 rows into
   the production `usage.jsonl` and the production `effort-pins.json` (unit
   tests now use `isolated_proxy_env()` in `tests/conftest_cache_proxy.py`).
   Stop scratch proxies by their PID, not with `pkill -f <pattern>` or a
   `pgrep -f` wait loop: the pattern also matches your own shell command, which
   then kills itself or waits forever. To compare with the old code, start the committed version on another
   port: `git show HEAD:src/cache_proxy.py > /workspace/runtime/cache-proxy-scratch/old.py`.
   Required result: `RESULT: PASS` on both routes, and your new scenario FAILs
   on the old code and PASSes on the new.
4. **The gate enforces steps 3-5 (vault-3px5).** `python -m obs_agent.maintenance_restart`,
   Telegram `/restart` and a push to `main` (pre-push hook: `scripts/cache_gate.py install-hook`)
   refuse to go ahead when cache-sensitive files changed since the running code
   (`/workspace/runtime/state/obs-running-code.json`, written by telegram_main at start) and no
   `gate.json` from `scripts/cache_regression.py` shows `PASS` on both routes, full scenario set,
   for exactly this code (HEAD + uncommitted-change hash, the proxy under test having loaded this
   `src/cache_proxy.py`). Sensitive = files with a `CACHE-PREFIX` marker plus `tools.py`,
   `pyproject.toml`, `uv.lock` and the CLIProxyAPI binary/config. Check any time with
   `.venv/bin/python scripts/cache_gate.py check`. Emergency override:
   `--skip-cache-gate "<reason>"`, `/restart skip-cache-gate <reason>` or
   `OBS_CACHE_GATE_SKIP=<reason> git push`; every bypass (and every plain restart that skipped a
   failing gate) is appended to `/workspace/runtime/state/cache-gate-bypass.jsonl`. The kill
   switch and crash/watchdog restarts are never gated.
5. **Commit, push, deploy with the maintenance restart only:**
   `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart`
   (running agents resume by themselves). Then confirm the live proxy loaded
   your code: `curl -s 127.0.0.1:28925/health` → `source_sha256` must equal
   `git show HEAD:src/cache_proxy.py | sha256sum`. A comment-only commit to
   `cache_proxy.py` also changes that hash, so do a maintenance restart after it
   too, or the identity check (and the runner's first line) will report a
   mismatch.
6. **Run the live check on production:** `.venv/bin/python scripts/cache_regression.py`.
   It must say `RESULT: PASS`. Write down the output directory (it keeps every
   request id).
7. **Watch real traffic for at least 1-2 hours.**
   ```text
   .venv/bin/python scripts/cache_tally.py --since <deploy time UTC> \
       --exclude-sessions <runner out dir>/harness_sessions.txt --breaks-out breaks.jsonl
   ```
   Compare with the same window before the change, separately for Claude
   ("anthropic") and GPT ("cli-proxy"). Explain every break in `breaks.jsonl`
   with `prefix_diff.py`. Light traffic proves little — say so rather than
   claiming an improvement.
8. **Record it**: commit message or note with the runner output dir, the request
   ids of any FAIL you fixed, and the tally numbers.

**Rollback:** `git revert <commit>`, push, maintenance restart, re-run step 6.

### How to read the runner's result

- `PASS` — every request pair was a pure append with at least 95% cache read.
- `FAIL` — a cache regression (or a scenario crashed without any provider
  error). The line shows the bad pairs; run `prefix_diff.py` on them.
  Two known FAIL shapes are not caused by a proxy change and are usually
  intermittent, so rerun that one scenario before concluding:
  - class `append` on the GPT route with ratio 0, or with ratio exactly 0.835
    (the one observed partial read, an exact point and not a band): our bytes
    were a perfect prefix but the provider read nothing, or less than the full
    prefix, from cache. Ratio 0 was
    seen 2026-10-12 (`codex boundary_marker`), 2026-10-15 (`codex cch_collision`)
    and 2026-10-16 (`codex reminder_block`, `prefix_diff.py`: pure append). The
    only partial read so far is 0.835 on 2026-10-15 (`codex boundary_marker`,
    cached 15104 of 18089 tokens). Its request bodies were not kept, so
    "provider-side" there is the runner's own `append` classification, not a
    re-run diff, and the case is under investigation (bead `ops-1.11.2`). Each
    passed on rerun. Every other partial ratio is NOT accepted: a byte-intact
    but intermittent cache-key regression on our side (`prompt_cache_key`,
    `cache_proxy.py`) would look exactly like a partial GPT append and could pass
    after one lucky rerun, so no band is accepted without evidence. The same class on the Claude route is not this
    shape: there a short read can mean a misplaced cache breakpoint, which is
    ours.
  - class `tail-block-append` with ratio 0.85 to 0.95 in `recovery_multi` on the
    Claude route: the open "Continue from where you left off." mechanism (bead
    `vault-1r7n.9`); it passes on some runs and fails on others. Seen 0.8721,
    0.9216 and 0.944.
  Any other class (`history-changed`, `system-changed`, `config-changed`,
  `tools-changed`, missing breakpoint), any other ratio, a different
  scenario for the second shape, or a FAIL with no recorded bad pairs is a
  real regression until proven otherwise.

  Known-shape table: the cache gate (`src/obs_agent/cache_gate.py`,
  `KNOWN_INTERMITTENT_SHAPES`, ratio bounds inclusive) accepts a full run with a
  FAIL only when every bad pair of that FAIL matches one of these rows exactly
  AND the same scenario passed on a later rerun of the same code:
  - GPT route, any scenario, class `append`, ratio 0.0 to 0.0 (exactly 0).
  - GPT route, any scenario, class `append`, ratio 0.835 to 0.835 (exactly 0.835).
  - Claude route, scenario `recovery_multi`, class `tail-block-append`,
    ratio 0.85 to 0.95.
  `PROVIDER-ERROR` and `SKIPPED` scenarios (no cache verdict) are also
  accepted once they pass on rerun. The report's top-level `providers` must
  list both routes as actually run. Changing a row needs evidence here and the
  same change in the gate; `tests/test_cache_gate.py` checks they agree.
  History: OBS `28cfbe3` (2026-10-15) first let the gate accept any rerun-passed
  FAIL; it accepted the 0.835 append above although this list then documented
  only ratio 0. OBS `5f94fbc` (2026-10-16) narrowed acceptance to a shape table
  but widened the GPT row to "below 0.95" on that single observation; the branch
  D verifier judged that a relaxation beyond the evidence, and the row was
  narrowed to ratio 0 plus the single observed point 0.835 (vault-3px5).
- `PROVIDER-ERROR` — the scenario failed or timed out while the upstream API
  returned 5xx errors. Usually provider overload, not our code. It is not a
  pass: rerun that route later.
- `SKIPPED` — after two provider errors in a row on one route the runner stops
  that route instead of wasting time (`--max-provider-errors`). Tested on
  2026-10-12 against a scratch proxy whose GPT upstream was a stub that always
  returned 502: two scenarios came back `PROVIDER-ERROR`, the next two
  `SKIPPED`, and the runner exited with 2.
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
- **The wake test with a long gap** (`scripts/prefix_repro.py resume_new_proc
  --gap 330`, not part of the default runner because it takes 6 minutes) needs
  1-hour cache entries. The CLI writes them only when its feature-flag service
  (GrowthBook) is on, because the 1-hour setting is a server flag
  (`tengu_prompt_cache_1h_config`); `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  turns that service off and leaves only 5-minute entries. Since commit
  86a1672 the wake scenario turns it back on; check that the first requests'
  usage shows `ephemeral_1h_input_tokens > 0`. If only 5-minute entries were
  written, a zero read after the gap is expiry, not a prefix break. The same
  applies to the fleet: never set that variable for production agents.
- **Every request is logged** with what the proxy did to it (`norm.*` fields in
  the request index: `ws_trimmed`, `volatile_pin`, `cache_key`,
  `dynamic_boundary`, plus `proxy_source`, the hash of the loaded code). The
  request log is capped at 60 GB, about 20 days of fleet traffic at the
  2026-10-12 rate of about 3 GB a day.

## Accepted side effects of the 2026-10-12 design

These are deliberate tradeoffs. Re-decide them if they start to hurt.

- **Two conversations with the same first message are treated as one.** The
  variant pin (`pin_volatile_config`) and the GPT cache key
  (`assign_conversation_cache_key`) are keyed on the model plus the first
  message. This is what lets resumes, wakes, restarts and forks share them.
  The cost: separate conversations that start with an identical first
  message share one pin and one GPT key. Examples are a recurring scheduled
  prompt or identical helper prompts.
  - Sharing a GPT key is harmless: they would share a cache prefix anyway.
  - Sharing a pin means a recurring prompt keeps the system-prompt variant it
    was first seen with for as long as it keeps running. The pin expires only
    after 2 days without use, so a prompt that runs every day may never pick
    up a newer variant.
  - Accepted because the pinned section ("# Output efficiency") only changes
    the model's answer style, while a flip re-bills the whole conversation.
- **A newly appearing `context_management` setting is removed for pinned
  conversations.** If the server starts sending it (a "clear old thinking"
  edit) in the middle of a conversation that began without it, the proxy
  removes it for the rest of that conversation. Old thinking blocks are then
  kept rather than cleared, so long conversations may grow a little faster
  toward compaction. If it disappears, it is restored, but only when thinking
  is enabled.
- **Only one system section is pinned.** That is `"# Output efficiency"`, the
  only one seen flipping. Any other flag-driven section that starts flipping
  is not covered until it is added to `VOLATILE_SYSTEM_SECTIONS`.
- **Why these sections flip is a hypothesis.** It comes from reading the
  bundled CLI, not from observing the mechanism end to end. The guess: a
  server feature flag (`tengu_swann_brevity`) is cached in the fleet's shared
  `~/.claude.json` and refreshed every few hours, so one process's refresh
  changes it for every running agent. The pin works whatever the cause.

## More detail

- The vault write-up of the 2026-10-12 investigation and the current
  architecture: `Projects/Personal Projects/OBS Platform/Cache Prefix Stability/Caching Long-Term Fix.md`
  in the OBS vault.
- `docs/configuration.md` "Request log"; `docs/inflight-message-delivery.md` (why
  live-only hook context is stripped rather than kept).
