# External control channel (`obs-agentctl`, bead vault-h667)

Run from bash what an agent does with its tools, so deterministic jobs (cron, error monitors,
the reflection watcher) can wake or launch agents without an agent in the loop.

```text
obs-agentctl --as error-monitor ping
obs-agentctl --as error-monitor message --team <root_team_key> --to <agent_name> --text "..." [--summary ..] [--allow-cold-wake]
obs-agentctl --as nightly-check launch --parent-team <team> --parent <agent> --name "Fix X" --prompt "..." [--fresh] [--model ..] [--prompt-file ..] [--hooks '{..}'] [--session-source ..] [--allow-cold-wake]
obs-agentctl --as nightly-check new-tree --title "Disk alert" --prompt "..." [--chat-id N]
```

- Wrapper: `/workspace/runtime/bin/obs-agentctl` = `/workspace/obs/.venv/bin/python -m obs_agent.control`.
- Exit 0 done, 1 refused or failed (reason printed), 2 cannot reach or authenticate to OBS.
- `--json` prints the full response; `--idempotency-key K` returns the earlier result for a repeated key
  (until the next OBS restart).

## How it is reached and who may call it

- A Unix socket owned by the running bot: `/workspace/runtime/state/obs-control/control.sock`
  (override `OBS_CONTROL_DIR`; `OBS_CONTROL_DISABLE=1` turns the listener off). No TCP port.
- Callers must run as the OBS user on this host (directory 0700, socket 0600, `SO_PEERCRED` uid
  check) and present the token in `obs-control/token` (0600, new at every bot start; the CLI reads
  it). From another machine, SSH into the `obs-test` container (port 2220) and run the CLI there:
  - from the Ubuntu host: `ssh -p 2220 agent@127.0.0.1 '/workspace/runtime/bin/obs-agentctl --as NAME ping'`
    (verified 2026-10-16). The host's `breedoon@breedoon-ubuntu-host` key, ED25519
    `SHA256:sIH80NjEmhl4rHBqp4TyPEVHqfMFnzpkQaaSMbFAPE0`, was appended to the host file
    `/data/config/authorized_keys`. That file is bind-mounted read-only into obs-test,
    obs-transcription, video-gen and lobby, so host user `breedoon` has SSH as `agent` in all four.
    Backup: `/data/config/authorized_keys.bak-20261016-d2d`. It persists across container restarts
    and re-creation, because the bind source is a host file that Docker re-binds at every start.
    Append in place (same inode); a replaced file is only seen after a container restart;
  - over Tailscale: `ssh -p 2220 agent@100.66.160.62 '...'` with a key in that same file.
- Global flags (`--as`, `--json`, `--idempotency-key`) may go before or after the op name (ops-1.11.5). `--as` is required.
- Every request, accepted or refused, is appended to `obs-control/audit.jsonl`.
  - Outcomes: `ok`; `cold_blocked` when the cold-cache guard (vault-vokl) refused a message or a
    fork (ops-1.11.13); `failed` when the dispatched action did not happen for any other reason;
    `refused` only for an authentication failure (peer uid or token); `invalid` for a malformed
    request; `replayed` for an idempotency hit. Entries written before the ops-1.11.13 deploy
    log cold refusals as `failed` with a `cold-cache guard: …` detail.
  - Retention: append-only and **never rotated or auto-deleted** (Daniel's standing never-auto-delete
    rule). It grows about 368 bytes per call (12,981 bytes for 35 calls on 2026-10-16).
  - The file is mode 0644 inside the 0700 `obs-control` directory, so only the OBS user can reach it.
    It records op, caller, outcome, target names and a 300-character detail. It holds no token and no
    message or prompt bodies.

## What runs

- `message` and `launch` call the same SendInboxMessage / AgentTask handlers agents use
  (`tools.create_obs_tools(return_tools=True)`), so the cold-cache guard (vault-vokl), the
  start-step check (vault-a6qm), lineage, teams, Telegram topics and session_source model rules apply
  unchanged. Messages come from `external:<name>`; launched prompts start with
  `[external trigger: <name>]`.
- An agent may reply to an `external:<name>` sender with SendInboxMessage. External callers have no
  route, so the reply is appended to `<control dir>/external-replies/<name>.jsonl` (one JSON row:
  `timestamp`, `team_name`, `from`, `to`, `summary`, `text`) and the tool returns
  `outcome: external_reply`. No agent is woken. Before obs-12 such replies failed with "recipient has
  no current route binding".
- `launch` acts as the named parent agent calling AgentTask; the child appears under that parent.
  Forking a parent whose prompt cache has gone cold is refused like any cold wake unless
  `--allow-cold-wake`.
- `new-tree` creates a new root topic like a person starting a topic (default new-chat hooks,
  `new_trunk` bootstrap) and runs the prompt as its first turn. With one forum chat known it is used;
  otherwise pass `--chat-id`.
