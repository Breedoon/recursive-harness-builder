# Reasoning effort

Effort is separate from model identity and context size. Changing it never adds
an effort suffix to the model ID or changes the context/compaction budget.

## Commands

Telegram and the `obs-agent` CLI both accept:

```text
/effort          # show this session's selected effort
/effort low
/effort medium
/effort high
/effort xhigh
/effort max
/effort auto     # return to this model's configured default
```

Changes are session/topic-local. They apply on the next turn, reconnecting the
SDK lazily while retaining the conversation ID and normal resume/cache-window
policy. Active or queued work must finish first (or use `/stop`). Telegram saves
the selection in its state database, restores it after restart and JSONL recovery,
and shows it alongside `/context` and model selection. `/clear` and `/new` reset
it along with the session's model override. CLI settings last for the daemon
session. The daemon also exposes `GET /effort` and `POST /effort` with
`{"effort":"xhigh"}`; a busy session returns 409, invalid input returns 422.

The displayed level is the harness selection, not a claim that every provider
supports it. Unsupported levels/models remain subject to Claude Code and the
upstream provider's validation or capability clamping.

## AgentTask

```json
{
  "display_name": "Review",
  "prompt": "Review the implementation and tests.",
  "fork": false,
  "model": "gpt[200k]",
  "effort": "xhigh"
}
```

`effort` also works with `fork: true`, without changing the parent's model or
context. Omitting it inherits the parent's selected effort when the model is
inherited. An explicitly selected model uses its own defaults instead; specify
`"effort":"inherit"` to carry the parent's effort across model changes, or
`"effort":"auto"` to force the selected model's configured default. An explicit
`effort` takes precedence over `env.CLAUDE_CODE_EFFORT_LEVEL`. An env-only effort
selection is also saved as child metadata so it survives Telegram restarts.

The existing `resume` parameter retains the child's selection. Combining it with
an effort change returns an error rather than silently ignoring the change; use
`/effort` in that child's topic before resuming it.

## Defaults and precedence

`MODEL_EFFORT_LEVELS` lives beside `MODEL_CONTEXT_WINDOWS` in
`src/obs_agent/config.py`. Built-in OBS defaults are `medium` for the configured
GPT-5.4/5.5/5.6 models; `high` for the configured modern Claude models, except
Opus 4.7 (`xhigh`). Other models, including Haiku/local models, use provider
`auto`. Aliases and context suffixes resolve before the default lookup.

Override them without editing Python:

```sh
OBS_MODEL_EFFORT_LEVELS='{"gpt":"xhigh","sonnet":"low"}'
# Optional shared default, overridden by per-session selections:
OBS_EFFORT_LEVEL=high
```

Precedence is: session `/effort` or AgentTask selection, per-session Claude env,
`OBS_EFFORT_LEVEL`, process `CLAUDE_CODE_EFFORT_LEVEL`, then per-model defaults.
An explicit `auto` skips lower-priority global/environment overrides and selects
the model default. A model whose default is itself `auto` retains provider
behavior. No process-global environment or shared OBSConfig object is mutated.

## Claude Code and CLIProxyAPI

OBS supplies `CLAUDE_CODE_EFFORT_LEVEL` in both the SDK subprocess environment and
its explicit settings env, alongside the existing context controls. For hosted
non-Claude models with a concrete selection, OBS additionally merges the
following into `CLAUDE_CODE_EXTRA_BODY`:

```json
{"thinking":{"type":"adaptive"},"output_config":{"effort":"xhigh"}}
```

This is intentional: native Claude Code effort capability detection may not
recognize GPT/custom model IDs. CLIProxyAPI's Claude request decoder reads
`output_config.effort` with adaptive thinking and its provider pipeline translates
the selection for OpenAI. `xhigh` and `max` are **not** treated as synonyms or
unconditionally downgraded; current OpenAI models can distinguish them. Installed
proxy versions and model registries may support only a subset of the levels.

Unrelated fields in the process/session extra-body JSON are retained, including
metadata, output format, and thinking display. Fixed `budget_tokens` is removed
when switching to adaptive thinking. Invalid JSON/objects fail before a new child
topic is created. Existing temperature/disabled-thinking controls take priority:
OBS does not re-enable thinking when the raw body disables it, specifies
`temperature`, or the environment disables thinking/adaptive thinking. In that
case the selected effort is still recorded, but no adaptive proxy bridge is
injected. Do not combine these overrides when expecting adaptive effort to take
effect. Arbitrary raw extra-body configuration with provider `auto` is left alone.

Compatibility sources checked on 2026-09-14:

- [Claude Code model configuration](https://code.claude.com/docs/en/model-config#adjust-effort-level)
- [OpenAI reasoning effort](https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort)
- [CLIProxyAPI Claude thinking extraction](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/thinking/apply.go)
- [CLIProxyAPI level conversion and provider capability handling](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/thinking/convert.go)

`tests/test_effort.py` checks the actual SDK options, JSON merge, delegation,
transports, persistence/migration, and isolation without live provider calls.
These tests do not establish compatibility with every locally installed proxy
version or validate an authenticated OpenAI request.
