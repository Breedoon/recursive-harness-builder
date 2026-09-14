# Live test suite matrix

This document is the future lane, scenario, and evidence matrix. It is not a
same-container recipe. Formal execution is allowed only through the separate
host-managed `obs-live-test` service and `scripts.isolated_test_runner` after
custody, protocol, credential, and strict-verification gates approve it.

Current repository state: the protocol is an **UNVERIFIED / UNRESOLVED**
authored candidate parked uncommitted. Every runtime/evidence result below is
planned, not executed. Future live and mundane execution uses `gpt-5.6-luna`.

## Scenario contract

Every later scenario records prerequisites, stimulus, expected result, required
redacted evidence, cleanup, and lane:

- `non-live` — safe to automate in the isolated container after preflight;
- `live` — explicit selection required, dedicated test Telegram/model resources;
- `manual-gate` — host/container/credential setup or evidence review required.

The evidence schema must distinguish `planned`, `executed`, `passed`, `failed`,
`skipped`, and `not-run`; this candidate uses `not run` fields and fabricates no
PASS results.

## Preflight negative matrix

| Requirement | Stimulus | Expected result | Evidence / cleanup | Lane |
| --- | --- | --- | --- | --- |
| Marker | Missing, malformed, or wrong positive marker | Deny before client/poller construction | Secret-free denial code; no process/network; no resources | non-live |
| Service identity | Missing/wrong service, especially `obs-test` | Deny; identify production-serving container | Preflight reason; production inventory unchanged | non-live |
| Profile/mode | `prod`, unknown, or `OBS_PROD_*` control | Deny; profile never acts as boundary | Environment key names only; no network | non-live |
| Source | `/workspace/obs`, production mount identity, missing source, or source mutation during snapshot | Deny | Requested/observed identity mismatch and read-only mount proof | non-live |
| Source identity | Requested commit differs from observed commit | Deny before launch | Secret-free commit IDs; remove snapshot | non-live |
| HOME/Claude/project | Production/shared or missing dedicated paths | Deny after normalized containment check | Realpath map; no state cleanup in production | non-live |
| Fixture | Missing/shared fixture project | Deny | Fixture identity; remove run root | non-live |
| Database/WAL | Production/shared SQLite or WAL | Deny | Path map; no DB touched | non-live |
| Cache | Production port/path/data/log or cache enabled without isolation | Deny | Port/path evidence; no cache process | non-live |
| Temp/log/evidence/daemon | Missing, shared, production, symlink, or `..` escape | Deny | Normalized path map; no residual files | non-live |
| Mount | Shared writable mount or source not explicitly read-only | Deny | Mount inventory; no mutation | non-live |
| Network | Host/production/shared namespace or missing identity | Deny | Namespace identity; no network call | non-live |
| Credentials | Missing, malformed, expired, wrong class, non-allowlisted, production match, or generic fallback | Deny without raw values | Safe ID/fingerprint only; no auth attempt | non-live |
| Environment | Repository `.env` or ambient production values repopulate keys | Deny | Key names and redacted source; no client | non-live |
| Poller | Selected bot already owned outside lane, stale PID/lock, duplicate/race | Deny or bounded race loser | Before/during/after inventory; remove isolated state | non-live |
| Unknown state | Any required fact cannot be proven | Deny, never warn-and-continue | Secret-free reason; no process/network | non-live |

## Positive and lifecycle matrix

| Scenario | Prerequisites / stimulus | Expected result | Required evidence / cleanup | Lane |
| --- | --- | --- | --- | --- |
| Cold bootstrap | Nothing running; clean snapshot and fixture | Isolated service preflights; poller remains inert until selected | Manifest, identities, path map, cleanup | non-live |
| Unit/focused regression | Approved preflight; fake clients | Focused tests pass without external credentials | Node IDs, return codes, redacted logs | non-live |
| Integration daemon | Dedicated state/cache/log paths | Start/stop is contained; no production process | Process inventory before/during/after | non-live |
| SQLite/WAL restart | Writes, restart, restoration | State/team/session restoration uses test DB only | DB/WAL checksums, restart evidence, cleanup | non-live |
| Service restart/reboot | Restart service/host; no autostart poller | Test service remains inert until explicit selection | Supervisor/host state; no Telegram call | manual-gate |
| Preparation interruption | Interrupt snapshot/path/fixture setup | Partial setup cleans idempotently | Remaining resources and retry manifest | non-live |
| Preflight interruption | Interrupt before decision | No client/poller starts; cleanup safe | Factory call fakes and cleanup | non-live |
| Daemon/test interruption | Interrupt startup, test, evidence, or cleanup | Processes stop; evidence remains bounded/redacted | Return code, partial evidence, final scan | non-live |
| Stale PID/lock/orphan | Inject stale metadata/process | Deny or safely reconcile without killing production | Inventory and decision reason | non-live |
| Occupied port | Cache/daemon port occupied | Deny; never reuse production port | Port identity; no fallback | non-live |
| Concurrent runs | Same bot versus distinct bots | Same bot one winner; distinct bots remain isolated | Race outcome and inventories | non-live |
| Disk/full/permissions | Deny writes or read-only source | Fail closed; no partial production writes | Error redaction, cleanup | non-live |
| Malformed manifest/evidence | Invalid field or partial write | Deny/mark incomplete; no fabricated PASS | Manifest state and checksum result | non-live |
| Cache off/on | Disabled then isolated cache enabled | Both modes preserve path/port isolation | Cache paths, process evidence | non-live |
| Large agent tree | 1,000–5,000 synthetic members | Bounded output and context; no repeated global scan | Timing/counts, output bounds | non-live |
| Pagination/activity | Offset pages; exact timestamps | Default 50 and documented changing-page behavior | Page metadata and boundary cases | non-live |
| Agent identity restart | Discover then output by stable team/agent before/after restart | UUID compatibility and session identity retained | Stable IDs, session IDs, bounded output | live |

## Live Telegram matrix

Live lanes are explicit and require dedicated test-only Telegram credentials,
validated model authentication, the dedicated container, and `gpt-5.6-luna`:

- `/stop_branch` from root, middle, and leaf; self plus recursive descendants;
  siblings/ancestors/other trees survive.
- `/stop_tree` from a grandchild; every root-team member including trunk is
  interrupted; repeated invocation is bounded and idempotent.
- Stale routes, disconnected clients, partial interrupt failures, and poller
  spawn races continue with aggregate results rather than aborting the batch.
- Completion callbacks and parent wakeups remain visible and acceptable; no
  notification suppression is introduced.
- `/delete` and `/delete all` return non-destructive deprecation guidance and
  do not delete topics, routes, or state.
- `search_team` discovers a running agent in another known tree, reports
  runtime status, and orders activity by authoritative JSONL writes.
- A supervisor discovers an agent, calls `AgentTaskOutput` by stable
  `team_name`/`agent_name` without a task UUID, receives bounded running or
  completed output, and repeats after daemon restart.
- Production and test poller inventories are captured before/during/after every
  live lane; cleanup leaves no test poller or secret-bearing artifact.

## Evidence and cleanup

A later run retains a bounded redacted bundle with run ID, UTC timestamps,
requested/observed source commit, service/marker/image/container identity, safe
bot ID and credential fingerprint, resolved model, lane/scenario, exact
commands/node IDs/return codes, preflight decisions, production process/source
state before/during/after, redacted logs/transcripts, cleanup result, remaining
resources, final secret scan, and checksums. All runtime fields in this
provisional slice are `not run`.

The legacy public `scripts/run_parallel_live_smoke.py` launcher is disabled and
fails closed before credential discovery, output creation, or subprocess
startup. Its non-launching helpers remain available only for unit/library
compatibility. Historical live modules that spawn `telegram_main --test` or
`--test-instance`, plus every direct consumer of their Telegram fixture/launch
helpers, are explicitly skipped scenario inventory rather than executable formal
entry points. Mixed unit/live modules retain only their pure unit coverage. Any
later migration must route those scenarios through `scripts.isolated_test_runner`,
the host attestation, and the `obs-live-test` inner preflight.
