# Configuration Guide

For the per-agent ownership and persistence map, see
[Agent and session architecture](agent-session-model.md).

This guide describes the public configuration surface for Recursive Harness Builder as it exists today, plus the naming/defaults that should be used by public install docs. Copy `env.example` to `.env` and fill in the values for your machine.

Formal testing has a separate configuration contract: use the host-managed
`obs-live-test` service and [`docs/testing.md`](testing.md). Repository `.env`
and ambient production values are not permitted to populate that lane. The
candidate protocol is authored but **UNVERIFIED / UNRESOLVED** and was not
executed.

## Loading rules

- The runtime reads `.env` from the repository root before constructing `OBSConfig`.
- Explicit shell environment variables win over `.env` values.
- Production is the runtime default. `OBS_PROD_*` values map to generic keys
  when production bootstrap is selected.
- Pure unit/library callers may still parse a test profile and directly exercise
  `OBS_TEST_*` mapping without launching a live runtime.
- All public live entry points reject `--test`, `--test-instance`,
  `--profile test`, and `OBS_PROFILE=test` before `.env` loading or factories.
- A profile is never an isolation boundary. Formal testing requires the
  independently observed host attestation, immutable source/image/executed-code
  binding, pairwise-disjoint topology, test-secret measurement, mount/network/
  port/poller denial, and inner preflight in `docs/testing.md`.

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
OBS_DEFAULT_MODEL=sol
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

## User timezone

Schedule wall-clock evaluation and all user-visible schedule timestamps use `OBS_USER_TIMEZONE`, an IANA timezone name. The default is `Europe/Warsaw`. For example, `OBS_USER_TIMEZONE=America/New_York` makes cron expressions use New York wall time and renders `from`, `until`, and `next_run_at` with that timezone's UTC offset.

## Model and provider selection

Use `OBS_DEFAULT_MODEL` for normal defaults and `OBS_AGENT_MODEL` only when you want to force every root session to a specific model.

Supported shorthands in current code include:

- `claude` and `opus` → `claude-opus-5-5`
- `sonnet` → `claude-sonnet-5-5` (1M context, 128K output); `fable` → `claude-fable-5-1`; `haiku` → `claude-haiku-5-5`
- `astra` → `gpt-6-astra`; `sol`/`gpt`/`gpt-pro`/`gpt-sol`/`openai`/`chatgpt` → `gpt-6.1-sol` (released 2026-09-29; `gpt-6-sol` and `gpt-5.6-sol` still work by full name); `terra` → `gpt-5.6-terra`; `luna` → `gpt-6-luna`
- `gpt-mini`
- Local Qwen: `qwen`/`qwen-fn`/`qwen-flash-next`/`qwen3.8-flash-next` → the DGX Sparks Qwen3.8 Flash Next (`local-sparks-qwen3.8-flash-next-abliterated`); `qwen-27b`/`qwen3.8-27b`/`local-qwen` → the 3090 Qwen (`local-qwen3.8-27b`). Plain `qwen` is a single switch, `DEFAULT_QWEN_MODEL` in `config.py`.
- Local GLM: `glm`/`glm-flash`/`glm-5.3-flash`/`glm-5.3-flash-uncensored` → the Sparks GLM (`local-sparks-glm-5.3-flash-uncensored`). The hosted `glm-5.3` (Z.AI through CLIProxyAPI) is a different model and keeps its own name.
- `gemini`, `gemini-pro`, `gemini-flash`

Claude models route directly to Anthropic through Claude Code. Non-Claude models route through the local cache proxy and then CLIProxyAPI.

### DGX Sparks models (`local-sparks-*`)

The Sparks host one model at a time, behind their own cache proxy (default `http://127.0.0.1:28931`). The backend follows the model *name*: `obs_agent.spark` fills `ANTHROPIC_BASE_URL`, the credential (read from `/workspace/runtime/secrets/spark-api-key` at session build; never stored) and the small-model variables for any session whose model resolves to `local-sparks-*`, so children, forks and restores reach the Sparks without an `env`. An explicit per-session `env` still wins. Overrides: `OBS_SPARK_LLM_BASE_URL`, `OBS_SPARK_LLM_KEY_FILE`.

- Default window is 262,000 tokens (the default profile serves 262,144). `[1m]` is valid only while the `qwen-1m` profile serves (≈1,048,576 tokens); OBS then raises the compaction ceiling to the requested window.
- When an AgentTask child is created with a Spark model, OBS asks `GET /v1/models` and fails with an actionable message if a different model is being served or the window is larger than the serving profile. Unreachable endpoint = no check (the ordinary connection error surfaces). `OBS_SPARK_PREFLIGHT=0` disables the check.
- Switching models is a host operation, not an OBS setting: `spark-model switch qwen|qwen-1m|glm` (see the vault DGX Sparks Runbook).

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

### Request log (prompt-cache diagnosis)

Since 2026-10-08 the proxy keeps a standing, session-keyed pool of recent
`/v1/messages` requests from all chats and routes (mission
cache-proxy-prefix-fix, beads `vault-mief.2`/`.8`; Daniel 2026-10-08 23:55Z:
"ideally we should have like ongoing some like gigabyte of pool for all ongoing
requests to be saved from all chats. So like if things like this happen there
at least there are logs"). It is the instrument for finding prompt-cache prefix
breakage: when a turn re-writes the conversation instead of reading it, diff
that request against the previous request of the same session.

```bash
CACHE_PROXY_LOG_DIR=/workspace/runtime/logs/cache-proxy     # usage.jsonl lives here
CACHE_PROXY_REQUEST_LOG=            # unset: ON on the production port (28925), OFF elsewhere; 1/0 forces
CACHE_PROXY_REQUEST_LOG_DIR=        # default: $CACHE_PROXY_LOG_DIR/requests
CACHE_PROXY_REQUEST_LOG_MAX_GB=1    # ring-buffer cap for everything under the dir
```

Layout: `requests/YYYY-MM-DD/index.jsonl` (one line per request: `req_id`,
`session_id`, `model`, `route`, `http_status`, `error`, `usage`, normalization
counts, sizes, `wire_sha256`, credential-free client headers) and
`requests/YYYY-MM-DD/<req_id>.{pre,post,wire}.json.gz` — `pre` is what the CLI
sent, `wire` what went upstream, `post` the body right after
`normalize_request()` (stored only when it differs from `wire`, i.e. when the
effort pin/strip changed something). `usage.jsonl` lines carry `req_id`,
`session_id` and `http_status`. Capture runs on a background thread: a full
queue drops the record (logged at 1/10/100/every 1000 drops), it never delays
or fails a request.

Ring buffer: the cap covers bodies AND index files. When exceeded, the writer
evicts oldest first down to 90%: body files of the oldest day first, then that
day's index once it holds no bodies (the current day's index is kept). At
2026-10-09 night-time volume (~1,900 requests/h, ~580 MB/h gzip) **1 GB holds
roughly 1–2 hours** of full bodies; busier hours shorten it. That short window
is the stated tradeoff — for an investigation needing longer history, raise
`CACHE_PROXY_REQUEST_LOG_MAX_GB` (needs a proxy restart). Index lines are
~1.5 KB each, so they are a small part of the cap.

Redaction: credentials never reach disk. Headers containing
auth/key/cookie/token/secret are dropped. Bodies and index lines pass through
`redact_secrets()`, which replaces secret-looking values with
`[REDACTED:<kind>:<sha256-12>]`: Anthropic/OpenAI-style `sk-` keys (incl.
OAuth `sk-ant-oat…`), GitHub/Slack/AWS/Google tokens, Telegram bot tokens, JWTs,
Bearer values, private-key blocks, `KEY=value`/`"api_key": "…"` assignments for
key/token/secret/password/api_hash/session-string names, and any ≥300-char
random-looking base64 run (this covers Telegram/Telethon session strings, image
data and thinking-block `signature`s). Same value → same placeholder, so two
captures compare equal/unequal exactly where the originals did; JSON stays
valid; `prefix_diff.py` works unchanged on redacted files (byte offsets refer to
the redacted text). `wire_sha256` is of the unredacted wire bytes. Redaction
costs ~150 ms per 1.2 MB body on the writer thread.

Diff tool: `scripts/prefix_diff.py` — `prefix_diff.py A B` (two `req_id`s or
files), `--session <id-prefix>` (every adjacent pair of one session, with the
usage of the later request), `--prev-any <req_id>` (pairs a request with the
earlier request of any session sharing the longest message prefix — use it for
recoveries, which continue a conversation under a new session id). It compares
tools → system → request config (model, thinking, output_config,
context_management, betas) → messages in prompt-cache order, ignoring only
`cache_control`, and reports the first divergence with byte offset and
snippets, plus whether the later request still carries a message-level cache
breakpoint.

Test runs: agents' shells inherit `CACHE_PROXY_LOG_DIR` from the daemon, so a
test proxy subprocess writes `usage.jsonl` (and, with `CACHE_PROXY_SAVE_BODIES=1`,
legacy `bodies/`) into the production directory unless you unset it:
`env -u CACHE_PROXY_LOG_DIR -u CACHE_PROXY_SAVE_BODIES pytest ...`. The request
log itself stays off on non-production ports unless forced.

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
OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS=900000
OBS_AUTO_COMPACT_WINDOW_TOKENS=0
OBS_FORK_CACHE_WARMUP_DELAY_SECONDS=1.0
```

`OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS` is telemetry for context reporting and
model suffix resolution. The fallback OBS context follows the default Sol model
at 900k. Current GPT models use a 900k default, while Qwen uses 262k. At the
Claude Code boundary, a model without an explicit suffix is sent with the
resolved model-specific context suffix, for example `sol` becomes
`gpt-6.1-sol[900k]`, `astra` becomes `gpt-6-astra[900k]`, and `claude` becomes
`claude-opus-5-5[1m]`. Local Qwen keeps its canonical unsuffixed provider model ID
while receiving the 262k context limit through the SDK environment.
`OBS_AUTO_COMPACT_WINDOW_TOKENS` optionally caps the Claude Code auto-compact
trigger window. Leave it at `0` to use OBS's model-aware default: the resolved
context window is passed through so Claude Code's built-in compaction curve is
used consistently. A 900k window targets compaction around 867k, Qwen's 262k
window around 229k, and a 200k window around 167k.
`OBS_FORK_CACHE_WARMUP_DELAY_SECONDS` gives parent prompt-cache writes a short
propagation window before a fork sends its first request.

Process/resource settings:

```bash
OBS_CLAUDE_IDLE_PROCESS_CAP=50
OBS_CLAUDE_KILL_ON_IDLE=false
OBS_CLAUDE_IDLE_EVICT_SECONDS=1800
```

`OBS_CLAUDE_IDLE_EVICT_SECONDS` (default 1800, `0` disables; vault-u3b.78)
closes the CLI of an idle AgentTask child whose last turn completed at least
that long ago. A 60 s sweep runs it (not only on fork completion); busy,
running and execution-active routes are skipped. The child reconnects lazily
with `--resume` on its next wake, the same path the cap uses. It adds to the
cap; `OBS_CLAUDE_KILL_ON_IDLE` still wins. Only fork/AgentTask child routes
are candidates; topic routes are never evicted.

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

## Reasoning effort

Use `/effort` in Telegram or the CLI to inspect a session's selected effort,
`/effort low|medium|high|xhigh|max` to change it between turns, and `/effort auto`
to return to its model default. AgentTask accepts the same levels plus `inherit`.
`OBS_EFFORT_LEVEL` sets a shared default; `OBS_MODEL_EFFORT_LEVELS` is a JSON
model-to-effort map, with aliases resolved like context defaults. See
[Reasoning effort](effort.md) for precedence, persistence, and CLIProxyAPI details.
