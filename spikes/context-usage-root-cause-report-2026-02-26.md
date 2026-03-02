# Context Usage Root-Cause Report (2026-02-26)

## Scope

This report investigates unreliable context usage reporting in OBS Agent, with focus on the user-observed pattern:

- tool-heavy turn reports a large jump in "context used"
- subsequent simple text turn reports a much lower number

Per request, analysis and spikes were scoped to:

- `/Users/breedoon/Documents/obs`
- `/Users/breedoon/Documents/obs/fixture_vault`
- corresponding JSONLs under `~/.claude/projects/-Users-breedoon-Documents-obs*`

I explicitly avoided running new spikes against the main Obsidian vault workspace (`...iCloud.../Documents/T`) as an execution target.

---

## Executive Summary

Primary conclusion:

1. `ResultMessage.usage` from Python SDK is not a stable "current context occupancy" signal.
2. On tool-heavy turns, `ResultMessage.usage` can represent multi-iteration aggregate usage (`num_turns > 1`) and inflate sharply.
3. The next simple turn often reports much lower usage (`num_turns = 1`), which looks like a "reset" even when session is continuous.
4. JSONL assistant-entry usage is more stable for context estimation because it reflects per-request snapshots rather than aggregated turn loops.

Actionable conclusion:

- For `/context`-style occupancy estimates, prefer JSONL-derived assistant usage snapshots (with same-session filtering) over raw Python SDK `ResultMessage.usage`.
- Keep SDK result usage for billing/telemetry, not occupancy.

---

## Questions Answered

### Q1: Do ancestry-linked drops exist in JSONL across all files?

Yes, but the magnitude depends heavily on comparison rules.

Early broad scan (all projects, nearest nonzero ancestor, no session/model filtering) found drops.

### Q2: Are big drops mostly artifacts from crossing session boundaries?

Yes.

When constrained to same `sessionId` and same model, large drops nearly disappear in OBS scope and disappear in fixture scope.

### Q3: Did session `caddbf09-e929-4f86-b6b6-6fb946ae770a` itself show ancestry drops?

No. That session does not show nearest-ancestor drops in JSONL.

### Q4: Can we reproduce the "tool-heavy inflate, text-turn drop" with SDK?

Yes.

A controlled Python SDK spike in fixture scope reproduced this pattern clearly.

---

## Data Sources

- JSONL logs under `~/.claude/projects/**.jsonl`
- TS SDK probes (`@anthropic-ai/claude-agent-sdk`) in `/Users/breedoon/Documents/obs/spikes`
- Python SDK probes (`claude_agent_sdk`) run from `/Users/breedoon/Documents/obs`

---

## Methodology and Results

## 1) Global JSONL ancestry scan

### 1.1 Immediate parent-only scan (initial)

Initial script compared only immediate parent-child where both sides had usage.

Result:

- Very few non-equal pairs in that narrow mode
- This was too restrictive for the intended analysis

Correction made:

- Traverse ancestry to nearest prior usage ancestor (not just immediate parent)

### 1.2 Nearest-ancestor scan (all projects)

Using all JSONLs and nearest nonzero ancestor comparisons:

- files: `6742`
- usage nodes: `57476`
- nearest edges: `51078`
- drops: `300`
- rises: `22511`
- equal: `28267`

After excluding zero-valued usage comparisons:

- edges: `50903`
- drops: `235`
- rises: `22427`
- equal: `28241`
- files with drops: `202`

Directory distribution for those `202` files (top buckets):

- `-Users-breedoon-Documents-obs`: `63`
- `-Users-breedoon-Library-Mobile-Documents-iCloud-md-obsidian-Documents-T`: `52`
- `-Users-breedoon-Documents-obs-fixture-vault`: `39`

Interpretation:

- Drops are real in ancestry terms, but this mode still mixes session boundaries.

---

## 2) Session-aware rerun (strict filtering)

I reran analysis with explicit filters:

- `any`: nearest nonzero ancestor (baseline)
- `same_sid`: nearest ancestor with same `sessionId`
- `same_sid_model`: nearest ancestor with same `sessionId` and same model

### 2.1 All projects

- files: `6751`
- drops_any: `244` (`34` >=20k)
- drops_same_sid: `83` (`9` >=20k)
- drops_same_sid_model: `79` (`7` >=20k)

### 2.2 OBS only (`-Users-breedoon-Documents-obs`)

- files: `1488`
- drops_any: `74` (`24` >=20k)
- drops_same_sid: `17` (`1` >=20k)
- drops_same_sid_model: `15` (`0` >=20k)

### 2.3 Fixture only (`-Users-breedoon-Documents-obs-fixture-vault`)

- files: `500`
- drops_any: `39` (all 5k-20k)
- drops_same_sid: `0`
- drops_same_sid_model: `0`

Interpretation:

- Large scary drops are mostly cross-session/model boundary artifacts.
- In fixture scope, same-session drops vanish completely.

---

## 3) Specific session checks

### 3.1 User-referenced session: `caddbf09-e929-4f86-b6b6-6fb946ae770a`

File:

- `/Users/breedoon/.claude/projects/-Users-breedoon-Library-Mobile-Documents-iCloud-md-obsidian-Documents-T/caddbf09-e929-4f86-b6b6-6fb946ae770a.jsonl`

Result:

- `usage_nodes=15`
- `edges=14`
- `drop=0`
- `rise=9`
- `equal=5`

Interpretation:

- No ancestry drop in this exact session JSONL.

### 3.2 Why `42b3df98...` looked like a huge drop

In `/Users/breedoon/.claude/projects/-Users-breedoon-Documents-obs/42b3df98-922d-4fba-bcd8-614891d29583.jsonl`:

- line 49 assistant (`uuid=4328...`):
  - `sessionId=5f13...`
  - model `claude-opus-4-6`
  - sum `57277`
- line 51 assistant (`uuid=1a52...`):
  - `sessionId=42b3...`
  - model `claude-haiku-4-5-20251001`
  - sum `35780`

These are ancestry-linked via `parentUuid`, but belong to different runtime session IDs and different models.

Interpretation:

- Valid lineage bridge, invalid same-session occupancy comparison.

---

## 4) TS SDK before/after probes (anchor-based)

I forked from specific anchor UUIDs and captured TS SDK `result.usage` and `/context`.

### 4.1 Session `b1922454...`

JSONL breakpoint: `20876 -> 20477`

TS fork at before UUID `e197...`:

- total `20283` (`10 + 6298 + 13975`)
- `/context`: `20.3k / 200k`

TS fork at after UUID `0ed2...`:

- total `20598` (`10 + 814 + 19774`)
- `/context`: `20.6k / 200k`

### 4.2 Session `70f836b3...`

JSONL breakpoint: `20848 -> 20635`

TS fork at before UUID `6ded...`:

- total `20212` (`10 + 4285 + 15917`)
- `/context`: `20.2k / 200k`

TS fork at after UUID `557e...`:

- total `21298` (`10 + 5371 + 15917`)
- `/context`: `21.3k / 200k`

### 4.3 Session `42b3df98...` (cross-session artifact case)

TS fork at `4328...`:

- total `33247` (`10 + 17320 + 15917`)
- `/context`: `33.2k / 200k`

TS fork at `1a52...`:

- total `33260` (`10 + 17333 + 15917`)
- `/context`: `33.3k / 200k`

Interpretation:

- TS-probed values are stable across these anchors; no giant true occupancy jump/drop in this case.

---

## 5) Reproducing the SDK inflate/drop pattern (fixture scope)

Goal:

- reproduce user-observed shape: tool-heavy turn inflates usage, next simple text turn drops.

### 5.1 Setup

Because `caddbf09...` belongs to main vault project scope, direct resume from fixture cwd fails.

Workaround used:

- copied single JSONL file into fixture project scope:
  - from: `/Users/breedoon/.claude/projects/-Users-breedoon-Library-Mobile-Documents-iCloud-md-obsidian-Documents-T/caddbf09-e929-4f86-b6b6-6fb946ae770a.jsonl`
  - to: `/Users/breedoon/.claude/projects/-Users-breedoon-Documents-obs-fixture-vault/caddbf09-e929-4f86-b6b6-6fb946ae770a.jsonl`
- TS-forked it in fixture cwd to:
  - `3385c925-d1d3-4e29-98d4-cc13e23a211a`

### 5.2 Python SDK controlled run

Run in `/Users/breedoon/Documents/obs/fixture_vault`.

A direct continuation from that fork did not produce huge jump/drop on one attempt, but deeper controlled run did.

### 5.3 Strong reproduction

Using Python SDK client with prompts:

1. simple text
2. tool-heavy three-file read
3. simple text

Observed `ResultMessage.usage`:

- turn 1 (`num_turns=1`): total `20884`
- turn 2 tool-heavy (`num_turns=6`): total `89315`
- turn 3 text (`num_turns=1`): total `23320`

This reproduces the inflate/reset shape.

### 5.4 Message-level check (same turn)

In session `517ad441-99ee-4153-9470-5e7cc806364b`:

- tool-heavy turn `ResultMessage.usage` total: `43109` (`num_turns=4`)

But JSONL assistant entries for same session showed max:

- `22196`

So SDK result usage exceeded per-request assistant snapshot by ~`20.9k` in that turn.

Interpretation:

- `ResultMessage.usage` is aggregating multi-iteration turn usage and is unsuitable as direct occupancy metric.

---

## Why this happens (root-cause hypothesis)

Most likely behavior:

1. A tool-heavy turn involves multiple internal request iterations.
2. Python SDK `ResultMessage.usage` reflects aggregated usage over these iterations.
3. Next plain turn has one iteration; usage collapses to a lower single-request level.
4. If treated as "context used right now," this appears as impossible drop/reset.

Supporting evidence:

- High `num_turns` coincides with spikes.
- Same-session same-model large drops mostly vanish in JSONL assistant-entry comparisons.
- Tool-heavy reproduction produced `89315` then `23320` without session reset.

---

## Reliability assessment of candidate signals

### A) Python SDK `ResultMessage.usage`

- Good for: billing/turn-level telemetry
- Bad for: stable occupancy estimate
- Failure mode: multi-iteration inflation

### B) JSONL assistant entry usage triplet (`input + cache_creation + cache_read`)

- Good for: per-request snapshot, bounded and stable relative behavior
- Better occupancy proxy than ResultMessage aggregate
- Caveat: still a proxy, not exact true window occupancy

### C) `/context` CLI

- Useful external cross-check when available
- In nested execution contexts can be blocked/noisy (observed during in-process script calls)

---

## Recommendation

For `/context` and MCP `context_info` logic:

1. Primary source for occupancy estimate:
   - JSONL assistant snapshots from current session scope
   - same-session filtering
2. Suggested reported metrics:
   - `latest_request_triplet_total`
   - `recent_peak_triplet_total` (e.g., last N assistant entries)
   - optional `session_high_water_triplet_total`
3. Keep SDK `ResultMessage.usage` separately labeled as turn/billing telemetry.
4. Never label aggregated SDK turn usage as "current context used".

---

## Limitations

1. Some direct `/context` subprocess checks from inside this Codex runtime returned nested-session refusal text; those checks were treated as non-authoritative for the scripted loops.
2. TS SDK probing is limited to what SDK exposes in emitted messages and fork behavior under current runtime.
3. This report does not change production code; it documents findings and reproducible evidence.

---

## Repro pointers

Key files:

- report source session: `/Users/breedoon/.claude/projects/-Users-breedoon-Library-Mobile-Documents-iCloud-md-obsidian-Documents-T/caddbf09-e929-4f86-b6b6-6fb946ae770a.jsonl`
- reproduced fixture sessions:
  - `/Users/breedoon/.claude/projects/-Users-breedoon-Documents-obs-fixture-vault/3385c925-d1d3-4e29-98d4-cc13e23a211a.jsonl`
  - `/Users/breedoon/.claude/projects/-Users-breedoon-Documents-obs-fixture-vault/517ad441-99ee-4153-9470-5e7cc806364b.jsonl`

TS spike scripts used:

- `/Users/breedoon/Documents/obs/spikes/ts_fork_message_types.mjs`
- `/Users/breedoon/Documents/obs/spikes/ts_fork_worker.mjs`

Python SDK runs were executed via inline `uv run python` in `/Users/breedoon/Documents/obs`.

