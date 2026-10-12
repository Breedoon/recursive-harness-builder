# Notes for agents working on this repository

## The cache proxy is easy to break — read this first

Every agent request goes through `src/cache_proxy.py`. Prompt caching only
works if each request starts with exactly the same bytes as the previous one.
A change to the proxy, or to any code that saves, picks, copies or replays
conversation history (`jsonl_fork`, `jsonl_health`, `jsonl_replay`, session
resume, Telegram recovery/wakes/fork children, queued messages, prompts, hooks,
effort, model config), can break that **silently**: no error, no failing unit
test, just every conversation re-billed in full on every request.

Before committing such a change:

1. Read [docs/cache-proxy-safety.md](docs/cache-proxy-safety.md) and follow its
   checklist.
2. Run the live check on a scratch proxy, then on production after a
   maintenance restart:
   `.venv/bin/python scripts/cache_regression.py` → must print `RESULT: PASS`.
3. Tally 1-2 hours of real traffic with `scripts/cache_tally.py` and explain
   every break with `scripts/prefix_diff.py`.

Sensitive sites carry a `CACHE-PREFIX WARNING` comment
(`grep -rn "CACHE-PREFIX" src scripts deploy`). Unit tests are a quick first
check only; they are not evidence that caching still works.

**Never put the Claude Code billing placeholder** (`cch=` followed by five
zeros; refer to it only by the regex `cch=0{5}`) into your context, files,
prompts or commit messages. A session that has it in context stops caching;
start a fresh one.

**Never print secrets either.** Proxy logs and the daemon's environment also
hold Telegram bot tokens, the userbot session string, API keys and OAuth
tokens. Read source, logs, reports and process environments through the
ready-made filter, which hides both the placeholder and secrets:

```text
<command> | scripts/redact.sh
tr '\0' '\n' < /proc/<pid>/environ | scripts/redact.sh
```

It is best-effort; when you only need variable names, print names only
(`cut -d= -f1`). The stored request log and `scripts/cache_regression.py`
results do not contain credentials by design, but the runner's per-scenario
`.txt` files keep unfiltered harness stderr — filter them before quoting.

## Other

- Testing protocol: [docs/testing.md](docs/testing.md).
- Restarting production: `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart`
  ([docs/maintenance-restart.md](docs/maintenance-restart.md)).
