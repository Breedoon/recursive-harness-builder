# W&B Weave Tracing

Weave tracing is optional. When enabled, every conversation turn and fork run is logged to the [Weave UI](https://wandb.ai/home) with token usage, cost, duration, tool calls, and agent identity (lineage, session_id, parent_session_id).

## Setup

### 1. Install the tracing extra

```bash
uv pip install -e ".[tracing]"
```

### 2. Authenticate (skip if you already have an API key saved)

If you've never used W&B on this machine, run:

```bash
uv run wandb login
```

This saves your API key to `~/.netrc`. Alternatively, set `WANDB_API_KEY=<your-key>` in your `.env` instead of running the login command.

The W&B project is created automatically on first trace if it doesn't exist.

### 3. Set the environment variable

In your `.env` file (copied from `env.example`):

```
WEAVE_PROJECT=your-entity/obs-agent
```

Replace `your-entity` with your W&B username or org name. The project name (`obs-agent`) can be anything — Weave creates it automatically if it doesn't exist.

## Running

Start the agent normally — tracing activates automatically when `WEAVE_PROJECT` is set:

```bash
uv run obs-agent
```

On startup you'll see a log line:

```
INFO obs_agent.tracing: Weave tracing enabled: your-entity/obs-agent
```

A link to the Weave UI is also printed by the Weave SDK itself.

## What gets traced

| Op name | Triggered by | Inputs | Outputs |
|---|---|---|---|
| `obs_agent/_drive_turn` | Every `/chat` request | `message`, `session_id`, `model` | `response`, `tool_uses`, `cost_usd`, `duration_ms`, token counts |
| `obs_agent/conversation_turn` | Every `/chat/stream` request | same + all output fields | same output dict |
| `obs_agent/_fork_run` | Every `ForkRunner.run()` call | `session_id`, `prompt`, `model` | response text |

Fork runs triggered during a `/chat` turn appear as **nested child spans** under the turn span (asyncio context propagation). Fork runs during `/chat/stream` appear as top-level spans.

Agent identity attributes (`obs.agent_name`, `obs.lineage`, `obs.parent_session_id`, `obs.root_team_key`, `obs.is_fork`) are attached to each span once the session is established.

## Viewing traces

After sending a message, open the Weave UI link printed on startup, or navigate to:

```
https://wandb.ai/<your-entity>/obs-agent/weave
```

The **Traces** tab shows a call tree per turn. Click a turn to see inputs, outputs, and any nested fork spans.

To correlate traces across parent and child daemon processes (subprocess forks), filter by `obs.parent_session_id` in the Weave UI.

## Disabling

Remove or unset `WEAVE_PROJECT`. The agent runs identically with zero tracing overhead — the tracing code path is fully bypassed when the variable is absent.

If `weave` is not installed, the agent still starts and runs normally — the `ImportError` is caught silently.
