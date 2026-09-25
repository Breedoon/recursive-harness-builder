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

## Local models: same window-derived plan (2026-09-25)

Local models used to be special-cased: OBS passed the bare model ID and only
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which 2.1.59 ignores. It also dropped the
percentage. So every local session compacted at the CLI's fixed 200K-capacity
threshold of **167,000** reported tokens, whatever its real window (262K for
`local-qwen3.8-27b`). Double-counted usage from vLLM made this worse, and was
fixed separately in the cache proxy (`LocalUsageFixer`, commit `ac20834`).

Local models now go through the same `build_claude_context_plan` as hosted
ones. The threshold is `window - 33K`, where the window comes from the model's
suffix or from `MODEL_CONTEXT_WINDOWS`. No per-model constant is added:

- `local-qwen3.8-27b` (262K): selector `[1m]`, percentage approx. 23.367%, target **229,000**.
- `local-qwen3.8-27b[500k]`: target 467,000.
- `local-gemma4-31b` (48K): selector `[200k]`, target 15,000.

The cache proxy strips the selector for every route before routing. So the
gate still receives the literal ID, and the upstream request only gains the
CLI's `context-1m-2025-08-07` beta header. A live request with that header was
tested against the gate: HTTP 200, completed stream. **Exception:** a local
session whose requests reach the gate directly keeps the bare ID. That happens
when the proxy is disabled, or when the session has an explicit
`ANTHROPIC_BASE_URL` that is not the proxy. The gate routes on the literal ID
and would not strip a selector. That session is therefore planned inside the
CLI's 200K capacity, and OBS logs a warning. The native probe confirms both
routes on the pinned binary: `local-262k-proxy` gives threshold 229,000 and
`local-262k-direct` gives 167,000
(`/workspace/runtime/tmp/compaction-harness/e5-native/`).

## Explicit per-session values win (2026-09-25)

Precedence, highest first:

1. an explicit per-session `env` (AgentTask `env`, `SessionManager.set_sdk_env_overrides`);
2. the OBS plan;
3. the daemon's process environment and project settings.

This applies to the plan-owned keys (`OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS`,
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`) at both
the SDK `env` and the inline `--settings` layer. Earlier, the plan overwrote
explicit values at both layers, so an AgentTask `env` could not change
compaction.

`DISABLE_AUTO_COMPACT` / `DISABLE_COMPACT` set explicitly for one session are
now accepted for **every** model family: Claude, GPT/Luna, and local. OBS
writes `DISABLE_AUTO_COMPACT=1` at both layers and keeps the selector and
percentage, so the context display stays correct. A daemon-wide disable switch
without an explicit per-session choice is still rejected. That is the case the
original check was written for: never silently reverse an operator's global
kill switch. An explicit `"0"` shadows a daemon-wide `1` for that session.
`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` is never set by OBS: with compaction
disabled there is no extra hard limit beyond the provider's own.

The native probe on 2.1.59 confirms that an explicit disable removes the
`autocompact` threshold diagnostic entirely (`luna-120k-disable`,
`claude-200k-disable`, `local-262k-disable`).

A launcher that copies a parent's resolved env into a child must not copy
the plan-owned keys, or they now pin the child's budget.

## PreCompact handoff policy (opt-in, 2026-09-25)

`OBS_COMPACT_POLICY=handoff` in a session's explicit env replaces automatic
compaction with "stop and hand off from full context". The pinned 2.1.59
ignores a PreCompact `block`, and it always appends "Please continue the
conversation from where we left off" after an automatic compaction. Steering
the summary through PreCompact `systemMessage` or SessionStart(compact) context
was tested and left no trace in the JSONL: the agent just continued its task.
So the mechanism is an interrupt:

1. OBS registers `PreCompact`. When an automatic (not manual) compaction
   fires, the session's user PreCompact hook runs first. Its
   `additionalContext` becomes the handoff prompt, so the vault owns the text;
   otherwise `DEFAULT_COMPACTION_HANDOFF_PROMPT` is used. OBS then **awaits
   `client.interrupt()` inside the callback**, before the CLI sends the summary
   request. The SDK serves control requests concurrently, so this does not
   deadlock. The CLI logs an `AbortError` for the aborted hook request; that
   is expected.
2. `ConversationRunner` sees the interception. It suppresses the
   "[Request interrupted by user]" residue, and does not retry on the old
   process: a running CLI cannot take `DISABLE_AUTO_COMPACT` and re-fires
   compaction on its next query. It marks the session
   (`SessionManager.activate_compaction_handoff`, sticky for that session ID)
   and reconnects. The same session ID is resumed in a new process with
   `DISABLE_AUTO_COMPACT=1`, and the handoff prompt is sent as a normal,
   persisted query.
3. A user Stop hook can now block ending the turn:
   `HookPipeline` passes `{"decision": "block", "reason": ...}` through for
   Stop, and 2.1.59 honours it. The vault's `level_composite.stop` uses this
   to require the handback file.

Evidence:

- E3 harness, 5/5 trials: no `compact_boundary`, full history kept.
- In-process OBS smoke on hosted Luna, 3/3 trials: the worktree
  `SessionManager`/`ConversationRunner` and the vault `level_composite` hooks
  produced 0 compact boundaries, 1 handoff query each, a written
  `handback.md`, and context that grew past the native threshold without
  compaction (`/workspace/runtime/tmp/compaction-harness/e5-smoke/`).

Live verification inside the daemon, after restart, is E6 of the enforcement
mission.

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
