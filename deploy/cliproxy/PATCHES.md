# Local CLIProxyAPI patches

The running `cli-proxy-api` is NOT a stock release. Re-apply these patches (or
confirm upstream contains them) before any upgrade, then follow
/workspace/obs/docs/cache-proxy-safety.md (key probe + full cache_regression.py).

**Before any swap, upgrade or restart, check for an active shared-service freeze** (bead ops-47.6):
`python3 "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops/freeze.py" check --command "supervisorctl restart cliproxyapi"`
Exit 3 means a freeze covers it: coordinate with the freeze owner (normally the Work Registry
root) instead. The 2026-10-18 13:25Z gvnf swap below was made during a declared freeze and
invalidated another tree's qualification gate. `cliproxy-watchdog.sh` now skips its 429 and
catalog-miss restarts during a freeze; it still revives a dead proxy, with an alert.

## 8.0.11-gvnf2 (deployed 2026-10-20 ~13:13Z, bead ops-65, P0) — RUNNING

- Binary sha256: `7e6cb092bd13f9bdd3f2f4a942157787d3f6fd3bc0990b6aceeeed765a70b5c4`
  (`cli-proxy-api` and `cli-proxy-api.8.0.11-gvnf2`; verified against `/proc/<pid>/exe` after the swap).
  Rollback: `cli-proxy-api.8.0.11-gvnf1` (sha `0e791c16…`), then `supervisorctl restart cliproxyapi`.
- Base: gvnf1 (below) plus source branch `ops65-replay-cap`, commit `44c62cb8`, in
  /workspace/runtime/src-cache/CLIProxyAPI. Build exactly like gvnf1 with
  `-X main.Version=8.0.11-gvnf2 -X main.Commit=44c62cb8`.
- Bug: the Codex reasoning-replay store trimmed each agent's turns FIFO at 256 turns / 16 MiB.
  Every completed response is a stored turn, so a long agent passed 256 within hours and the
  oldest turns were evicted, INCLUDING orphan turns (reasoning + empty answer) that the client never
  echoes back and that gvnf1 re-inserts at a fixed early index. The orphan then vanished from the
  translated prompt and the provider cache missed for the whole rest of the conversation once
  (2026-10-19: 0.28–0.57M-token re-bills, e.g. item 231 of 973).
- Fix: caps raised to 8192 turns / 256 MiB per agent (the 1 h TTL and 10,240-entry cap still bound
  memory). Test `TestCodexReasoningReplayOrphanSurvivesLongConversation` FAILs at 256 and PASSes
  patched; all 100 packages pass.
- Gate: key probe PASS (scratch candidate on port 18318, then live); full dual-route
  `cache_regression.py` PASS 37/37 against a scratch cache proxy pointed at the candidate,
  report `/workspace/runtime/cache-regression/20261020T1340Z-ops65-gvnf2-scratch` (CLIProxy
  identity `53d965f5…` = this binary + the production config).
- Known cost of ANY CLIProxyAPI restart: the replay store is in process memory, so live sessions
  that depend on an inserted orphan lose it once (bead vault-j3ms tracks persisting it).
- Diagnosis of what remains (provider routing misses, Claude Code tool-result persistence):
  obs-artifacts `Drafts/Artifacts/2026-10-17-11-36-proc-work/b41d7e9d27-p0-sol-cache-re-bills-ops-65/report.md`.
  Detector now reads the gz archive: `cliproxy_translation_drift.py --log-dir /workspace/runtime/backups/cliproxy-logs`.

## 8.0.11-gvnf1 (deployed 2026-10-18 ~13:25Z, bead vault-gvnf, P0) — superseded by gvnf2

- Binary sha256: `0e791c16fb04f97490719ebcd922fe8d40b3fffc933e7b4c79b3401e9bbfa85a`
  (`/workspace/runtime/cliproxy/cli-proxy-api` and `cli-proxy-api.8.0.11-gvnf1`; verified against the
  running process `/proc/<pid>/exe` on 2026-10-20, obs-15 re-check 2 N7). The stock rollback
  `cli-proxy-api.v8.0.11.bak` is `69099598c2ee14ba…`. Check before trusting a running proxy:
  `sha256sum /proc/$(pgrep -x cli-proxy-api)/exe`.
- Base: upstream commit e2bff0107bb3 (v8.0.11), source clone
  /workspace/runtime/src-cache/CLIProxyAPI branch `gvnf-replay-anchor`, commit 7858f4be.
- Patch: patches/0001-codex-replay-anchor-fingerprint-less-turns-after-the.patch
- Bug: the Codex reasoning-replay cache re-inserted a fingerprint-less replay
  turn (reasoning + EMPTY final_answer message, typical after an interrupted or
  retried turn, e.g. around OBS restarts) "before the last assistant message".
  That index moves every turn, so the translated upstream prompt stopped being
  an append-extension and the provider cache fell back to the old insertion
  point on every new assistant turn (production: one Sol supervisor reread only
  82,176 of ~120-188k tokens on 32 turns in 100 minutes; the 20,736 partial reads
  of 2026-10-17 were the same mechanism with no assistant message yet). The cache
  proxy's own request log could not show it: the Anthropic-shaped wire was a pure
  append; only CLIProxyAPI's translated input changed.
- Fix: anchor such turns right after the request prefix they were produced for.
- Build: Go 1.27.2 at /workspace/runtime/toolchains/go, CGO_ENABLED=1,
  -ldflags "-s -w -X main.Version=8.0.11-gvnf1 -X main.Commit=7858f4be ...".
- Rollback: cli-proxy-api.v8.0.11.bak (stock), then `supervisorctl restart cliproxyapi`.
  Swapping the binary or restarting the proxy is blocked during an active `cliproxy`/`supervisor`/`all-shared`
  freeze (vault `Agent Operations/scripts/ops/freeze.py status`); the freeze hook resolves `cd` chains,
  relative paths and variables, so coordinate with the freeze owner instead of rephrasing the command.
- Canonical copy: OBS repo `deploy/cliproxy/PATCHES.md` (installed to `/workspace/runtime/cliproxy/PATCHES.md`).
- Detector for the translated side:
  /workspace/runtime/cliproxy/tools/cliproxy_translation_drift.py (run with /workspace/obs/.venv/bin/python; exit 1 = replay-caused breaks)
