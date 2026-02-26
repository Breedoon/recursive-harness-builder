# Evals Runbook

## Purpose

`tests/evals/` contains end-to-end behavioral validation using real platform adapters
(CLI via pexpect, Telegram via Telethon) and an SDK-based judge.

## Profiles

Scenarios can declare frontmatter metadata:

```yaml
---
lane: deterministic | judge
profiles: smoke, feature, full
first_message_timeout: 45
done_timeout: 90
idle_quiescence_timeout: 20
response_timeout: 240
continuation_timeouts: 30,15
---
```

Profile env var:

- `OBS_EVAL_PROFILE=smoke` -> fast default regression set
- `OBS_EVAL_PROFILE=feature` -> targeted feature loop set
- `OBS_EVAL_PROFILE=full` -> broad sweep

Lane env var:

- `OBS_EVAL_LANE=judge`
- `OBS_EVAL_LANE=deterministic`

Current implementation:

- CLI scenarios can run in either lane.
- Telegram scenarios can run in either lane.

If a profile is requested, only scenarios explicitly tagged with that profile run.

## Commands

```bash
# Default: run Telegram eval scenarios (CLI evals are disabled unless enabled)
.venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Enable CLI eval scenarios explicitly
OBS_EVAL_ENABLE_CLI=1 .venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Smoke profile (recommended default during iteration)
OBS_EVAL_PROFILE=smoke .venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Full profile
OBS_EVAL_PROFILE=full .venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Only judge-lane scenarios in smoke profile
OBS_EVAL_PROFILE=smoke OBS_EVAL_LANE=judge .venv/bin/pytest tests/evals/ -v -m eval --timeout=300

# Only deterministic-lane scenarios in smoke profile
OBS_EVAL_PROFILE=smoke OBS_EVAL_LANE=deterministic .venv/bin/pytest tests/evals/ -v -m eval --timeout=300
```

CLI eval switch:

- `OBS_EVAL_ENABLE_CLI=1` -> include CLI eval scenarios.
- Unset/false (default) -> skip CLI eval scenarios.

## Timeout Tuning

Telegram collection supports per-scenario timeout metadata:

- `first_message_timeout`
- `done_timeout`
- `idle_quiescence_timeout`
- `response_timeout`

These override platform defaults for that scenario only.

## Fixture Isolation

Eval runs use a per-run ephemeral vault copy created from a template fixture.

- Template path defaults to `fixture_vault/`
- Override template with `OBS_EVAL_TEMPLATE_VAULT=/abs/path`
- Optional template-clean guard: `OBS_EVAL_REQUIRE_CLEAN_TEMPLATE=1`
- Real vault safety guard can be configured with `OBS_REAL_VAULT_PATH`

Refresh the template explicitly:

```bash
scripts/refresh_fixture_vault.sh
# or
OBS_REAL_VAULT_PATH=/abs/path/to/real/vault scripts/refresh_fixture_vault.sh
```

## Authoring Checklist

Before adding/updating any eval scenario:

- Provide at least two concrete criteria; avoid trivial criteria like "responds".
- Add an explicit broken-mode thought process:
  - What realistic failure should this catch?
  - What suspicious behavior should still be called out even if criteria pass?
- Judge-lane scenarios must include an `Intent` section with broken/suspicious examples.
- Deterministic-lane scenarios must have at least one negative/mutation-style assertion path in tests.
- If introducing long waits, justify them and prefer metadata timeout tuning over blanket sleeps.
