# WIP review: 13 September 2026

## Scope and status

Reviewed the preserved WIP snapshot at `7193b651c38db7c0206a07fce2f665174db94531`.
The first hardening commit is `347d97ff7ce25122424e8491c66bad2946c74c8b`.
This follow-up addresses search correctness and another transcript-decoding pass.
It is not a deployment, a release approval, or an assertion that the branch is
free of defects.

The work used an isolated assistant workspace, not the owner's Ubuntu host.
No Telegram poller, model, daemon, Docker service, or credentialed test was
started. The existing live-test restrictions remain in place.

## Fixed and regression-covered

### Transcript lookup and launch gates

The first commit rejects path-like session identifiers, skips malformed event
shapes and invalid UTF-8, excludes negative/boolean usage counters, and preserves
valid lookup results when another project or duplicate becomes inaccessible.
It also rejects every explicit legacy test selector: a later production option
cannot erase an earlier test request. Library-only profile parsing is unchanged.

The follow-up handles JSON decoder `ValueError`/`RecursionError` failures without
hiding later valid records. A failure to estimate deeply nested message content
no longer discards independent usage counters in that same decoded record.

### Search timestamps and persisted metadata

Activity timestamps must be finite and representable as a UTC datetime before
they participate in newest-event selection or result serialization. Previously,
a finite value such as `1e100` could win the event scan and crash the subsequent
`datetime.fromtimestamp` call. Timezone offsets near datetime's year boundaries
had the same problem. Invalid preferred activity fields now fall through to a
valid lower-priority timestamp; legacy `updated_at` milliseconds remain supported.

Wrong-shaped team configuration JSON is ignored. Non-string session identifiers
and runtime statuses from a provider cannot trigger unhashable-key exceptions.
Valid session identifiers are normalized consistently for lookup and output.

### Incomplete trees and pagination

Ancestor searches skip unavailable ancestor records instead of indexing missing
metadata. On-behalf searches preserve the caller in `current_agent` and report
the inspected agent separately in `target_agent`, including cursor continuation.
Snapshots copy nested metadata so later provider mutations cannot change a page
that was already frozen. Fractional page sizes and offsets are rejected rather
than silently truncated. Existing integer-compatible inputs remain supported.

### Provider failures

Provider call style is selected by inspecting its signature before calling it.
A TypeError raised inside a provider is no longer mistaken for a positional-only
signature and retried. A failed provider falls back to projection metadata;
legacy positional-only providers remain supported.

## Verification and its limits

All checks below ran under Python 3.13.5 in the isolated assistant workspace.

- 55 direct tests in `test_context_jsonl_resilience.py` and
  `test_runtime_env_gate_regressions.py` passed without SDK stand-ins.
- 81 tests in `test_search_team_resilience.py` passed against the full modified
  `tools.py`, using explicit stand-ins for unavailable SDK registration and unused
  import boundaries. The tests independently substitute identity/projection and
  provider boundaries. This is not an SDK integration test.
- All 136 focused cases passed together. The initial 68-case search reproducer
  set had 48 failures and 20 passes against the original `tools.py`.
- The added decoder-limit pass initially reproduced four failures before repair.

The complete repository suite was not run. Claude Agent SDK, Telegram, and
croniter were unavailable in this workspace. The numbers above are focused test
counts, not a claim that the repository's historical integration failures have
been eliminated. No tests were deleted or marked passing to hide those failures.

## Remaining high-priority review findings

### Scoped stop still has a concurrent-launch gap

`TelegramBot._handle_scoped_stop` takes two snapshots without an intervening
await, then awaits interruption. Those two reads do not establish a fence against
children published after the snapshot. The original preservation document
already records this gap. The fix must coordinate admission/publication of child
tasks with stop requests, not merely increase the snapshot count. This review
has not implemented or live-validated such a lifecycle change.

### Runtime mount inventory omits unrecognized mounts

`ContainerRuntimeObservationAdapter._mount_records` returns only four exact
known targets and skips every other mountinfo record. `inner_preflight` then
compares that filtered collection against the attested mount set. An unexpected
mount, including a nested mount under the source or run root, cannot be detected
by that comparison because the observation discarded it first.

This is a source-level isolation-check blind spot, not evidence of an actual
production incident or a demonstrated complete preflight bypass. The protocol
needs an explicit policy for the complete observed inventory, including required
container-system mounts and forbidden extra/nested mounts. Its dry-run-only
status must not be promoted to a security assurance on the basis of the current
synthetic observation tests.

### Telegram tree rendering has a separate missing-ancestor path

`TelegramBot._render_tree_html(mode="ancestors")` derives `allowed` from the
lineage and later calls `_label_html` for each derived name. `_label_html` indexes
`members[agent_name]`. A missing intermediate projection is therefore not filtered
out by the `if agent_name in allowed` condition. The analogous MCP `search_team`
path is fixed here; the separate Telegram renderer still requires a regression
and repair. Its async observability flush also cancels the registered flush task
without excluding the current task; cancellation from the delayed-flush path
needs targeted reproduction before proposing a transport change.

## Acceptance work still required

The original preservation report records 246 passing and 23 failing Telegram
unit tests. Those are historical repository-reported counts, not results from
this review. They cover worker wake/persistence, stale cleanup, model aliases,
caller-team inference, and output snapshot compatibility. They remain unverified.

Still required: the full suite with actual dependencies, deterministic concurrent
stop/launch tests, stable identity and output tests across restart, the combined
`search -> AgentTaskOutput -> Stop -> search` oracle, and authorized host-managed
`obs-live-test` validation. The testing framework remains a dry-run candidate;
its disposable service lifecycle and credentialed smoke are not implemented or
verified by this review.
