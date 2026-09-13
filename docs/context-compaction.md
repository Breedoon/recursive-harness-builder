# Context budgets and automatic compaction

OBS context suffixes specify a requested budget, not a Claude Code model ID.
`gpt[400k]` must remain 400K in OBS state and child inheritance, while the Claude
subprocess needs a recognized capacity selector plus an independent budget.

## Reference curve

The reference points are 200,000 context tokens / approximately 167,000 tokens
at compaction, and 1,000,000 / approximately 967,000. The latter is documented by
Anthropic; the previous OBS estimate of 920,000 was not the correct reference.
Linear interpolation in **token counts**, not percentages, gives:

```text
slope = (967000 - 167000) / (1000000 - 200000) = 1
intercept = 167000 - 200000 = -33000
T(C) = max(0, C - 33000)
```

The reference headroom is a 20K output reserve plus a 13K compaction buffer.
This is the configured target; actual trigger timing can cross it by the size
of an assistant/tool turn. Context usage must also be reported correctly by the
provider. A configured budget does not grant additional provider capacity.

| Requested suffix | OBS target | Claude selector | CLI compact window |
| --- | ---: | --- | ---: |
| `[64k]` | 31,000 | `[200k]` | 100,000, with earlier percentage |
| `[100k]` | 67,000 | `[200k]` | 100,000 |
| `[128k]` | 95,000 | `[200k]` | 128,000 |
| `[200k]` | 167,000 | `[200k]` | 200,000 |
| `[333k]` | 300,000 | `[1m]` | 333,000 |
| `[400k]` | 367,000 | `[1m]` | 400,000 |
| `[1m]` | 967,000 | `[1m]` | 1,000,000 |

The supported OBS range is 34K through 1M. The 34K minimum preserves the 33K
reserve and a positive threshold representable by Claude's percentage control.
Claude's documented compact-window floor is 100K; smaller OBS budgets use that
floor plus an earlier percentage. Unsupported budgets fail explicitly instead
of reverting to a different context length.

## Subprocess boundary

`SessionManager` resolves the requested model/context once, then builds an
immutable `ClaudeContextPlan` for each new client:

- `HookState.effective_model` retains the requested suffix for reporting and
  inheritance. The historical `normalize_model_for_claude_code` helper remains
  an OBS metadata formatter; its output is not passed directly as a CLI model.
- `ClaudeAgentOptions.model` selects `[200k]` for budgets at or below 200K and
  `[1m]` above 200K. Arbitrary `[400k]` is never sent as a capacity selector.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` supplies the budget independently.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is recalculated per session against the CLI
  window **after** its output reserve. A half-token rounding bias avoids an
  accidental one-token early threshold when JavaScript floors the calculation.
- The three managed context values are supplied in both SDK `env` and inline
  settings `env`, rather than editing shared process state. This also prevents
  an old project-level window/percentage from silently undoing the budget.

The SDK merges `os.environ` into the subprocess environment. Merely removing an
old percentage key from `options.env` does not remove the inherited value. OBS
therefore supplies a fresh percentage explicitly, including for 200K and 1M.

`OBS_AUTO_COMPACT_WINDOW_TOKENS` remains an optional earlier-compaction cap. For
example, a 400K request with a 150K cap retains 400K OBS metadata but targets
117K compaction. A cap cannot increase the requested window.

An explicitly smaller `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is preserved and accounted
for in the percentage denominator; it does not silently move the OBS target.
Raw Claude window/percentage values are superseded by OBS's per-session policy.
Explicit compaction-disable switches are rejected, not secretly reversed. A
conflicting `CLAUDE_CODE_DISABLE_1M_CONTEXT` or `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
produces an actionable error. Provider access restrictions and managed settings
remain outside OBS's control. Project settings that disable compaction entirely
must not be used with this policy.

Connected clients cannot be reconfigured by changing Python settings alone.
Restart/reconnect the affected sessions after deploying this change. Fresh
children, inherited children, resumed sessions, and reconnects rebuild the plan.

## Verification layers and version scope

The dependency pin remains `claude-agent-sdk==0.1.44`, whose bundled CLI version
is `2.1.59`. Current documentation also describes later CLI versions, so the
native binary check is an acceptance requirement, not an assumption based solely
on documentation or a Python formula.

1. `test_context_budget_regressions.py` verifies parsing and the reference curve.
2. `test_claude_context_policy.py` exercises every whole-K budget in the supported
   range, output reserves, operator caps, bad inputs, and environment precedence.
   Its reference formula is **not** an actual-Claude test.
3. `test_context_process_boundary.py` runs the real installed SDK transport with
   an inert child executable which reports its own argv/environment. This checks
   actual SDK process creation, inherited overrides, and reconnect propagation.
4. `test_claude_context_binary.py` launches the installed Claude binary using the
   SDK's command builder. A loopback fake Anthropic API answers a tiny request.
   The assertion reads the binary's own `autocompact ... threshold=` diagnostic.
   Missing diagnostics or mismatched values fail the test. This verifies the
   computed threshold, not a paid long-context model conversation.

The native test requires `OBS_RUN_LOCAL_CLAUDE_CONTEXT_TESTS=1` and an actual
loopback-only network namespace. It uses a temporary HOME, dummy API key, no
production credentials, no Telegram poller, and no daemon. The scoped
`context-compaction.yml` workflow installs locked dependencies, then runs the
selected tests in such a namespace on a disposable GitHub-hosted Ubuntu runner.
It does not use the production-serving `obs-test` host or bypass the existing
host-managed live-test protocol.

A passing arithmetic test must not be reported as proof of native compaction.
Retain native debug artifacts and rerun compatibility tests whenever the SDK,
CLI, model family, or output-token policy changes. Unsupported models still need
provider-specific validation even when the CLI can represent their budget.

## Sources checked 13 September 2026

- Anthropic model configuration, extended context, and compact-window controls:
  https://code.claude.com/docs/en/model-config
- Anthropic environment-variable reference:
  https://code.claude.com/docs/en/env-vars
- SDK v0.1.44 bundled version:
  https://github.com/anthropics/claude-agent-sdk-python/blob/v0.1.44/src/claude_agent_sdk/_cli_version.py
- SDK v0.1.44 subprocess command and environment merge:
  https://github.com/anthropics/claude-agent-sdk-python/blob/v0.1.44/src/claude_agent_sdk/_internal/transport/subprocess_cli.py
- Upstream diagnostic reports (observations, not a stable API specification):
  https://github.com/anthropics/claude-code/issues/31806
  https://github.com/anthropics/claude-code/issues/44850
