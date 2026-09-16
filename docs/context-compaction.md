# Context budgets and automatic compaction

For the per-agent ownership and persistence map, see
[Agent and session architecture](agent-session-model.md).

OBS suffixes specify a requested budget, not a Claude Code model capability.
`gpt[400k]` must remain 400K in OBS state and child inheritance. The Claude
subprocess instead needs a recognized capacity selector and a percentage that
places compaction at the requested target within that capacity.

## Reference curve

The reference points are 200,000 context tokens / approximately 167,000 tokens
at compaction, and 1,000,000 / approximately 967,000. Anthropic documents the
latter; the previous OBS estimate of 920,000 was not the correct reference.
Linear interpolation in **token counts**, not percentages, gives:

```text
slope = (967000 - 167000) / (1000000 - 200000) = 1
intercept = 167000 - 200000 = -33000
T(C) = max(0, C - 33000)
```

The reference headroom is a 20K output reserve plus a 13K compaction buffer.
This is the configured target; actual trigger timing can cross it by the size
of an assistant/tool turn. The provider must also report usage correctly. An OBS
budget or a CLI capacity selector does not grant additional provider capacity.

| Requested suffix | OBS target | Claude selector | Native CLI window |
| --- | ---: | --- | ---: |
| `[35k]` | 2,000 | `[200k]` | 200,000 |
| `[64k]` | 31,000 | `[200k]` | 200,000 |
| `[100k]` | 67,000 | `[200k]` | 200,000 |
| `[128k]` | 95,000 | `[200k]` | 200,000 |
| `[200k]` | 167,000 | `[200k]` | 200,000 |
| `[333k]` | 300,000 | `[1m]` | 1,000,000 |
| `[400k]` | 367,000 | `[1m]` | 1,000,000 |
| `[1m]` | 967,000 | `[1m]` | 1,000,000 |

Every whole-K OBS budget from 35K through 1M is supported. The lower bound leaves
both the 33K reserve and a positive threshold within the documented percentage
range (at least 1%). Unsupported budgets fail explicitly, not silently default.

## Why the window variable alone did not work

The repository pins `claude-agent-sdk==0.1.44`, bundling Claude Code `2.1.59`.
The first native CI run for this change proved that **this binary ignores
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`**. Current documentation describes a newer
window-cap feature; assuming that contract for the pinned version was wrong.

The installed binary reported these values despite receiving a smaller window:

```text
requested 100K, selector 200K: effectiveWindow=180000
requested 400K, selector 1M:   effectiveWindow=980000
```

Those are the selector capacity minus the 20K output reserve, not the requested
window minus that reserve. The initial candidate computed percentages against
the smaller window, causing the native tests to fail even though Python policy
and actual SDK child-process propagation tests passed. The assertions were not
weakened to accept those wrong thresholds.

The final representation deliberately sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
to **the selector capacity**, not the requested budget. The percentage alone
carries the smaller requested threshold. Old releases that ignore the window
and newer releases that honor it then have the same percentage denominator.

```text
capacity = 200000 if requested_context <= 200000 else 1000000
reserve = min(explicit_max_output_tokens, 20000), or 20000 by default
target = min(requested_context, optional_OBS_cap) - 33000
percentage = 100 * (target + 0.5) / (capacity - reserve)
```

The half-token bias stays within the target token's floor interval, avoiding a
one-token rounding error when JavaScript converts the percentage to a float.
For 400K with the normal reserve the percentage is approximately 37.449%,
not 96.579%. The resulting integer threshold is 367,000.

## Session and environment boundaries

`SessionManager` resolves the requested model/context once and creates an
immutable `ClaudeContextPlan` for every new client. OBS state retains the
requested suffix; `ClaudeAgentOptions.model` uses only `[200k]` or `[1m]`.
The historical `normalize_model_for_claude_code` helper remains an OBS metadata
formatter. Its name is not a promise that arbitrary suffixes work in the CLI.

The three managed environment values are:

- `OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS`: the requested OBS budget.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: the recognized CLI capacity, deliberately.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`: the per-session percentage derived above.

They are supplied in both SDK `env` and inline settings `env`, without changing
shared process environment. The SDK merges `os.environ` into the child process;
popping an old percentage out of `options.env` does not remove that inherited
value. Explicit fresh values also prevent stale project-level window/percentage
settings from silently undoing the policy.

`OBS_AUTO_COMPACT_WINDOW_TOKENS` remains an optional earlier-compaction cap.
A 400K request with a 150K cap retains 400K OBS metadata and the 1M CLI selector,
but targets 117K compaction through its percentage. A cap cannot enlarge the
budget. Extremely small caps on the 1M selector (below about 43K) are rejected
because their percentage would fall below the documented 1% minimum.

An explicit smaller `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is preserved and reflected
in the denominator so it does not move compaction later. Conflicting disable
switches are rejected rather than secretly reversed. A raw
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` that differs from the CLI selector capacity is
also rejected: otherwise it could change the denominator on newer versions.
Configure arbitrary budgets with the OBS suffix, not that raw variable.

Provider access restrictions and managed settings remain outside OBS's control.
Project/managed settings that disable compaction or independently change the
output allowance need separate validation and must not be assumed compatible.
A context selector does not bypass provider entitlements or input limits.

Connected clients are not reconfigured in place. Reconnect/restart affected
sessions after deployment. Fresh children, inherited children, resumes, and
reconnects rebuild the plan without replacing 400K OBS metadata with 1M.

## Verification layers

1. `test_context_budget_regressions.py` checks parsing and the linear curve.
2. `test_claude_context_policy.py` covers every supported whole-K budget under
   both window contracts (ignored and honored), output reserves, operator caps,
   invalid inputs, and environment precedence. This is **not** a binary test.
3. `test_context_process_boundary.py` uses the real installed SDK transport and
   an inert child executable reporting its own argv/environment. It covers
   inheritance, fresh-child overrides, settings, and reconnect propagation.
4. `test_claude_context_binary.py` launches the installed Claude binary using
   the SDK's command builder. A loopback fake Anthropic API answers a tiny
   request. Assertions read the binary's own `autocompact ... threshold=`
   diagnostics. Independent tests remove OBS overrides to check native 200K/1M
   anchors, and another test installs stale project overrides. Missing or wrong
   diagnostics fail rather than being reported as a successful verification.

The native tests require `OBS_RUN_LOCAL_CLAUDE_CONTEXT_TESTS=1` and an actual
loopback-only network namespace. They use a temporary HOME and dummy key: no
model-provider calls, production credentials, Telegram poller, or daemon.
The scoped `context-compaction.yml` workflow installs locked dependencies and
then runs the selected tests in that namespace on a disposable Ubuntu runner.
It does not use the production-serving `obs-test` host or bypass the existing
host-managed live-test protocol. Diagnostics and JUnit results are preserved.

The first run, 34773836369 at commit 3edca1d, had 268 passing tests and seven
failing native assertions. That failure exposed the ignored window variable.
The follow-up changes the implementation to match the observed binary contract;
it does not change the requested 67K/367K target assertions.

Native diagnostics establish the binary's computed threshold, not an entire
paid long-context conversation or a deployment on the owner's Ubuntu host.
Rerun compatibility tests when changing SDK/CLI versions, model families,
output-token policy, or provider routing. A green arithmetic test alone is not
proof of native compaction behavior.

## Sources checked 13 September 2026

- Anthropic model configuration and extended-context controls:
  https://code.claude.com/docs/en/model-config
- Anthropic environment-variable reference:
  https://code.claude.com/docs/en/env-vars
- SDK v0.1.44 bundled version and subprocess implementation:
  https://github.com/anthropics/claude-agent-sdk-python/blob/v0.1.44/src/claude_agent_sdk/_cli_version.py
  https://github.com/anthropics/claude-agent-sdk-python/blob/v0.1.44/src/claude_agent_sdk/_internal/transport/subprocess_cli.py
- Initial native test run, including retained failing diagnostics:
  https://github.com/Breedoon/recursive-harness-builder/actions/runs/34773836369
- Upstream diagnostic reports (observations, not a stable API specification):
  https://github.com/anthropics/claude-code/issues/31806
  https://github.com/anthropics/claude-code/issues/44850
