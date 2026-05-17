# Recursive Harness Builder

Recursive Harness Builder is a local runtime for building recursive agent workflows out of markdown prompts, optional hooks, and ordinary project files.

It wraps the Claude Agent SDK with a few added design choices: the ability for agents to clone themselves into a forked subagent, unlimited depth of subagents, agent-to-agent messaging, scheduling, and hookable tool behavior.

## Why use it

Most agent tools give you one agent in one terminal, with one layer of subagents. Recursive Harness Builder lets you define a harness around the agent:

- **Fork work into child agents** that inherit context and continue independently.
- **Attach hooks** to constrain or extend behavior, such as preventing orchestrators from editing files.
- **Coordinate agents** through inbox messages, artifacts, schedules, and resumable sessions.
- **Use Telegram as a UI** when you want a phone-friendly or multi-window view of many concurrent agents.
- **Route non-Claude models** through a CLIProxyAPI-compatible endpoint when you want Codex, GPT, Gemini, or other providers.

The runtime is the harness. The workflow is yours.

## Example workflow

Start with the bundled recursive workflow example:

- [`examples/recursive-workflow/`](examples/recursive-workflow/) — a small project with flat markdown procedures and a guard hook.
- [`docs/procedures.md`](docs/procedures.md) — how procedure files are organized and launched.

The example shows the intended pattern: describe coordination behavior in markdown, point an agent at the right procedure file, and let the harness manage forks, messages, hooks, and artifacts.

## Quick start

The recommended setup path is to let an agent install it for you.

Open Claude Code, Codex, or another coding agent and give it this prompt:

```text
Clone https://github.com/Breedoon/recursive-harness-builder and follow INSTALL.md to set it up for me. Use examples/recursive-workflow as the first project. Ask me for any credentials or Telegram setup values you need.
```

## Requirements

You will need:

- Python 3.12+
- `uv`
- Claude subscription OAuth for the default Claude path
- Optional: Telegram bot credentials for the Telegram UI
- Optional: CLIProxyAPI-compatible proxy for non-Claude models (eg, Codex)

## Interfaces

Recursive Harness Builder currently includes:

- a local CLI runtime;
- a Telegram runtime with one topic per agent;
- AgentTask-based forking;
- inbox messaging between agents;
- interval and cron-style schedules;
- hook support for tool calls and lifecycle events;
- cache/proxy support for provider routing.

Telegram is the UI. The core model is markdown-defined workflow procedures running on top of resumable, forkable agent sessions.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
