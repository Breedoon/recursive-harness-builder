# External control channel (`obs-agentctl`, bead vault-h667)

Run from bash what an agent does with its tools, so deterministic jobs (cron, error monitors,
the reflection watcher) can wake or launch agents without an agent in the loop.

```text
obs-agentctl --as error-monitor ping
obs-agentctl --as error-monitor message --team <root_team_key> --to <agent_name> --text "..." [--summary ..] [--allow-cold-wake]
obs-agentctl --as nightly-check launch --parent-team <team> --parent <agent> --name "Fix X" --prompt "..." [--fresh] [--model ..] [--prompt-file ..] [--hooks '{..}'] [--session-source ..] [--allow-cold-wake]
obs-agentctl --as nightly-check new-tree --title "Disk alert" (--prompt "..." | --prompt-file P) [--chat NAME|ID | --chat-id N] [--model ..] [--effort ..] [--hooks '{..}'] [--env '{..}']
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
- `new-tree` creates a new root tree: a new topic in an **existing** group chat, like a person
  starting a topic (`new_trunk` bootstrap, a fresh `root_team_key`), and runs the prompt as its
  first turn (obs-15.5 / ops-61 / ops-1.11.6).
  - Chat: `--chat` takes a chat id or a name from the chat registry
    (`Projects/Personal Projects/Agentic/Agent Operations/chat-registry.json` in the vault; override
    the path with `OBS_CHAT_REGISTRY`; re-read on every call). `--chat-id N` still works. With no
    chat given, the only forum chat OBS knows is used, otherwise the call is refused with the list.
    A chat must be one OBS already has topics in or one the registry declares. **OBS never creates
    a chat**; an unknown chat is refused before any topic is created.
  - Launch parity with AgentTask: `--prompt-file` (wrapped in `<prompt_file_context>` as for
    AgentTask), `--model`, `--effort`, `--hooks` (replace the default new-chat hooks; omitted keeps
    them; the launch guard's role-hook rules are the caller's to follow), `--env`.
  - Result (`--json`): `chat_id`, `chat_name`, `thread_id`, `link`, `root_team_key` (= `team_name`)
    and `agent_name` of the new root, so the caller can message it directly.
  - Provenance: the launcher is recorded in `~/.claude/teams/<root_team_key>/origin.json`
    (`kind: external, caller`). The new tree is not a lineage child of anyone.
- Agents can do the same without Bash: `AgentTask(new_tree_chat="<chat name or id>",
  display_name="<topic title>", prompt=..., prompt_file=..., hooks=..., model=..., effort=..., env=...)`.
  It is always a fresh root (fork, resume and session_source are refused); an omitted model means
  the caller's own model. The caller is recorded as `origin_team`/`origin_agent` in `origin.json`,
  and the first turn names it with the `SendInboxMessage team_name=…` to report completion to.
  Adding `new_tree_chat` to the AgentTask schema changed every session's tool list, so the deploy
  that ships it breaks each live session's cache once (docs/cache-proxy-safety.md); undeclared
  AgentTask arguments are dropped before OBS sees them (probed 2026-10-20), so it had to be declared.
