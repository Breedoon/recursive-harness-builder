# Cold-notice model-free E2E scaffold

These scenarios execute the OBS identity extracted with `git archive`, the real `TelegramBot`, the real background and inbox pollers, real tool handlers, and the installed SDK and bundled Claude CLI. Only Telegram transport and the Anthropic Messages API are scripted. No provider credentials or model spend are used.

## Running

Use the host-side Python entry point from the RUOI worktree:

```bash
python3 /workspace/obs/worktrees/obs-22-ruoi/scripts/e2e_cold_notice/run_scenario.py \
  --identity RP --sha 6b3136cf02c8655e8bedc45f371e9bb579851dd6 --scenario 2 \
  --ledger /absolute/path/to/assigned/actor/runs.md
```

Each invocation creates a unique persistent run directory at `/workspace/runtime/e2e/ruoi/<run_id>`, extracts the chosen commit there, and uses the existing helper at `/workspace/obs/worktrees/obs-22-e1/tests/e2e/harness/docker.py` to start the existing `obs-e2e:b4f1276048e1` image. A missing image is an error; this scaffold never rebuilds it.

The three mounts are the run directory read-write at `/run/ruoi`, `/workspace/obs/.venv` read-only at its same absolute path, and this driver directory read-only at `/drivers`. The network is `none`; the only interface must be loopback. No live state, vault, logs, user credentials, proxy, Docker socket, or production environment file is mounted. All daemon configuration comes from a synthetic environment. The stub is loopback-only, and all state, HOME, control authentication, replay and transcript data are run-local. Code execution, including external control invocations, occurs only inside this container. Cleanup is `rm_run` for the invocation's own unique run id.

Run a named list sequentially with `run_matrix.py --identity RP --sha <sha> --scenarios '2,3,3b-i' --ledger <runs.md> --index <index.jsonl>`. The matrix has no default scenario list, retries, concurrency or expected-result-driven tuning. A harness failure stops it; a behavioral finding remains an observation. Record any permitted harness rerun with `run_scenario.py --rerun-reason 'specific cause'`.

For S6 also specify `--control-site rounds-cleanup|rounds-failure|post-reboot`. The source defaults to the actual vault scripts. `--control-source-dir <directory>` selects owner-provided patched copies for fixed-code proofs. The driver parses the named function's argv with `ast`, evaluates only its data-producing expressions against a recorded deterministic fixture, and never imports or executes the operational script. Only binary, team and recipient are replaced; the original flags and data-producing expressions are recorded. The control client is the snapshot's `python -m obs_agent.control`, using the real daemon's run-local Unix socket and token.

`AP` runs require the owner's explicit fixed commit; this driver does not choose or infer it. The same driver files, scenario policy and data fixtures are used on both identities; every file hash is in the manifest.

## Scenario matrix

- `1`: per owner Amendment 1, child A sleeps 60 s, then its next Messages request receives an intentional non-retryable HTTP 400 `invalid_request_error`. Record the genuine terminal status. If OBS treats that as idle instead of failed, the owner permits a separate `--s1-mode stop` run: with the parent cold, invoke the snapshot's real external-control stop on the exact running child generation. The initial stop-mode parent trigger carries a fake-transport Telegram message id for the real generation binding. No timeout field or task-record value is injected; production deliberately disables launch timeouts.
- `2`: child A sleeps 45 s, then completes with an idle notice to an idle parent.
- `3`: child A sleeps 45 s; parent calls real `AgentTaskOutput(block=true, timeout=120000)` in its still-running turn.
- `3b-i`: children A/B sleep 35/80 s; parent ends its launch turn; both notices are observed independently.
- `3b-ii`: children A/B sleep 25/70 s; parent blocks on B while A's notice arrives.
- `4`: child A sleeps 5 s, inside the 20 s cache-readiness TTL.
- `5a`: sender's real `SendInboxMessage`, cold parent physically idle over 25 s, no override.
- `5b`: same tool, warm parent.
- `5c`: same tool, cold parent with the unchanged explicit override.
- `6`: one exact source argv site against a cold parent; three separate runs for the three source sites.
- `7-human`: fixed-only S2 shape; at least 30 s and five measured real poller cycles without a parent request, then a real `TelegramBot.handle_message` update makes the stub call `ReadInbox` twice.
- `7-inbox`: same fixed-only proof, but the next wake is a real sender `SendInboxMessage(allow_cold_wake=true)`.
- `8`: sender sends to a real child through the real tool, becomes cold, then the daemon calls real `_send_bounce_backs_for_dead_agent` on that child's route. This explicit invocation exercises the bounce producer and real inbox poller without deleting a topic or running a kill path.

Per owner Amendment 2, the fixture team is `2026-10-22-18-00-ruoi-e2e` on both identities and in bootstrap, routes, environment and inboxes. The originally requested short team name is rejected by production task-lookup authorization; it was not replaced by weakening that authorization. Early qualification runs using the short name remain recorded but are not blocked-parent proofs.

The real installed SDK advertises `AgentTaskOutput`'s simple-dictionary schema as required string properties. The stub follows the actual request's wire schema, supplies empty mutually exclusive stable-identity fields for the legacy task-id path, and lets the production handler coerce block/timeout. Native handler results are observed read-only by the Python profiler, separately from the text actually seen by the model. Schema-validation and no-task responses are harness failures, never substitutes for a real wait.

Parents and senders are actual root routes with explicit run-local team/inbox projections. Their initial input enters `_run_and_send` under the real route lock, the initial entry point permitted by the scenario specification. Children are launched by the parent CLI's real `AgentTask(fork=false)`, not injected launch results. The surfacing human wake enters the actual Telegram message handler with a real `telegram.Update` and recording transport.

## Evidence fields

`manifest.json` records the identity SHA, driver hashes, image id, helper identity, run id, container name, start UTC and any control-source provenance. `container-inspect.json` records the actual isolation flags and mounts. `summary.json` adds end UTC, native exit status, observations, tool results and relevant log lines.

- A notice observation is sampled by a read-only done callback attached to the real child task. It records task id, child terminal status, actual callback notice status, error and the code's completion time. Delivery preflight is independently sampled by a read-only logging handler.
- Each readiness sample calls the code-under-test's `_state_cache_readiness`. It records cold/family/TTL/idle/reason and independently records the last assistant-record time and physical elapsed idle. Running parents are warm by the production readiness rule even when physical idle exceeds the TTL.
- Parent/sender request counts after an event and request byte lengths come from the recording API stub. Each observation ends at the next explicit wake/message/notice boundary or the run's final observation, so a later legitimate surfacing wake is not misattributed to a held notice. Session JSONL row counts/types/timestamps are sampled before and after. Stub request log rows also record tool calls returned, tool results actually seen by the CLI, short sanitized text digests, request hashes and any held-notice hint lines.
- Stub cache-creation numbers are synthetic zero and **not billing evidence**. There is no claimed real cache bill or cache-hit ratio. Request counts and sizes prove whether a model request was initiated.
- Inbox rows are projected as from/from_team/summary/read/wake/timestamp; long message bodies are not copied into the summary. `wake: null` means the original row did not contain that optional field, not that it was false.
- Real poller cycles are observed through a read-only Python call profiler, not by invoking or replacing a poller. Their timestamps and measured intervals are recorded, including the fixed-only no-wake interval.
- Relevant daemon lines include auto-delivery, route preflight, the held marker and the blocked-inbox-wake marker. These distinguish queued delivery from model requests and inline running-turn continuation.

No scenario outcome is substituted for the lane's independent verification or its release gate. This scaffold neither edits production code nor commits or deploys anything.
