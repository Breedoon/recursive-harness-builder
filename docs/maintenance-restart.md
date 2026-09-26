# Restarting OBS: maintenance restart (default), plain restart (kill switch), crash resume

History: the opt-in maintenance restart was added 2026-09-25 on Daniel's request
(OBS `0c85907`, `4c84f0e`, `5e8a93a`). On 2026-09-26 Daniel asked to make it the
default for planned restarts, with a flag for the plain restart, and to cover
crashes too ("crashes do randomly kill agents like that sometimes"). That is
this version (vault-u3b.70).

## The three ways OBS goes down

- **Maintenance restart — the default for planned restarts.** Agents that are
  mid-turn are snapshotted and resumed automatically after the restart.
  - Telegram: `/restart` (alias: `/maintenance_restart`).
  - Shell, in the OBS container:
    `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart`
    (sends SIGUSR1 to `telegram_main`).
  - Directly: `kill -USR1 <telegram_main pid>` — the Python process, not the
    supervisord wrapper.
- **Plain restart — the kill switch.** Every running CLI dies and **nothing
  resumes**.
  - Telegram: `/restart plain`.
  - Shell: `/workspace/obs/.venv/bin/python -m obs_agent.maintenance_restart --plain`
    (runs `supervisorctl restart obs-telegram-prod` in its own session, so it
    survives when the stop kills the shell it was typed in).
  - `supervisorctl restart obs-telegram-prod` or `supervisorctl stop
    obs-telegram-prod` themselves.
  - `/stop`, `/stop_branch` and `/stop_tree` still stop agents, and **stopped
    agents are never resumed** by any restart or crash.
  - **Keep OBS down for more than 5 minutes** (unplug the server, stop the
    container, `supervisorctl stop obs-telegram-prod` and wait): nothing
    resumes, whatever kind of snapshot was left behind. See the 5-minute
    window below.
- **Crash** (out-of-memory kill, the cache-proxy watchdog, an unhandled exit):
  agents that were mid-turn resume automatically, like a maintenance restart,
  with a note saying OBS restarted unexpectedly.

## How each path is told apart

- The daemon writes `crash-resume.json` next to the Telegram state DB every
  `OBS_CRASH_RESUME_SNAPSHOT_SECONDS` (default 15 s; `0` turns crash resume
  off). It lists every mid-turn route in the same format as the maintenance
  marker. It is written by a timer, not a signal handler, because an OOM kill
  can be a SIGKILL that no handler sees.
- **Kill switch = SIGTERM.** `supervisorctl stop|restart` TERMs the whole
  process group. Two independent guards then discard the crash snapshot:
  1. `telegram_main`'s SIGTERM handler renames it to
     `crash-resume.json.killswitch`, and the process dies with 143 as before.
     It is a Python-level handler, so it runs even when the event loop is busy.
  2. The supervisord wrapper's TERM trap touches the kill-switch sentinel
     (`OBS_KILLSWITCH_SENTINEL`, default
     `/workspace/runtime/state/obs-telegram-prod.killswitch`). The next daemon
     sees it, discards the crash snapshot, and deletes the sentinel.

  A maintenance restart also ends in a group SIGTERM, so both guards fire
  there too. That is fine: the maintenance marker is a separate file and wins.
- **Crash = no SIGTERM to the wrapper.** OOM kills and unhandled exits never
  reach either guard, so the snapshot survives and is resumed.
- **Dead cache proxy.** The wrapper's watchdog used to SIGTERM `telegram_main`
  after three failed `/health` checks. That looked like a restart and resumed
  nothing (the 2026-09-24/25 "crashes", vault-u3b.73). It now sends SIGUSR1, a
  maintenance restart. If `telegram_main` has not exited 30 s later
  (`OBS_WRAPPER_MAINTENANCE_EXIT_WAIT_SECONDS`), it gets SIGKILL and the crash
  snapshot resumes the agents instead.

## On startup

1. The kill-switch sentinel is consumed; if it was present, the crash snapshot
   is discarded.
2. `maintenance-resume.json` and `crash-resume.json` are each **renamed to
   `*.consumed` before anything acts on them**, so a crash loop can never
   replay them.
3. **5-minute window (Daniel, 2026-09-26 11:02Z):** a maintenance marker or
   crash snapshot resumes only if its **last write is at most 5 minutes old**
   when the new daemon starts (`OBS_MAINTENANCE_RESUME_MAX_AGE_SECONDS`,
   default 300 s; was 900 s). The crash snapshot is rewritten every 15 s, so a
   quick crash plus supervisord autorestart still resumes, while a server that
   stayed down longer than 5 minutes (unplugged, stopped) resumes nothing.
   Age = time since the older of the file's `requested_at` stamp and its
   mtime. Files outside the window,
   corrupt files and unknown versions are ignored.
4. A maintenance marker wins; otherwise the crash snapshot is used.
5. Each recorded route gets a new turn in its own topic:
   - A note comes first: "restarted for maintenance" or "restarted
     unexpectedly (a crash…)". It tells the agent that a tool call in flight
     may or may not have taken effect, so it must verify before repeating
     anything. After a crash, the turn may also have finished already; the
     agent then replies with one line and stops.
   - Queued messages are replayed after the note.
   - The normal JSONL health recovery repairs poisoned tails from the kill.
   - **Routes with no session id yet.** The SDK reports a session id with its
     first message, so a turn cut off in its first seconds (or a fork reserved
     but still waiting for its child lock) has no transcript to continue. The
     snapshot still lists such a route (routes are never dropped for lacking a
     session id) and also records the turn's input text (`inflight_prompt`,
     only while there is no session id). The resume then starts a fresh
     session whose note carries that original request instead of "continue".
     Older markers without the field still parse, and older readers ignore it.
6. Fork and AgentTask children get `emit_parent_callback` re-armed, so the
   waiting parent is notified when the resumed child finishes. (Unit-tested;
   not yet observed live — in the first live run the resumed children were
   stopped by `/stop_tree` before finishing; bead vault-u3b.71.)
7. **Late callbacks (vault-u3b.35).** After a maintenance restart or crash (a
   fresh marker or crash snapshot and no kill-switch sentinel), a restored
   child whose persisted status is still `launched`, that has no parent
   callback yet and whose parent is another route, *owes* its launching parent
   a callback (log: `[restore] parent callback owed`) **only if it was cut
   off by this outage** (vault-u3b.81): the previous daemon listed it in
   flight (`inflight_task_ids` in its maintenance marker or crash snapshot,
   i.e. its parent was still waiting), or it was launched after that file's
   `requested_at` (the snapshot's last 15 s). Historical `launched` rows left
   over from older outages are never owed, and no stored row is rewritten.
   Such a child was not resumed (for example it started within the snapshot's
   last 15 s, or the crash-loop guard skipped it). Its next run,
   including an inbox wake, keeps the original parent for that one run. When
   it finishes, the parent gets the normal completion callback as a reply to
   its original launch message (log: `delivering owed parent callback`). The
   debt is cleared when the wake starts, and the finished run persists a
   terminal status, so the callback is delivered at most once. A crash during
   that run keeps the debt, because the record is still `launched` with the
   original parent. Resumed children (step 6) and explicit `AgentTask` resumes
   clear the debt, since they call back anyway. A restored parent without a
   bot uses the primary bot for the callback.
   - **Not owed:** after a kill switch (plain restart, `supervisorctl`, or no
     fresh resume file at all), or when the parent was stopped (`/stop*`
     terminal request on the parent's record, or a stop pending on its route)
     or is gone. Those cases keep the old behaviour: no callback, nothing
     woken.
   - **Scope:** only records restored from the task-handle table are covered.
     A child without team/agent identity is woken only by a message in its
     own topic, which is not an AgentTask run and has no parent callback.
8. **Local-model routes** (`local-*`) resume strictly one at a time: the next
   local route starts only after the previous resumed turn ends, errors or is
   stopped (vault-u3b.86; before 2026-09-26 a 600 s fallback let three local
   turns run concurrently). `OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS`
   (default 21600 s = 6 h) is only a last-resort safety timeout: when it
   expires the chain logs an error and starts the next route without
   cancelling the wedged one; `0` waits without limit. **Hosted routes** start
   `OBS_MAINTENANCE_RESUME_STAGGER_SECONDS` apart (default 3 s).
9. **Crash-loop guard:** a turn already crash-resumed
   `OBS_CRASH_RESUME_MAX_CONSECUTIVE` times in a row (default 2) is not resumed
   again, and the log says so. The count resets when the resumed turn ends.
10. **Outage messages (vault-u3b.35):** after a maintenance restart or a crash,
   unread inbox messages sent since the previous daemon was last alive (minus
   60 s of slack) stay unread and wake their agent. Older ones, and every
   message after a kill switch, are marked read as before.
11. **Orphaned CLIs (vault-u3b.66):** Claude CLIs (the Agent SDK's bundled
    `claude`) with parent PID 1 whose process-group leader is gone get
    SIGTERM, then SIGKILL 5 s later. `OBS_ORPHAN_CLI_REAP=0` turns this off.
    The wrapper now also terminates the rest of its process group when
    `telegram_main` dies on its own, so new orphans should not appear.
12. Trunk and user-facing chats that were mid-turn are resumed like any other
    busy route (supervisor decision 2026-09-25 21:14Z, bead `vault-u3b.41`).
    Use the plain restart or `/stop` if nothing should resume.

## A stop that really stops (vault-u3b.65)

`/stop`, `/stop_branch`, `/stop_tree`, `/stop all`, `/clear`, `/new` and
`AgentTaskStop` mark the running turn as stopped. While it is marked:

- If the CLI exits, OBS ends the turn. It no longer reconnects with "Resume the
  interrupted response", which is what resurrected a stopped router on
  2026-09-26.
- The route is left out of maintenance and crash snapshots.
- If the turn is still running `OBS_STOP_KILL_GRACE_SECONDS` after the stop
  (default 30 s; `0` disables), OBS SIGKILLs that agent's CLI process and
  every tool subprocess it started (not the process group, which is shared
  with the daemon). This covers
  CLIs stuck at 100 % CPU that ignore the interrupt (vault-u3b.76).

The mark is cleared when the route's next turn starts.

## Out-of-memory victim choice

Each Claude CLI child gets `oom_score_adj` 300 (`OBS_CLI_OOM_SCORE_ADJ`;
`0` disables). When the container hits its memory limit, the kernel then kills
one CLI, whose turn reconnects on its own. Before this it killed the cache
proxy, the largest single process, which restarted the whole daemon three
times on 2026-09-24/25 (vault-u3b.73).

## Diagnosing a restart you did not do

- `/workspace/runtime/logs/supervisord.log`: `exited: obs-telegram-prod (exit
  status N; not expected)`. A `supervisorctl stop/restart` also logs
  `stopped:` / `waiting for … to stop`.
- Daemon stderr (`/workspace/runtime/logs/obs-telegram-prod.stderr.log*`):
  `Cache proxy on port 28925 failed three health checks`, and the
  `[crash_resume]`, `[maintenance_restart]`, `[plain_restart]`,
  `[orphan_reaper]` and `[stop_escalation]` lines.
- Host kernel OOM kills: `ssh ubuntu-host 'sudo journalctl -k | grep "Killed
  process"'` (the host journal is in EDT).

## Limits and caveats

- A new mechanism only works once its code is live. The restart that deploys
  this version runs the *old* daemon's snapshot code; the new daemon consumes
  the old maintenance-marker format unchanged (tested against a marker written
  by `19ddec9`).
- The crash snapshot can be up to 15 s old. A turn that ended in those seconds
  is resumed once and told it may already have finished.
- Idle agents need nothing: they are restored and wake on their next message.
- An owed late callback (startup step 7) can arrive long after the parent
  moved on, because a child's record stays `launched` until a run of it
  finishes. That includes children cut off by earlier crashes: on the first
  start of this code, ~65 such prod records existed (2026-09-26 10:10Z,
  read-only count). It is delivered only if the child is woken again, and at
  most once. The parent then gets one extra "went idle" notice, which it can
  ignore. This follows the daemon's over-deliver policy for wakes.
- A turn killed mid-tool-call may have completed its side effect. The resume
  note says to verify first; residual duplicate-effect risk is low, not zero.
- Two daemons sharing one state directory would share these files. Only the
  supervised prod daemon should run against `/workspace/runtime/state`.
- **Rollback:** check out the previous main (`2996397`, bundle
  `/workspace/runtime/artifacts-large/obs-bundles/obs-main-2996397-20260926.bundle`)
  in `/workspace/obs`, put back the previous wrapper from
  `/workspace/runtime/bin/obs-telegram-prod-wrapper.sh.bak-e2-*` by atomic
  rename, then do a plain restart. The old daemon ignores `crash-resume.json`.

## Code and tests

- Module: `src/obs_agent/maintenance_restart.py` (marker format, consume,
  crash snapshot, kill-switch discard, orphan reaper, CLI).
- Daemon, in `src/obs_agent/telegram.py`: `TelegramBot.start_maintenance_resume`,
  `write_crash_snapshot`, `start_crash_snapshot_writer`,
  `request_maintenance_restart`, `request_plain_restart`, `handle_restart`,
  `_mark_stop_requested` / `_escalate_stop`, `_compute_outage_inbox_floor`,
  `_restored_record_owes_callback` / `_owed_callback_deliverable` (late
  callbacks, applied in `_restore_state_from_store` and
  `_start_idle_team_worker_wake`), `_collect_maintenance_entries`
  (`inflight_prompt`), and `_install_kill_switch_sigterm_handler`. No-reconnect-after-stop is in
  `src/obs_agent/runner.py`; the OOM score is in `src/obs_agent/session.py`.
- Wrapper source: `deploy/obs-telegram-prod-wrapper.sh`, installed as
  `/workspace/runtime/bin/obs-telegram-prod-wrapper.sh` by atomic rename.
- Tests: `tests/test_maintenance_restart.py`, `tests/test_crash_resume.py`,
  `tests/test_restore_late_callbacks.py` (late callbacks and session-id
  coverage),
  `tests/test_prod_wrapper.py` (fake daemon; never touches port 28925), and
  the fixture `tests/fixtures/maintenance-resume-19ddec9.json`.
