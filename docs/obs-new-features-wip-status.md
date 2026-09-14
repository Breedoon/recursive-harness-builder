# OBS New Features WIP Preservation Status

> **WIP source preservation only. Not deployed or released.**

## Purpose

This branch preserves the recoverable implementation candidates from the August 2026 OBS New Features mission in one GitHub-visible branch. It is based on `b7624fa8632add4619e422856ee8d2a87c092323` and intentionally retains unfinished behavior and known gaps rather than presenting the union as production-ready.

Before this preservation branch, the requested public products and shipped products were both zero. Historical internal acceptance existed only for the stop/delete and stable identity/output candidates.

## Preserved feature work

### Stop and delete safety

- Implements `/stop_branch` for the current agent and recursive descendants.
- Implements `/stop_tree` for the complete team tree, including the trunk when invoked below it.
- Makes legacy `/delete` and `/delete all` non-destructive compatibility paths.
- Preserves repeat invocation and partial-failure reporting behavior.
- Historical focused verification: 11 passed, 0 failed.
- Known gap: the implementation uses bounded repeated tree snapshots rather than a generation fence, so a descendant created after the final snapshot can escape interruption.

Provenance:

- Commit: `870355857f486f03798ab513cef4d3b55a28dcb7`
- Parent/base: `b7624fa8632add4619e422856ee8d2a87c092323`
- Patch SHA-256: `7ca54f07f9432a736973ff9b52e96c7018a674d925729d1bcaa0ac9449d17900`

### Stable AgentTask identity and output

- Preserves candidate support for resolving and reporting task identity through stable team and agent names while retaining the real session identity internally.
- Preserves related prompt, session, Telegram, procedure, and test changes.
- Historical candidate-bound focused verification: 39 passed.
- Known gaps: the candidate was never shipped, cold-installed by a receiver, or validated as part of the complete four-feature union.

Provenance:

- Source: `/workspace/runtime/worktrees/obs-mission-20260812-task-identity`
- Base: `b7624fa8632add4619e422856ee8d2a87c092323`
- Tracked patch SHA-256: `672fab00c3c4421770e0fdc734cc0721fcd44f37ac757a7568d57203eb62c2c2`

### Lineage and agent-tree search

- Preserves target-relative tree relations, recursive descendants, activity filtering, pagination, and newest-activity ordering work.
- Preserves complete JSONL event scanning, maximum valid event timestamp selection, explicit mtime fallback, malformed-record skipping, and overflow-safe timestamp parsing.
- Historical focused evidence included 17 passing tests, a repaired six-test timestamp set, and ten independent overflow checks.
- Known gaps: later green lower-level evidence was not cryptographically bound to the final candidate postimage, so root acceptance was withheld; the candidate was never shipped or validated in the complete union.

Provenance:

- Source: `/workspace/runtime/worktrees/obs-mission-20260812-lineage-search`
- Base: `b7624fa8632add4619e422856ee8d2a87c092323`
- Tracked patch SHA-256: `c3692ed709991c5ac9568649c965df5aad5f8478b035e739f23970753a481b4c`

### Isolated testing protocol

- Preserves the proposed host/container preflights, state and credential separation, runtime attestation, evidence handling, cleanup checks, documentation, and protocol tests.
- Historical bounded design validation reached 47 passed.
- Known gap: this remains a dry-run/design-only framework. It does not own and operate a trusted disposable-container lifecycle, run a harmless offline OBS service through that lifecycle, or perform a credentialed test-provider smoke.

Provenance:

- Source: `/workspace/runtime/worktrees/obs-mission-20260812-testing`
- Base: `b7624fa8632add4619e422856ee8d2a87c092323`
- Tracked patch SHA-256: `eb0fb1f6c4c7a0e760f8b11e413bd10c32eb909ed3dc86d3f39f34da262bbba5`
- Complete candidate SHA-256: `23da38ca8478e13bbbf155141a85bac7176c8c00f5ec6d4f77b7d5c887df29bd`

## Integration status

The four candidates had not previously been assembled and tested as one combined tree. Preservation required two minimal overlap reconciliations:

- The import union in `src/obs_agent/tools.py` retains `re`, `math`, `secrets`, `time`, and `OrderedDict`.
- The path-injection regression test supplies an explicit stable `team_name` so it exercises path filtering without depending on unavailable caller-team fixture state.

No incomplete runtime feature was redesigned or silently finished.

Source-bound preservation checks produced:

- Prompt identity tests: 12 passed.
- JSONL activity tests: 10 passed.
- Tools identity/search tests: 91 passed.
- Focused stop/delete compatibility tests: 6 passed.
- Offline testing-protocol and related runtime tests: 163 passed, 8 skipped.
- Complete Telegram unit file: 246 passed, 23 failed.
- `git diff --check`: one preserved blank line at EOF in `tests/test_live_test_protocol.py`; retained to keep that candidate file byte-exact.

The 23 Telegram failures are retained as integration evidence rather than hidden. They cluster around team-worker inbox wake/persistence, stale worker cleanup, model shorthand expectations, caller-team inference, and output-snapshot fixture compatibility across the independently authored candidates. The union is therefore not production-ready.

The required end-to-end `search → AgentTaskOutput → Stop → search` oracle, broad integrated regression suite, separate credentialed smoke, receiver custody proof, and deployment remain incomplete.
