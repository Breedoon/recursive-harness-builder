# OBS testing protocol

> **Authoring status:** this bounded repository-side correction is parked
> uncommitted and is **UNVERIFIED / UNRESOLVED**. No syntax check, import, test,
> hook, build, container, credential, network, Telegram, model, cleanup,
> integration, release, or production-readiness claim has been executed.

Formal OBS testing is canonical only through a separately created Ubuntu service
named `obs-live-test` with marker `obs-live-test-v1`. The current container named
`obs-test` is production-serving and is never a formal-test boundary. Legacy
`--test`, `--test-instance`, `--profile test`, `OBS_PROFILE=test`, and
`scripts/run_parallel_live_smoke.py` live entry points fail closed and redirect
to this protocol; only pure unit/library helpers retain compatibility.

## Repository-owned surfaces

- `scripts/live_test_protocol.py` defines host observation, immutable host
  attestation, inner observation, source/image/executed-code binding, topology,
  credential and poller inventory checks, evidence redaction/writing, and
  ownership-bound cleanup.
- `scripts/isolated_test_runner.py` is the only formal runner. Its `host-preflight`
  phase runs before service creation; its `inner` phase runs before any client,
  process, daemon, network, or poller factory.
- `deploy/obs-live-test/compose.yaml` is an inert service-creation template. It
  consumes a host-preflight attestation and exact run-owned resources; it does
  not contain caller-set “verified” booleans.
- `tests/test_live_test_protocol.py` and focused public-entry tests are authored
  adversarial coverage. They were not collected or run.
- `docs/live-test-suite.md` is the planned lane/scenario/evidence matrix.

## Two-stage fail-closed sequence

1. A trusted host runtime observer, independent of Compose and the test
   container, records a complete time-bounded JSON observation.
2. Host preflight validates that observation before service creation and emits
   a deterministic `sha256:<64 lowercase hex>` integrity-bound attestation.
3. Only after that attestation exists may the host populate the exact Compose
   inputs and create `obs-live-test`.
4. Inside the created container, a runtime observer measures procfs, mountinfo,
   network namespace, source tree bytes, executed runner bytes, immutable image
   marker/service/runtime identity, mounted secret bytes, topology ownership/modes,
   actual hostname, and environment wiring.
5. Inner preflight compares those measured facts with the host attestation. A
   mismatch or unknown fact denies before every application factory.
6. This provisional candidate authorizes only `--dry-run`; it writes a
   `planned_not_run` manifest and starts no client, daemon, network, test,
   service, or poller.

A Compose variable, environment variable, caller boolean, requested commit, or
container self-report, including marker or service strings, is not host evidence. The host observer must record its
kind, independent provenance, observation time, completeness, ownership, and
measured values.

## Canonical dry run

There is exactly one provisional host command, authored but unrun:

```text
python -m scripts.isolated_test_runner --phase host-preflight --host-observation /workspace/runtime/obs-live-test-host-observation.json --compose-file deploy/obs-live-test/compose.yaml --lane unit --scenario focused --dry-run
```

It validates the host observation and prints an attestation plus the exact inner
command. It does not write the attestation or create a service. A later approved
host executor must exclusively write the returned document to the exact
attested path, bind the ownership marker, populate the exact Compose values, and
only then create the service. The immutable inner command is:

```text
python -I /source/snapshot/scripts/isolated_test_runner.py --phase inner --attestation /run/obs-live-test-attestation/host-attestation.json --lane unit --scenario focused --dry-run
```

The exact inner runner adds only `/source/snapshot` to imports after its resolved
file path matches `/source/snapshot/scripts/isolated_test_runner.py`; ambient
`PYTHONPATH` is neither configured nor trusted. The live lane must be selected
explicitly later and uses `gpt-5.6-luna`; this
candidate does not claim that model was invoked.

## Required host observation

The observation must be produced by `host-runtime-inspector-v1`, not copied from
Compose/environment, and include:

- run ID `run-YYYYMMDDTHHMMSSZ-<8 lowercase hex>`, unique nonce, issue/expiry
  timestamps, exact run root, exact attestation path, and independently observed
  non-root runtime UID/GID;
- requested and independently observed 40-character source commit, measured
  immutable source-tree SHA-256, an observed run-owned non-symlink/non-writable
  source snapshot path, executed runner path, measured runner SHA-256, and source
  copy SHA-256;
- image reference pinned as `repository@sha256:<64 lowercase hex>`, independently
  observed image digest/provenance, image identity embedded outside the writable
  run mount, and the resolved runtime executable path and measured bytes;
- complete independently observed production/shared root, production credential,
  production poller, production port, and production network inventories;
- the measured test-secret fingerprint, regular-file/no-symlink/UID/GID/mode
  evidence, test-only credential class, allowlist, safe credential and bot IDs,
  authentication observation, provenance, and expiry covering the complete
  attestation window;
- a fresh selected-bot poller reservation whose measured lease ID matches the
  credential and whose lease remains valid through attestation expiry;
- exact source/run/secret/attestation mount plan and ownership;
- exact private run-owned network and host-observed cache/daemon port leases,
  with fresh network/port reservation provenance and lease expiry through the
  attestation window retained in the attestation.

Repository `.env`, generic ambient token fallback, production secret paths, and
empty “production inventories” are denied. Raw secret values never enter the
attestation or evidence.

## Exact pairwise-disjoint topology

Every resource below is a distinct canonical sibling under
`/run/obs-live-test`; no two are equal and no resource is an ancestor or
descendant of another:

| Concern | Container path / setting |
| --- | --- |
| HOME | `/run/obs-live-test/home` / `HOME` |
| Claude config | `/run/obs-live-test/claude-config` / `CLAUDE_CONFIG_DIR` |
| Claude projects | `/run/obs-live-test/claude-projects` / `OBS_CLAUDE_PROJECTS_DIR`, `CLAUDE_PROJECTS_DIR` |
| Claude teams | `/run/obs-live-test/claude-teams` / `OBS_CLAUDE_TEAMS_DIR`, `CLAUDE_TEAMS_DIR` |
| Claude sessions | `/run/obs-live-test/claude-sessions` / `OBS_CLAUDE_SESSIONS_DIR`, `CLAUDE_SESSIONS_DIR` |
| Claude auth file | `/run/obs-live-test/claude-auth.json` / `OBS_CLAUDE_AUTH_FILE`, `CLAUDE_AUTH_FILE` |
| Project root | `/run/obs-live-test/project` / `OBS_TEST_PROJECT_ROOT` |
| Fixture | `/run/obs-live-test/fixture` / `OBS_VAULT_PATH` |
| SQLite | `/run/obs-live-test/state.sqlite3` / `OBS_TELEGRAM_STATE_DB_PATH` |
| WAL | `/run/obs-live-test/state.sqlite3-wal` / `OBS_TELEGRAM_STATE_WAL_PATH` |
| Cache root/data/log | `/run/obs-live-test/cache-root`, `cache-data`, `cache-log` |
| Temp/download | `/run/obs-live-test/temp`, `/run/obs-live-test/download` |
| Runtime/Telegram logs | `/run/obs-live-test/runtime.log`, `telegram.log` |
| Evidence | `/run/obs-live-test/evidence` |
| Daemon metadata/PID/lock | `/run/obs-live-test/daemon-metadata`, `daemon.pid`, `daemon.lock` |
| Ownership marker | `/run/obs-live-test/ownership.json` |
| Cache/daemon ports | two distinct host-observed ports, neither production nor occupied |

The source snapshot is separately read-only at `/source/snapshot`; the test
secret and host attestation are separate read-only file mounts. The topology
may not overlap source, production, shared, secret, attestation, or evidence
retention paths. All directories are observed mode `0700`; all files are `0600`
except the read-only secret, which is host-observed `0400` or `0440`. Inner
preflight returns the complete topology in one immutable `RuntimePolicy`; that
same object is the sole input to process, network, and poller factories, and its
exact environment is applied before any factory. No individual factory may
reconstruct paths or ports from ambient defaults.

## Canonical denial policy

Host and inner preflight deny:

- `obs-test`, wrong hostname/service/marker, production mode, root or unapproved
  process ownership, or profile-only “isolation”;
- `/workspace/obs`, any production/shared/source ancestor or descendant overlap,
  writable source, source symlinks, source commit/tree/runner mismatch, mutable
  image tag, or image mismatch;
- missing, incomplete, caller/environment/container-derived inventories;
- malformed SHA-256, unmeasured/expired/non-test/non-allowlisted credentials,
  production fingerprint matches, repository `.env`, any populated sensitive
  ambient credential variable outside the exact policy-owned path settings, or
  populated `OBS_PROD_*` state;
- missing selected-bot reservation, selected-bot production leases, competing
  leases, unknown poller state, stale inventory epoch, or any duplicate poller
  race;
- host/shared/production network namespace, a non-internal Compose bridge,
  unknown network ownership, shared writable mount, unobserved mount, unknown
  mount, or changed mount mode;
- selected ports outside `29000..29999`, malformed production ports outside
  `1..65535`, duplicate ports, production ports, occupied ports, missing host
  provenance, or lease ownership outside the run;
- path aliasing, parent/child topology overlap, symlink components, wrong type,
  wrong mode, wrong owner, traversal, missing setting, or environment rewiring;
- any fact that cannot be positively measured. Unknown means deny, never warn
  and continue.

## Evidence, redaction, and cleanup

Evidence is decision-bound to exactly
`/run/obs-live-test/evidence/manifest.json`. The writer:

- canonicalizes and contains the path under the attested evidence root;
- rejects symlink components and arbitrary caller paths;
- requires directory-descriptor and no-follow support, then opens the run root
  and evidence directory with `O_DIRECTORY | O_NOFOLLOW`;
- opens an exclusive temporary sibling relative to the anchored evidence
  descriptor with `O_EXCL | O_NOFOLLOW`;
- refuses any existing target or temporary path;
- flushes/fsyncs, hard-links the temporary file to a new target relative to the
  same descriptor without following symlinks, verifies directory inode stability,
  removes the temporary name, and fsyncs the directory;
- never overwrites evidence.

Recursive redaction covers structured keys and free text: environment mappings,
URLs and query strings, Authorization headers, exception and subprocess text,
commands, cookies, passwords, API keys, sessions, Telegram token shapes,
userbot credentials, logins, and credential emails. The only retained
credential schema is: safe credential ID, safe test bot ID, exact SHA-256
fingerprint, `test` class, non-secret provenance, expiry, expiry status, and
authentication status.

Cleanup accepts a `PreflightDecision`, never an arbitrary path. It checks the
exact lexical host run root, complete canonical host topology, runtime UID/GID,
fixed retention base, and run/nonce/attestation ownership marker; denies source,
production, shared, secret, attestation, evidence-retention, ancestor, symlink,
wrong-owner, or unknown paths before deleting a known resource; and removes only
enumerated transient resources. Before the atomic evidence-directory move,
cleanup hard-links the decision/topology-bound ownership marker into the
transient evidence bundle and verifies the link identity, allowed bundle
contents, modes, and owners. Therefore interruption before or after the move
leaves a verifiable retry state. A retry after retention does not require the
transient marker; it verifies the retained bundle and marker. No broad recursive
deletion is available, and final states report either `already_absent`,
`already_absent_evidence_retained`, or retained evidence after transient removal.

## Same-container and legacy-entry deprecation

`obs-agent --test`, `obs-agent --test-instance`, `obs-agent --profile test`, the
same environment profile at CLI/daemon/Telegram factories, and
`scripts/run_parallel_live_smoke.py` now fail before `.env` loading, logging
configuration, HTTP checks, cache proxy startup, daemon construction, credential
discovery, subprocess creation, or Telegram polling. Existing historical live
modules and every direct consumer of their Telegram fixture/launch helpers are
explicitly skipped as deprecated inventory until a later authorized migration
wraps their scenarios behind the isolated runner. Mixed unit/live modules retain
pure unit coverage while their direct-live class is skipped. Pure
`_resolve_profile`, configuration parsing, and non-launching helper tests remain
compatible for unit/library use.

## Later gate sequence

This candidate opens no runtime gate. A later sequence requires custody approval,
fresh static review, trusted host observer implementation, immutable image and
source provisioning, test-only credential setup, negative preflight proof,
authored test execution, non-live lanes, explicitly selected live scenarios,
redacted evidence, independent strict verification, integration, and release
review. Until then every result is `planned_not_run`, never PASS.
