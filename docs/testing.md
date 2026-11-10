# OBS testing protocol

The full protocol, with Daniel's words and the reasons, is the vault note
`Projects/Personal Projects/OBS Platform/E2E Harness/Testing Protocol.md`. This
file keeps the essentials next to the code. If they disagree, the vault note wins.

## Tests are end-to-end only

- The only tests that count as evidence are **E2E suites**: pytest files under
  `tests/e2e/harness/suites/`, written against the lane API (`tests/e2e/harness/lane.py`).
  Each run starts whole OBS instances (lanes) in their own containers and drives
  them through real Telegram with test bots. Waits are on OBS events (the event
  tap, `src/obs_agent/event_tap.py`), never on fixed sleeps (`obs-e2e lint`).
- **The unit suite is deprecated and deleted** (2026-11-10, Daniel, AD-15/AD-16):
  no new unit tests, and nothing ever requires running a full suite. The deleted
  files remain readable at rc1 `5adcd336` (`git show 5adcd336:tests/<file>`);
  older docs that cite a `tests/test_*.py` file as evidence refer to that commit.
  The earlier `obs-live-test` design (`scripts/isolated_test_runner.py`,
  `deploy/obs-live-test/`) was never run and is superseded.
- **Keep-list** (intentional targeted tests, each added by a named decision):
  - the live cache regression and gate: `scripts/cache_regression.py`
    (`scripts/prefix_repro.py`) and `scripts/cache_gate.py` with its pre-push hook
    (`docs/cache-proxy-safety.md`);
  - obs-cli's pinned-reference golden and parity comparisons, run by their own
    named commands in the obs-cli repository;
  - the harness's acceptance self-tests (tap fail-open, dependency unavailable,
    `--help` inertness, lint, lane-daemon cycling);
  - `tests/e2e/conftest.py`, the kill guard and host-collection refusal.
- **Pending Daniel's answer:** `tests/test_cache_proxy*.py`, `tests/test_cache_gate.py`,
  `tests/conftest_cache_proxy.py` and the `tests/conftest.py` they need stay
  untouched; nothing runs or requires them.
- Adding a test means adding an E2E suite file. A new targeted non-E2E test is
  added only by a named decision recorded in the vault note's keep-list.

## Where tests run

- **Never on obs-test** (the container production runs in), in `/workspace/obs`,
  in any worktree or clone: no `pytest`, collection, single file or single test.
  obs-test shares production's user, process tree and paths.
- **Only through the launcher**, started from any worktree that contains the harness:

  ```bash
  deploy/obs-e2e/obs-e2e run --code "$PWD" --lanes 5 --suite smoke --profile dev
  deploy/obs-e2e/obs-e2e run --code "$PWD" --lanes 5 --suite isolation --profile dev --prod-send
  ```

  Results: `/workspace/runtime/e2e/runs/<run id>/` (`summary.json`, `timing.json`,
  `manifest.json`, `isolation/summary.json`). Exit 0 is green; a skip is never
  green. Runs are serialized on the test userbot's launch lease; a second run waits.
- The isolation suite proves a run separate from production with seven probes:
  processes (i, ii), environment (iii), production state files (iv), the shared
  refresh token (v), the production update stream (vi), and production paths (vii:
  read-write mounts only under the run's directory, in-lane write attempts land
  only on lane mounts, production directory modes and owners unchanged, launcher
  writes only under `/workspace/runtime/e2e/`).

## From a passing candidate to production

From the candidate's worktree, after a green 5-lane `smoke` run and a green
`isolation --prod-send` run on exactly that commit, a passing two-provider cache
gate, and the Work Registry's `obs-source` and `obs-restart` approvals:

```bash
/workspace/obs/.venv/bin/python scripts/promote.py --candidate <sha> \
  --lane-run <smoke run id> --lane-run <isolation run id> \
  --gate-report /workspace/runtime/cache-regression/<label> \
  --approval-source <id> --approval-restart <id> --dry-run
```

`--dry-run` runs every check and prints the planned writes. Without it the command
fast-forwards `/workspace/obs`, pushes `main`, runs the maintenance restart through
the cache-launch gate, confirms the candidate is admitted and running, and writes
a promotion record. It refuses, naming each failed check; it never uses
`--skip-cache-gate`, `--plain` or a freeze acknowledgement. A promotion on a
partial gate (one provider waived by Daniel) stays the hand sequence of the
vault's shared-changes procedure.

The shared-changes hook reads `OPS_SHARED_ACK` from the outer command line, so
the operator's command carries the approval id there too
(`OPS_SHARED_ACK=<source id> /workspace/obs/.venv/bin/python scripts/promote.py …`);
the script itself re-verifies both approvals and passes each id to its own step.
