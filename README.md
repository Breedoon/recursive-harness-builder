# Recursive Harness Builder

Recursive Harness Builder is a local Python runtime for recursive AI-agent workflows defined mostly as markdown: project instructions, procedure files, and optional hooks that shape how agents plan, execute, verify, and hand work to each other.

It is currently extracted from a personal system still named `obs-agent` internally. The public concept is a recursive harness: package names, commands, environment variables, and some code still use OBS/vault-oriented names until the runtime is renamed.

## What this is

Most AI coding tools treat an agent as one long-running assistant in one terminal. This project treats agents as observable, forkable, resumable workers:

- A conversation can fork into another agent with inherited context.
- Agents can spawn other agents recursively.
- Procedures are markdown prompt files that define roles such as Router, Assembler, Executor, and Verifier.
- Hooks can attach behavior to tool calls, stops, feedback, permissions, and reporting.
- Agents can message each other through inbox files and wake each other when work arrives.
- Schedules can resume agents later using interval or cron-style triggers.
- The harness can route models through Claude Code, Claude Agent SDK, and proxy-compatible providers.

The goal is not to replace Claude Code. The goal is to wrap Claude Code and similar agent CLIs in an orchestration layer that makes recursive work visible, controllable, and recoverable.

## Telegram is a UI choice

Telegram is the first polished UI for this harness, not the product itself.

The current Telegram runtime maps chats/topics to long-lived agent sessions. That makes the agent tree easy to watch from a phone or from Telegram Desktop / Telegram Lite, where topic-based window management is useful for many concurrent agents.

A different UI could sit on the same concepts: markdown procedures, agent sessions, forks, hooks, inboxes, schedules, and artifact paths.

## Harness vs. procedures

This repository is the **harness**:

- Python runtime code
- CLI and daemon entrypoints
- Telegram adapter
- session and fork lifecycle
- schedule handling
- team/inbox messaging
- hook integration
- cache/proxy support
- live-test infrastructure

The harness does not define one correct way for agents to plan, verify, or coordinate. Those behaviors live in **procedures**: markdown instructions that tell agents what roles to play and how to work.

The intended public split is:

- The harness is reusable infrastructure.
- A runnable example project is included so a new user can run recursive workflows immediately.
- Project-specific procedures can live outside the repo and be pointed to by configuration or project instructions.

## What it can do today

The current runtime supports:

- Local CLI and HTTP daemon entrypoints.
- Telegram bot operation with one topic per agent.
- Forked agent tasks through the AgentTask interface.
- Multi-agent lineage tracking.
- Inbox messaging between agents in the same tree.
- Per-topic schedules through interval or cron modes.
- Telegram voice/file ingestion paths.
- Multi-model routing through Claude Code and CLIProxyAPI-compatible endpoints.
- Cache-normalizing proxy support for Claude Code API calls.
- Live Telegram evals and smoke tests.

Some pieces are still being generalized from a personal setup:

- Configuration is still environment-variable heavy.
- Several names still say `OBS`, `vault`, or `obs_agent`.
- Transcription is being made pluggable; older code assumes a local script.
- Native Windows is intended to be supported because the runtime is Python, but it has not yet been validated end-to-end.

## Repository layout

- `src/obs_agent/` — Python runtime package.
- `src/cache_proxy.py` — cache-normalizing proxy support.
- `tests/` — unit, integration, eval, and live Telegram smoke tests.
- `examples/recursive-workflow/` — runnable starter project with its own `CLAUDE.md` and v1 recursive-workflow procedures.
- `docs/configuration.md` — configuration and deployment modes.
- `docs/procedures.md` — procedure-pack guidance.
- `INSTALL.md` — setup from a fresh clone.
- `env.example` — placeholder environment template.

## Requirements

The current project expects:

- Python 3.12 or newer.
- A virtual environment managed by `uv`.
- Claude Code / Claude Agent SDK access.
- Claude Code authentication through the normal local OAuth/subscription flow for the default Claude path.
- A Telegram bot token for Telegram mode.
- A project directory containing `.claude/` and `CLAUDE.md` until the entry-file setting is generalized; use `examples/recursive-workflow/` for the first run.
- Optional: additional Telegram bot tokens for higher-concurrency Telegram operation.
- Optional: Telegram userbot credentials for automated group/topic provisioning and live tests.
- Optional: CLIProxyAPI-compatible proxy for Codex, GPT, Gemini, or other non-Claude model routing.
- Optional: transcription backend for Telegram voice messages.

## Quick start for developers

```bash
git clone <repo-url>
cd <repo>
uv sync --extra dev
cp env.example .env
```

Then edit `.env` with at least:

- `OBS_VAULT_PATH` pointing to `examples/recursive-workflow/` for the starter project, or another project directory that contains `.claude/` and `CLAUDE.md`.
- `OBS_TELEGRAM_BOT_TOKEN` for Telegram mode.
- `OBS_TELEGRAM_ALLOWED_USERS` with your numeric Telegram user ID.
- model/provider settings appropriate for your Claude Code or CLI proxy setup.

Run the Telegram runtime:

```bash
uv run python -m obs_agent.telegram_main
```

Or run the terminal client:

```bash
uv run obs-agent
```

The public command remains `obs-agent` for now even though the external framing is Recursive Harness Builder.

See `INSTALL.md` and `docs/configuration.md` for setup details.

## Documentation

Start with:

- `INSTALL.md` — setup from a fresh clone.
- `env.example` — supported configuration surface with placeholders.
- `docs/configuration.md` — environment variables and deployment modes.
- `docs/procedures.md` — bundled and external procedure packs.

## License

Apache License 2.0. See `LICENSE`.

## Development posture

This project is built around live behavior, not mocked confidence. Unit tests are useful, but the real proof is whether an agent works through the actual runtime surface.

For Telegram-facing changes, use the live Telegram smoke/eval infrastructure. For changes touching schedules, process lifecycle, forks, or inbox messaging, test the behavior end-to-end in an isolated worktree and with test credentials.

## Publication status

This repo is being prepared for private GitHub sharing before public release. Before publishing broadly, it still needs:

- final external naming decision;
- clean-clone install test;
- native Windows validation;
- CLIProxyAPI setup validation for non-Claude models;
- continued terminology cleanup from vault/OBS-specific names to project-directory/harness language;
- a preflight command that verifies Python, project directory shape, Telegram config, proxy state, and Claude Code binary resolution.

Do not assume every internal name is final yet.
