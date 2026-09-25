# Maintenance restart (opt-in)

Added 2026-09-25 on Daniel's request. It is **not the default**. A plain
restart (`supervisorctl restart obs-telegram-prod`, or the process dying) and
`/stop` / `/stop_branch` / `/stop_tree` remain the kill switch: they resume
nothing.

## What it does

A maintenance restart snapshots every agent that is **mid-turn** at the moment
of the request. It restarts OBS and then resumes those agents automatically.
Each resumed agent is told that its turn was cut off, and that an in-flight
tool call may or may not have taken effect, so it must check before repeating
anything.

## How to trigger it

Pick one:

- **Telegram:** send `/maintenance_restart` in any OBS topic, as an authorized
  user.
- **Shell:** in the OBS container, run
  `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart`. This
  sends `SIGUSR1` to the `obs_agent.telegram_main` process. Use `--pid N` if
  more than one daemon runs. `--status <state-dir>` prints the pending or last
  consumed marker.
- **Directly:** `kill -USR1 <telegram_main pid>`. Signal the Python process,
  not the supervisord wrapper, which does not forward `USR1`.

The daemon then:

1. Writes `maintenance-resume.json` next to the Telegram state DB (the
   directory of `telegram_state_db_path`). It lists every route whose turn is
   running (`busy` or `execution_active`). For each: session id, fork/team
   task id, team and agent names, model, local/hosted flag, JSONL head, and
   queued messages. Routes whose task already carries a `/stop` terminal
   request are left out.
2. Terminates its own process group, the same blast radius as
   `supervisorctl restart`. supervisord's `autorestart=true` brings OBS back.

## On startup

- If `maintenance-resume.json` exists, it is **renamed to
  `maintenance-resume.json.consumed` before anything else**, so a crash loop
  can never replay it.
- Markers older than `OBS_MAINTENANCE_RESUME_MAX_AGE_SECONDS` (default 900 s)
  are ignored. So are corrupt or unknown-version markers.
- Each recorded route gets a new turn in its own topic:
  - The note "restarted for maintenance … verify before repeating" comes
    first.
  - Queued messages are replayed after it.
  - The normal run-start JSONL health recovery applies, so poisoned tails from
    the kill are repaired as for any turn.
- Fork and AgentTask children get `emit_parent_callback` re-armed, so the
  waiting parent is notified when the resumed child finishes. Without this,
  restored children never call back.
- **Local-model routes** (`local-*`) resume **strictly one at a time**. The
  next starts only after the previous resumed turn ends. This avoids
  overloading the single inference server and vLLM preemption.
- **Hosted routes** start staggered by `OBS_MAINTENANCE_RESUME_STAGGER_SECONDS`
  (default 3 s).

## Limits and caveats

- The snapshot is taken by the running process, so the feature only works once
  this code is live. The restart that deploys it cannot use it.
- Idle agents are not in the marker and need nothing: they are restored as
  today and wake on their next inbox message.
- Agents waiting on children are idle, not busy. They are woken by the resumed
  child's callback.
- Messages sent to idle agents *during* the outage are still marked read at
  startup without waking anyone. That is pre-existing behaviour, tracked as a
  bead.
- A turn killed mid-tool-call may have completed its side effect. The resume
  note tells the agent to verify first. Residual duplicate-effect risk is low
  but not zero.
- Sessions persisted before the env-persistence fix (`vault-u3b.20`, OBS
  `62dcda0`) come back once without their explicit AgentTask env.

## Code and tests

- Module: `src/obs_agent/maintenance_restart.py` (marker format, consume,
  ordering, CLI).
- Daemon integration: `TelegramBot.request_maintenance_restart`,
  `start_maintenance_resume`, `_resume_maintenance_entry`,
  `handle_maintenance_restart`, and `_install_maintenance_signal_handler` in
  `src/obs_agent/telegram.py`.
- Tests: `tests/test_maintenance_restart.py`.
