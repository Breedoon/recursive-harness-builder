# Installation Guide

This guide is written for an agent or developer setting up Recursive Harness Builder from a fresh clone. It describes the current repository behavior plus public-release gaps that still need verification.

The project is a Python harness around Claude Code / Claude Agent SDK. Workflows are defined primarily through markdown project instructions, procedure prompts, and optional hooks. Telegram is one UI for the harness, not the harness itself.

## Current support level

- macOS, Linux/WSL, and native Windows are intended support targets.
- Native Windows has not yet been validated end-to-end; if first-run setup fails there, use WSL while the compatibility issue is narrowed.
- Claude models can run without CLIProxyAPI.
- Non-Claude models route through CLIProxyAPI via the cache proxy and need a working local CLIProxyAPI-compatible service.
- Telegram voice transcription is optional and currently depends on an external executable script configured by environment variable.

## Prerequisites

Install these before configuring the harness:

1. Python 3.12 or newer.
2. `uv` for Python dependency and virtualenv management.
3. Git.
4. Claude Code / Claude Agent SDK access, authenticated locally through Claude Code OAuth/subscription.
5. A Telegram bot token from BotFather if using Telegram mode.
6. Optional: extra Telegram bot tokens for concurrent Telegram output from many agents.
7. Optional: Telegram API ID/hash and a Telethon session if using group provisioning commands such as `/new_group`.
8. Optional: CLIProxyAPI if using Codex, GPT, Gemini, or other non-Claude models.
9. Optional: a transcription script if using Telegram voice messages.

## Clone and install Python dependencies

```bash
git clone <repo-url>
cd <repo-directory>
uv sync
```

For development and tests:

```bash
uv sync --extra dev
```

The package exposes this console script:

```bash
uv run obs-agent --help
```

The Telegram runtime is launched as a Python module:

```bash
uv run python -m obs_agent.telegram_main
```

## Choose a project directory

The current code still calls this `OBS_VAULT_PATH`, but it is the project directory where each agent runs Claude Code. The directory must contain:

- `.claude/` — project Claude Code settings, procedures, skills, agents, and related project state.
- `CLAUDE.md` — the project entry/instructions file.

For the first run, use the bundled starter project:

```bash
OBS_VAULT_PATH="$PWD/examples/recursive-workflow"
```

That directory contains its own minimal `CLAUDE.md` plus flat v1 recursive-workflow procedure prompts under `procedures/`.

To make your own project later:

```bash
cp -R examples/recursive-workflow ~/recursive-harness-project
OBS_VAULT_PATH=~/recursive-harness-project
```

Public-release note: the variable and code terminology should eventually be renamed from vault to project directory, and the entry file should become configurable instead of hardcoded to `CLAUDE.md`.

## Configure environment

Copy the example file and edit it:

```bash
cp env.example .env
```

The runtime loads `.env` from the repo root. Existing shell environment variables win over values in `.env`.

Runtime profiles are supported:

- Default profile: `prod`
- Test profile: pass `--test` or `--profile test`
- Profile-specific keys such as `OBS_TEST_TELEGRAM_BOT_TOKEN` are mapped to generic keys when that profile is active.

## Required settings for Telegram mode

At minimum:

```env
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow
OBS_TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
OBS_TELEGRAM_ALLOWED_USERS=123456789
```

`OBS_TELEGRAM_ALLOWED_USERS` is required. Use numeric Telegram user IDs, comma-separated. The bot refuses to start without an explicit allowlist.

To run:

```bash
uv run python -m obs_agent.telegram_main
```

Then message the bot from an allowed Telegram account.

## Telegram setup

Telegram is one UI for the harness, not the harness itself. The runtime uses Telegram because forum topics give each agent a navigable conversation, and Telegram Desktop/Lite provides a practical multi-window operator console. Other UIs can be added later.

### Bot-only setup

Use this when you already have a chat/group and can manually add the bot. This is the simplest public setup path.

1. In Telegram, open `@BotFather`.
2. Send `/newbot`, choose a display name, then choose a username ending in `bot`.
3. Copy the raw BotFather token into `OBS_TELEGRAM_BOT_TOKEN`. A real token looks like `1234567890:AA...`; do not add labels around it.
4. Message the bot once from the Telegram account that will control the harness.
5. Find your numeric Telegram user ID and put it in `OBS_TELEGRAM_ALLOWED_USERS`.

One deterministic way to find the numeric user ID is through the Bot API after you message the bot:

```bash
TOKEN="replace-with-raw-botfather-token"
python - <<'PY'
import json, os, urllib.request
payload = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{os.environ['TOKEN']}/getUpdates"))
for item in payload.get("result", []):
    user = (item.get("message") or {}).get("from") or {}
    if user:
        print(user.get("id"), user.get("username") or user.get("first_name") or "")
PY
```

For a private 1:1 chat with the bot, no group permissions are needed. For a Telegram forum supergroup, add the bot to the group and make it an admin with at least:

- send messages;
- create/manage topics;
- edit topics;
- delete topics;
- invite users if the bot should help add participants.

If you want the bot to see ordinary group messages, disable bot privacy for that bot in BotFather with `/setprivacy`. Command-only operation may work with privacy enabled, but the harness is easiest to operate when the bot can receive the messages you send in its topics.

### Multiple sender bots

A single bot is valid. For heavier concurrent agent trees, configure multiple bots so outbound messages can be spread across a sender pool and per-bot Telegram rate limits are less likely to become the bottleneck.

`OBS_TELEGRAM_BOT_TOKENS` is a comma-separated list of raw BotFather tokens. The primary polling bot is `OBS_TELEGRAM_BOT_TOKEN` if set, otherwise the first token in `OBS_TELEGRAM_BOT_TOKENS`.

```env
OBS_TELEGRAM_BOT_TOKEN=1234567890:replace-with-primary-token
OBS_TELEGRAM_BOT_TOKENS=1234567890:replace-with-primary-token,2345678901:replace-with-second-token
```

Add every configured bot to the operating group and grant the same topic/message admin permissions. Secondary bots are send-only participants; the runtime clears stale command menus on secondary bots at startup.

### Userbot-assisted group provisioning

Use this optional mode if you want the harness to create forum supergroups, add/promote bots, talk to BotFather, or update Telegram chat folders. Bot-only mode does not require a userbot.

The userbot is a real Telegram user account controlled through Telethon. You may use your main Telegram account, but a secondary account is safer if you do not want automation tied to your personal account. The account that runs provisioning remains the creator/owner of groups it creates until the runtime promotes the target human user and leaves where supported.

1. Create Telegram API credentials at `https://my.telegram.org/apps`.
2. Generate a Telethon `StringSession` for the chosen user account.
3. Store the API ID, API hash, and StringSession in `.env`.

Example StringSession generator:

```bash
uv run python - <<'PY'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("Telegram API ID: ").strip())
api_hash = input("Telegram API hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("Copy this value into OBS_TELEGRAM_USERBOT_SESSION:")
    print(client.session.save())
PY
```

Telethon will prompt for the phone number, login code, and two-factor password if the account uses one. Treat the printed session string like a password.

Add:

```env
OBS_TELEGRAM_USERBOT_API_ID=12345
OBS_TELEGRAM_USERBOT_API_HASH=replace-with-api-hash
OBS_TELEGRAM_USERBOT_SESSION=replace-with-telethon-string-session
```

### Optional chat folder placement

Folder placement is optional. The simpler first install is to skip folder settings entirely.

If you want provisioned chats to land in a Telegram folder, create that folder yourself first in Telegram Desktop/Lite or mobile:

1. Open Settings.
2. Open Folders / Chat Folders.
3. Create a folder such as `Recursive Harness`.
4. Set `OBS_TELEGRAM_GROUP_FOLDER_TITLE` to the exact folder title.

```env
OBS_TELEGRAM_GROUP_FOLDER_TITLE=Recursive Harness
```

`OBS_TELEGRAM_GROUP_ADDLIST_URL` is only for advanced shared-folder/addlist workflows. If set, the current runtime also requires `OBS_TELEGRAM_GROUP_FOLDER_TITLE` and expects the addlist invite to already exist.

## Model/provider setup

### Claude models

Claude models are the default path and should work with Claude Code OAuth/subscription authentication. Set:

```env
OBS_DEFAULT_MODEL=claude
```

or a specific model:

```env
OBS_AGENT_MODEL=claude-opus-4-7
```

Claude traffic goes to Anthropic directly through the cache-normalizing proxy when the proxy is enabled.

### Codex, GPT, Gemini, and other non-Claude models through CLIProxyAPI

For non-Claude model names, OBS passes the model to Claude Code and routes API traffic through the cache proxy to CLIProxyAPI.

Set:

```env
OBS_AGENT_MODEL=gpt
OBS_CLI_PROXY_BASE_URL=http://127.0.0.1:8317
OBS_CLI_PROXY_API_KEY=sk-anything
```

The cache proxy currently also reads these bare names when forwarding non-Claude requests:

```env
CLI_PROXY_BASE_URL=http://127.0.0.1:8317
CLI_PROXY_API_KEY=sk-anything
```

CLIProxyAPI setup is external to this repo and still needs a dedicated public walkthrough/link.

## Cache proxy

The Telegram and daemon entrypoints attempt to start the cache-normalizing proxy automatically unless disabled.

Defaults:

```env
OBS_CACHE_PROXY_ENABLED=true
OBS_CACHE_PROXY_PORT=18923
```

Disable it for troubleshooting:

```env
OBS_SKIP_CACHE_PROXY=1
```

The proxy routes:

- Claude models to Anthropic directly.
- Non-Claude models to CLIProxyAPI.

## Claude Code / Agent SDK binary control

The Python package depends on `claude-agent-sdk`. OBS creates `ClaudeSDKClient` instances from that package; the SDK is responsible for locating and running the Claude Code runtime.

For public installs, do not rely on an arbitrary globally-installed `claude` being compatible unless the SDK contract guarantees it. The recommended release posture is:

1. Install through the pinned Python dependency set (`uv sync` using `uv.lock`).
2. Run OBS through `uv run ...` so the installed SDK version is deterministic.
3. If a separate Claude Code binary is required by the SDK, document the exact supported version and verify which binary the SDK launches.
4. Add a preflight command before public release that prints the SDK version and the resolved Claude Code binary path/version.

Current gap: the repo does not yet expose a preflight command that proves which Claude Code binary the SDK will execute. Until that exists, installation docs should treat binary resolution as a release-blocking verification item.

## Voice transcription

Voice messages are optional. Current behavior:

- Voice files are downloaded into `OBS_TELEGRAM_TEMP_ROOT`.
- The runtime calls `OBS_TELEGRAM_TRANSCRIPTION_SCRIPT` as an executable.
- Arguments are: `audio_file`, `title`, and `destination_directory`.
- The script is expected to write `<title>.md` in the destination directory.
- If the script is missing or exits nonzero, the message is still delivered to the agent with a transcription error note.

Configure:

```env
OBS_TELEGRAM_TRANSCRIPTION_SCRIPT=/absolute/path/to/transcribe.sh
```

A portable public release should include either a bundled example script or a pluggable transcription command interface with documented backends. Native Windows transcription is not verified.

## CLI mode

For local terminal use without Telegram:

```bash
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow uv run obs-agent
```

The CLI starts a local FastAPI daemon if one is not already running, then streams responses in the terminal.

Default daemon settings:

```env
OBS_DAEMON_HOST=127.0.0.1
OBS_DAEMON_PORT=7832
```

## First-run smoke test

After configuring `.env`:

1. Start Telegram runtime:

   ```bash
   uv run python -m obs_agent.telegram_main
   ```

2. Send a message from an allowed Telegram account:

   ```text
   hello, reply with one sentence
   ```

3. Confirm:

   - The bot accepts the message.
   - An agent response appears.
   - `.obs-agent/state/telegram-state.sqlite3` is created or updated.
   - No secrets are printed in logs.

For CLI-only setup:

```bash
printf 'hello, reply with one sentence\n/quit\n' | OBS_SIMPLE_INPUT=1 uv run obs-agent
```

## Before publishing or sharing

Do this before pushing a public repository:

1. Ensure `.env` is ignored and never committed.
2. Provide `env.example` with placeholders only.
3. Audit tracked files for personal vault data, Telegram tokens, session transcripts, generated worktrees, local spikes, and runtime state.
4. Decide whether to include `fixture_vault/` as a sanitized fixture or regenerate it during tests.
5. Decide which procedures are public examples and which remain private.
6. Add a preflight command that verifies Python version, dependency install, project directory shape, Telegram config, cache proxy port availability, CLIProxyAPI reachability when needed, and Claude Code/SDK binary resolution.
7. Run a clean-clone install test on macOS, Linux/WSL, and native Windows before making the repo public.

## Known public-release gaps

- The project entry file is hardcoded to `CLAUDE.md`.
- The project directory setting is still named `OBS_VAULT_PATH`.
- The default transcription script path points to a local developer machine path unless overridden.
- Native Windows support is intended but not validated end-to-end.
- CLIProxyAPI installation and Windows support need a dedicated external check.
- There is no first-class preflight command yet.
- The public example procedure bundle needs live validation in a clean install.
