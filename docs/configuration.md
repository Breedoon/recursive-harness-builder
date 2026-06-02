# Configuration Guide

This guide describes the public configuration surface for Recursive Harness Builder as it exists today, plus the naming/defaults that should be used by public install docs. Copy `env.example` to `.env` and fill in the values for your machine.

## Loading rules

- The runtime reads `.env` from the repository root before constructing `OBSConfig`.
- Explicit shell environment variables win over `.env` values.
- `OBS_PROFILE` selects profile-specific overrides. For example, `--profile test` or `--test` causes `OBS_TEST_TELEGRAM_BOT_TOKEN` to populate `OBS_TELEGRAM_BOT_TOKEN` when the generic key is not already set.
- Profile-specific keys should be used for test/prod separation; generic keys should describe the active profile after bootstrap.

## Current naming caveat

The current code still uses `OBS_VAULT_PATH` and validates that the target directory contains both `CLAUDE.md` and `.claude/`. Public docs should describe this as the **project directory** because the harness can run against any prepared directory, not only an Obsidian vault.

Until the runtime is renamed, use:

```bash
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow
```

The configured directory must contain:

- `CLAUDE.md` — entry context for the root agent.
- `.claude/` — Claude Code project metadata, procedures, skills, settings, or other runtime context.

For a first install, use the bundled `examples/recursive-workflow/` directory. It already has the required shape and includes v1 recursive-workflow procedures.

## Minimal one-user setup

For the first public setup path, aim for a single bot and one human operator:

```bash
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow
OBS_DEFAULT_MODEL=claude
OBS_TELEGRAM_BOT_TOKEN=1234567890:replace-with-bot-token
OBS_TELEGRAM_ALLOWED_USERS=123456789
```

This supports an existing-chat/manual Telegram setup. The user creates a bot with BotFather, adds it to a Telegram chat or group, grants the needed permissions for forum topics when using groups, and starts the Telegram runtime.

## Telegram modes

### Bot-only mode

Required:

- `OBS_TELEGRAM_BOT_TOKEN`
- `OBS_TELEGRAM_ALLOWED_USERS`

Optional:

- `OBS_TELEGRAM_BOT_TOKENS` — comma-separated sender pool. The primary token is always first.
- `OBS_TELEGRAM_STATE_DB_PATH` — persistent SQLite state location.
- `OBS_TELEGRAM_TEMP_ROOT` — temporary attachment/download root.
- `OBS_RUNTIME_LOG_FILE` or `OBS_TELEGRAM_LOG_FILE` — runtime logs.

Bot-only mode is the safest default for public docs because it avoids a Telethon user session. If the bot cannot create groups itself, the user manually adds it to a Telegram group and grants admin permissions.

### Bot plus userbot provisioning mode

Add these only if the runtime should create groups, enable forum topics, add/promote configured bots, talk to BotFather, or manage Telegram chat folders:

```bash
OBS_TELEGRAM_USERBOT_API_ID=123456
OBS_TELEGRAM_USERBOT_API_HASH=replace-with-api-hash
OBS_TELEGRAM_USERBOT_SESSION=replace-with-telethon-string-session
OBS_TELEGRAM_GROUP_FOLDER_TITLE=Recursive Harness
OBS_TELEGRAM_GROUP_ADDLIST_URL=https://t.me/addlist/...
OBS_TELEGRAM_NOTIFY_USERNAME=your_telegram_username
```

This mode depends on Telethon and Telegram API credentials from my.telegram.org. `OBS_TELEGRAM_USERBOT_SESSION` is a Telethon `StringSession`, not a `.session` file path. The userbot may be the operator's main Telegram account, but a secondary Telegram account is safer if the user does not want group ownership and automation tied to their personal account. Folder placement is optional; omit `OBS_TELEGRAM_GROUP_FOLDER_TITLE` and `OBS_TELEGRAM_GROUP_ADDLIST_URL` for the simplest setup.

## Model and provider selection

Use `OBS_DEFAULT_MODEL` for normal defaults and `OBS_AGENT_MODEL` only when you want to force every root session to a specific model.

Supported shorthands in current code include:

- `claude`, `opus`, `sonnet`, `haiku`
- `gpt`, `gpt-mini`, `openai`, `chatgpt`
- `gemini`, `gemini-pro`, `gemini-flash`

Claude models route directly to Anthropic through Claude Code. Non-Claude models route through the local cache proxy and then CLIProxyAPI.

For direct Anthropic/API-key setups, set `ANTHROPIC_API_KEY` unless Claude Code authentication supplies credentials through a local subscription/session.

For non-Claude models, configure:

```bash
OBS_CLI_PROXY_BASE_URL=http://127.0.0.1:8317
OBS_CLI_PROXY_API_KEY=sk-anything
```

The standalone `src/cache_proxy.py` currently also reads legacy names:

```bash
CLI_PROXY_BASE_URL=http://127.0.0.1:8317
CLI_PROXY_API_KEY=sk-anything
```

Keep both pairs until the proxy code is unified behind `OBS_CLI_PROXY_*`.

## Cache proxy

The cache-normalizing proxy starts automatically by default before daemon or Telegram sessions:

```bash
OBS_CACHE_PROXY_ENABLED=true
OBS_CACHE_PROXY_PORT=18923
```

Use `OBS_SKIP_CACHE_PROXY=1` for debugging or if the proxy fails to start. When disabled or unhealthy, sessions route directly to Anthropic for Claude models.

## Voice transcription

Current runtime expects an executable transcription script:

```bash
OBS_TELEGRAM_TRANSCRIPTION_SCRIPT=/absolute/path/to/transcribe.sh
```

The script interface is:

```bash
transcribe.sh AUDIO_FILE TITLE DEST_DIR
```

It should write a markdown transcript into `DEST_DIR`. If the script is missing or exits non-zero, the voice message still reaches the agent with a transcription failure note and the stored audio path.

Public docs should describe transcription as optional. The current default points to a developer-local path and should not be treated as portable. A future pluggable transcription implementation should preserve the command-style interface or provide a compatibility wrapper.

## Runtime tuning

Common settings:

```bash
OBS_DAEMON_HOST=127.0.0.1
OBS_DAEMON_PORT=7832
OBS_MAX_QUEUE_CONTINUATIONS=3
OBS_BG_FORK_TIMEOUT=600
OBS_MAX_BUFFER_SIZE=10485760
OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS=1000000
OBS_AUTO_COMPACT_WINDOW_TOKENS=0
OBS_FORK_CACHE_WARMUP_DELAY_SECONDS=1.0
```

`OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS` is telemetry for context reporting and
model suffix resolution. The default OBS context is 1M; at the Claude Code
boundary, a model without an explicit suffix is sent with the resolved context
suffix, for example `claude` becomes `claude-opus-4-7[1m]`.
`OBS_AUTO_COMPACT_WINDOW_TOKENS` optionally caps the Claude Code auto-compact
trigger window. Leave it at `0` to pass the full resolved context through to
Claude Code.
`OBS_FORK_CACHE_WARMUP_DELAY_SECONDS` gives parent prompt-cache writes a short
propagation window before a fork sends its first request.

Process/resource settings:

```bash
OBS_CLAUDE_IDLE_PROCESS_CAP=50
OBS_CLAUDE_KILL_ON_IDLE=false
```

Telegram transport settings:

```bash
OBS_TELEGRAM_TRANSPORT_BASE_CHAT_INTERVAL_SECONDS=0.35
OBS_TELEGRAM_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS=5.0
OBS_TELEGRAM_TYPING_ACTION_INTERVAL_SECONDS=4.0
OBS_TELEGRAM_TYPING_ACTIONS_ENABLED=true
```

## Platform notes

- macOS and Linux/WSL are the safest paths today.
- Native Windows is an intended support target because the runtime is Python, but it has not yet been validated end-to-end.
- Native Windows non-Claude/GPT/Gemini support depends on CLIProxyAPI and still needs testing there.
- Native Windows transcription requires a Windows-native executable/script; the historical Mac shell script is not portable.

## Public config cleanup recommendations

Before public release, the config surface should be simplified or aliased:

- Add `OBS_PROJECT_DIR` as the public name and keep `OBS_VAULT_PATH` as a backward-compatible alias.
- Add `OBS_AGENT_ENTRY_FILE=CLAUDE.md` when entry-file injection is implemented.
- Replace the developer-local transcription default with either no default or a repo-local example adapter.
- Prefer `OBS_CLI_PROXY_*` everywhere and retire bare `CLI_PROXY_*` names in docs after code unification.
- Make bot-only Telegram setup the default, with userbot provisioning clearly optional.
- Keep `examples/recursive-workflow/` as the default starter project so new users do not need to build a project directory from scratch.
