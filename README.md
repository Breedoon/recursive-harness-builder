# Recursive Harness Builder

Recursive Harness Builder is a local runtime for building recursive agent workflows out of markdown prompts, optional hooks, and ordinary project files.

It wraps the Claude Agent SDK with a few added design choices: the ability for agents to clone themselves into a forked subagent, unlimited depth of subagents, agent-to-agent messaging, scheduling, and hookable tool behavior.

## Subscription requirements

The harness itself runs at any subscription tier. However, the recursive workflow example — where agents spawn child agents that spawn further children — benefits from the higher rate limits and concurrent sessions available on **$100+/month plans** (Anthropic Max, OpenAI Pro, or equivalent). Without those limits, deep recursion will hit throttling quickly.

## Why use it

Most agent tools give you one agent in one terminal, with one layer of subagents. Recursive Harness Builder lets you define a harness around the agent:

- **Fork work into child agents** that inherit context and continue independently.
- **Attach hooks** to constrain or extend behavior, such as preventing orchestrators from editing files.
- **Coordinate agents** through inbox messages, artifacts, schedules, and resumable sessions.
- **Use Telegram as a UI** when you want a phone-friendly or multi-window view of many concurrent agents.
- **Route non-Claude models** through a CLIProxyAPI-compatible endpoint when you want Codex, GPT, Gemini, or other providers.

The runtime is the harness. The workflow is yours.

## Example workflow

Start with the bundled recursive workflow example:

- [`examples/recursive-workflow/`](examples/recursive-workflow/) — a small project with flat markdown procedures and a guard hook.
- [`docs/procedures.md`](docs/procedures.md) — how procedure files are organized and launched.
- [`docs/external-control.md`](docs/external-control.md) — `obs-agentctl`: message, launch or start agent trees from bash/cron through the agent tools (local socket, uid + token auth, audit log).

The example shows the intended pattern: describe coordination behavior in markdown, point an agent at the right procedure file, and let the harness manage forks, messages, hooks, and artifacts.

## Quick start

For repository development, use the canonical isolated testing protocol in
[`docs/testing.md`](docs/testing.md). Formal tests must run through the
host-managed `obs-live-test` service, never inside the production-serving
`obs-test` container or by relying on `OBS_PROFILE=test` alone. The authored
command is intentionally parked and unrun in this slice:

```text
python -m scripts.isolated_test_runner --phase host-preflight --host-observation /workspace/runtime/obs-live-test-host-observation.json --compose-file deploy/obs-live-test/compose.yaml --lane unit --scenario focused --dry-run
```

This command performs only host preflight against an independently produced
host observation; it does not create a service. Legacy live launch through
`--test`, `--test-instance`, `--profile test`, or the parallel live-smoke script
fails closed before runtime side effects. The protocol candidate is
**UNVERIFIED / UNRESOLVED** for runtime, container, credential, test, live,
integration, release, and production-readiness claims.

The recommended setup path is to let an agent install it for you.

Open Claude Code, Codex, or another coding agent and give it this prompt:

```text
Clone https://github.com/Breedoon/recursive-harness-builder and follow INSTALL.md to set it up for me. Use examples/recursive-workflow as the first project. Ask me for any credentials or Telegram setup values you need.
```

## Requirements

You will need:

- Python 3.12+
- `uv`
- Claude subscription OAuth for the default Claude path
- Optional: Telegram bot credentials for the Telegram UI
- Optional: CLIProxyAPI-compatible proxy for non-Claude models (eg, Codex)

## Interfaces

Recursive Harness Builder currently includes:

- a local CLI runtime;
- a Telegram runtime with one topic per agent;
- AgentTask-based forking;
- inbox messaging between agents;
- interval and cron-style schedules;
- hook support for tool calls and lifecycle events;
- cache/proxy support for provider routing.

Telegram is the UI. The core model is markdown-defined workflow procedures running on top of resumable, forkable agent sessions.

## Architecture and session inspection

Use `/session` in Telegram or the CLI to inspect the current agent's identity,
lineage, model/effort, one context summary, transcript path, runtime parameters
and configured hooks. The command is read-only, including during active work.

See [Agent and session architecture](docs/agent-session-model.md) for the data
model, ownership/persistence map, child inheritance rules and architecture review.
Related policies: [configuration](docs/configuration.md),
[effort](docs/effort.md), and [context/compaction](docs/context-compaction.md).

## Critical maintainer warning: the cache proxy is easy to break

**Changing the cache proxy, or any code that saves, picks, copies or replays
conversation history, can silently multiply the cost of every agent.** A break
raises no error: the API just stops reusing its prompt cache and re-bills the
whole conversation on every request (one changed system-prompt section re-billed
391k tokens in a single request on 2026-10-12). Read
[docs/cache-proxy-safety.md](docs/cache-proxy-safety.md) before such a change and
pass its checklist after it.

`src/cache_proxy.py` is part of the fork/resume correctness contract. Live CLI
attachments absent from JSONL must be stripped: preserving even useful transient
hook context makes parent and replayed historical prefixes differ. **Do not fix
message delivery by weakening normalization.** Deliver semantic input through
canonical persisted queries instead, as described in
[in-flight message delivery](docs/inflight-message-delivery.md).

Before changing normalization, compare actual bundled CLI live requests and JSONL
with actual fork **and** resume requests. Notification visibility alone does not
establish cache safety, and unit tests (`tests/test_cache_proxy_*.py`) are only a
quick first check: none of the real break mechanisms found in October 2026 was
caught by them. The required check is the live runner:

```text
.venv/bin/python scripts/cache_regression.py     # both routes, all scenarios
```

Follow the full checklist in
**[docs/cache-proxy-safety.md](docs/cache-proxy-safety.md)** (scratch proxy
first, maintenance restart, production run, 1-2 h of real-traffic tally with
`scripts/cache_tally.py`). Every sensitive code site carries a
`CACHE-PREFIX WARNING` comment; `grep -rn "CACHE-PREFIX" src scripts deploy`
lists them.

To diagnose a prompt-cache miss, use the proxy's standing request pool
(`/workspace/runtime/logs/cache-proxy/requests`, capped at 60 GB — about 20 days
of fleet traffic at the 2026-10-12 rate of ~3 GB/day; secrets are redacted in
stored copies, but the billing placeholder is kept as sent, so read stored bodies
only through `scripts/redact.sh`, and never edit the stored files themselves —
they are the raw evidence) and `scripts/prefix_diff.py --session <id>` — see
docs/configuration.md "Request log".

**Working on the proxy safely.** Never load the Claude Code billing placeholder
literal (the key `cch=` followed by five zeros; describe it only by the regex
`cch=0{5}`) into an agent's context: the CLI rewrites its first occurrence in the
request body with a random hash, so a session that read proxy source, logs or
reports containing it stops caching on every later call. Read the proxy source,
request logs and reports through a redacting filter (`scripts/redact.sh`, which
also hides tokens, API keys and session strings; minimal form `sed -E 's/cch=0{5}/cch=<PH>/g'`),
never write the literal in prompts, notes or messages, and abandon a poisoned
session for a fresh one. On 2026-10-08/09 this self-poisoning cost the cache-prefix
mission roughly 30-35 points of the Claude weekly pool (vault-mief). The regression
check is `scripts/cache_regression.py` (it runs the live drivers in
`scripts/prefix_repro.py`), not unit tests, which are not evidence here.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
