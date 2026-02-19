# Telegram Bot API & Testing Research

**Researcher**: telegram-api-researcher
**Date**: 2026-02-16
**Status**: Complete

---

## Part 1: Telegram Bot API Deep Dive

### 1.1 Polling vs Webhooks

Telegram offers two mutually exclusive methods for receiving updates:

| Aspect | getUpdates (Long Polling) | Webhooks |
|--------|--------------------------|----------|
| **Mechanism** | Bot polls Telegram servers via HTTP GET | Telegram pushes updates to your HTTPS endpoint |
| **Setup complexity** | Minimal — just call `getUpdates` in a loop | Requires HTTPS endpoint, SSL cert, open port |
| **Ports** | N/A (outbound only) | Must be 443, 80, 88, or 8443 |
| **Latency** | Configurable via `timeout` param (0-50s). With long polling (timeout=30s), updates arrive within seconds | Near-instant — Telegram pushes as events occur |
| **Reliability** | Immune to replay loops. If bot is down, updates queue for 24h | Must respond HTTP 200 within 60s or Telegram re-delivers. Can cause replay loops on failures |
| **Scaling** | Single consumer. Simple but limited throughput | Can handle higher throughput with proper infra |
| **Cloud exposure** | None — no inbound connections needed | Requires publicly reachable URL |
| **Local dev** | Works anywhere, including behind NAT | Requires ngrok/Cloudflare tunnel for local dev |

**Recommendation for OBS Agent**: **Long polling is the clear winner.**
- No exposed ports or cloud infrastructure needed (aligns with design goals)
- Simpler setup and error handling
- Good enough latency for a personal assistant (sub-second with timeout=30)
- Updates stored for 24h if bot is offline

**Key getUpdates parameters:**
- `offset`: Integer. Updates with `update_id <= offset` are discarded server-side. After processing, set `offset = last_update_id + 1`
- `limit`: 1-100 (default 100). Number of updates per batch
- `timeout`: Long polling timeout in seconds. 0 = short poll (testing only). Recommended: 20-30s
- `allowed_updates`: Filter which update types to receive (e.g., `["message"]`)

### 1.2 Message Limits

| Limit | Value |
|-------|-------|
| **Text message length** | 4096 UTF-16 code units (after entity parsing) |
| **Caption length** | 1024 characters (for photos, videos, documents) |
| **Message entities** | Counted against the 4096 character limit |
| **Messages per second (same chat)** | ~1 msg/s to same individual chat |
| **Messages per second (different chats)** | 30 msg/s (free), up to 1000 msg/s (paid with Telegram Stars) |

**Handling long messages (critical for AI agent output):**

The agent frequently generates responses longer than 4096 characters. Our implementation must:

1. **Split at safe boundaries**: Split on paragraph breaks (`\n\n`), then sentence boundaries (`. `), then word boundaries. Never split mid-word or mid-entity
2. **Preserve formatting across splits**: If a MarkdownV2 entity spans a split point, close it before the split and reopen after
3. **Send as sequential messages**: Use `sendMessage` for each chunk with a small delay (0.5-1s) between to maintain order
4. **Consider `sendDocument` fallback**: For very long outputs (>10 messages), send as a `.txt` or `.md` file attachment instead

**Inbound message splitting**: Telegram clients auto-split messages at 4096 characters but try to avoid cutting sentences. We should handle receiving multiple messages as a single logical input (buffer with a short timeout).

### 1.3 Media Types

The Bot API supports sending and receiving:

| Type | Method | Max Size | Format Notes |
|------|--------|----------|-------------|
| **Photo** | `sendPhoto` | 10 MB (compressed by Telegram) | Multiple sizes returned. Telegram compresses |
| **Document** | `sendDocument` | 50 MB (bot API), 2 GB (local API server) | Any file type. Preserves original |
| **Voice** | `sendVoice` | 50 MB | Must be .OGG encoded with OPUS, or .MP3/.M4A |
| **Audio** | `sendAudio` | 50 MB | .MP3 or .M4A. Shows as music player |
| **Video** | `sendVideo` | 50 MB | H.264 video, MP4 preferred |
| **Video Note** | `sendVideoNote` | Circular video messages | Max 1 min |
| **Sticker** | `sendSticker` | — | .webp, .tgs (animated), .webm (video) |
| **Animation** | `sendAnimation` | — | GIF or H.264/MPEG-4 AVC without sound |
| **Location** | `sendLocation` | — | Latitude/longitude |
| **Contact** | `sendContact` | — | Phone number + name |

**Three ways to send files:**
1. **file_id**: If file was previously sent/received, reuse its ID (fastest, no re-upload)
2. **HTTP URL**: Provide URL for Telegram to download (5 MB photos, 20 MB other via URL)
3. **Multipart upload**: Direct file upload via multipart/form-data

**Receiving files:**
- Bot receives a `file_id` in the update
- Call `getFile(file_id)` to get a download URL
- Download from `https://api.telegram.org/file/bot<token>/<file_path>`
- Files are stored on Telegram servers for at least 1 hour

**Relevance for OBS Agent:**
- Voice messages could be transcribed (Whisper API) for voice input
- Documents/images could be saved to vault or analyzed
- Agent could send vault files as documents
- Start with text only, add media support incrementally

### 1.4 Bot Setup Process

**Creating a bot:**
1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Provide a display name (e.g., "OBS Agent")
4. Provide a username (must end in `bot`, e.g., `obs_agent_bot`). 5-32 chars, alphanumeric + underscores
5. BotFather returns the bot token (format: `123456789:ABCdef...`). Treat as password

**For our setup, we need TWO bots:**
- `obs_agent_dev_bot` — development bot (personal use)
- `obs_agent_test_bot` — eval testing bot (used by test harness)

**Key BotFather settings:**
- `/setprivacy` — Disable for group mode (bot receives all messages, not just /commands). For private chat, irrelevant
- `/setcommands` — Set the command menu (e.g., `/start`, `/stop`, `/status`)
- `/setdescription` — What users see before starting the bot
- `/mybots` — Manage existing bots
- Token can be revoked anytime via `/revoktoken`

**Important constraints:**
- Bots CANNOT initiate conversations. User must send `/start` first
- Each bot token is tied to exactly one bot
- Bot usernames are globally unique and cannot be changed after creation

### 1.5 Python Libraries Comparison

| Library | Type | Async | Protocol | Best For | Stars | Active |
|---------|------|-------|----------|----------|-------|--------|
| **python-telegram-bot** (PTB) | Bot API wrapper | Yes (v20+) | HTTPS Bot API | Stable, well-documented bots | 26k+ | Yes |
| **aiogram** | Bot API wrapper | Yes (native) | HTTPS Bot API | Modern async bots, FSM | 5k+ | Yes |
| **Telethon** | MTProto client | Yes | MTProto | User clients, advanced features, testing | 10k+ | Yes |
| **Pyrogram** | MTProto client | Yes | MTProto | User clients, elegant API | 5k+ | Slow |

**For the bot itself (OBS Agent Telegram interface):**

**Recommendation: `python-telegram-bot` (PTB) v22+**
- Most mature, best documented
- Full Bot API coverage with type safety
- Built-in polling with `Application.run_polling()`
- Conversation handlers for multi-step flows
- Large community, many examples
- Pure async since v20

Example:
```python
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Forward to OBS Agent daemon
    response = await daemon_client.send(update.message.text)
    await update.message.reply_text(response)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()
```

**For testing (eval judge sending messages to the bot):**

**Recommendation: `Telethon`**
- Can act as a real Telegram user (MTProto)
- `Conversation` helper: send message, wait for bot response, with timeout
- `StringSession` for storing auth in env vars (no file-based sessions)
- Battle-tested for bot E2E testing (multiple blog posts, ShallowDepth guide)
- pytest-asyncio compatible

**Why not the others for testing?**
- PTB: designed for receiving updates as a bot, not for acting as a user
- aiogram: same — bot framework, not user client
- Pyrogram: viable alternative to Telethon but slower maintenance. `tgintegration` library built on it is promising but less mature
- `tgintegration`: nice abstraction over Pyrogram for testing, but adds dependency and Pyrogram itself is less actively maintained

### 1.6 Rate Limits and Throttling

| Scope | Limit | Notes |
|-------|-------|-------|
| Same chat | ~1 msg/s | Soft limit, returns 429 with `retry_after` |
| Different chats | 30 msg/s (free) | Can pay for up to 1000 msg/s |
| Group chat | 20 msg/min per group | Stricter for groups |
| getUpdates | No explicit limit | But avoid calling faster than every 1s |
| Inline queries | ~100/s | Not relevant for us |

**429 Too Many Requests handling:**
- Response includes `retry_after` field (seconds to wait)
- Implement exponential backoff with jitter
- Token-bucket algorithm as of Bot API 7.0 (Jan 2025)
- Bot "reputation score" (0-1000, hidden) affects limits — adaptive windows coming 2026

**For OBS Agent**: Rate limits are unlikely to be an issue for a personal bot with one user. The main concern is splitting long messages — need ~1s delay between chunks to same chat.

### 1.7 Conversation/Chat Management

**Chat types:**
- **Private** (1:1 with user) — our primary use case
- **Group** — bot added to group, sees messages based on privacy mode
- **Supergroup** — large groups with admin features
- **Channel** — broadcast only

**Key concepts:**
- `chat_id`: Unique identifier. For private chats, equals the user's `user_id`. Positive for users, negative for groups
- `message_id`: Sequential within a chat. Used for replies, edits, deletions
- `update_id`: Global sequence number across all chats. Used for `offset` in getUpdates

**For OBS Agent:**
- Restrict to private chat only (single user). Check `chat_id` matches authorized user
- Store `chat_id` in config for message delivery
- Use `reply_to_message_id` to thread responses to specific messages (optional, good UX)

### 1.8 Markdown/Formatting Support

**Three formatting modes via `parse_mode`:**

1. **MarkdownV2** (recommended by Telegram):
   - Bold: `*text*`
   - Italic: `_text_`
   - Underline: `__text__`
   - Strikethrough: `~text~`
   - Code: `` `code` ``
   - Code block: ` ```lang\ncode``` `
   - Links: `[text](url)`
   - Spoiler: `||text||`
   - **Escaping is painful**: Must escape `_*[]()~>#+\-=|{}.!` outside entities

2. **HTML**:
   - `<b>bold</b>`, `<i>italic</i>`, `<code>code</code>`, `<pre>block</pre>`
   - `<a href="url">text</a>`
   - Much simpler escaping (just `<`, `>`, `&`)

3. **Entities array** (bypass parsing entirely):
   - Send plain text + array of MessageEntity objects specifying formatting
   - Most reliable but verbose

**Recommendation for OBS Agent:**

The agent outputs standard Markdown (CommonMark). Two approaches:
1. **Convert to HTML**: Safer, simpler escaping. Convert `**bold**` → `<b>bold</b>`, `` `code` `` → `<code>code</code>`, etc. Use a library like `markdownify` in reverse or write a simple converter
2. **Use entities array**: Parse agent Markdown and generate Telegram entities. Most reliable but more work
3. **Send as MarkdownV2**: Risky — the escaping rules are different from standard Markdown and agent output will frequently break

**Recommendation: Convert to HTML.** The escaping is simpler and the agent's Markdown is close enough to convert reliably. Fall back to plain text if conversion fails.

---

## Part 2: Testing Approaches for Telegram Evals

### Current CLI Eval Architecture (for reference)

Our existing eval system:
1. **Platform abstraction** (`platform.py`): `Platform` protocol with `send()`, `send_nowait()`, `read()`, `wait_for_prompt()`, `close()`
2. **CLIPlatform**: Implements `Platform` using pexpect to drive the CLI
3. **Scenario parser** (`scenario.py`): Parses markdown scenario files into steps + criteria
4. **Judge agent** (`judge.py`): SDK agent with MCP tools (`send_message`, `read_output`) that follows scenario steps and renders PASS/FAIL verdict
5. **Dual mode**: Sequential scenarios use MCP tools; concurrent scenarios use pexpect directly and judge evaluates transcript

The key insight: **the Platform protocol is already an abstraction.** A `TelegramPlatform` that implements the same protocol would slot in with zero changes to the judge, scenario parser, or test runner.

### 2.1 Approach 1: Telethon User Client (RECOMMENDED)

**How it works:**
- Log in as a real Telegram user (the "secondary test account")
- Use Telethon's `TelegramClient` with `StringSession` to persist auth
- Use the `Conversation` helper to send messages to the bot and wait for responses
- Wrap this in a `TelegramPlatform` that implements our `Platform` protocol

**Setup requirements:**
1. **Two Telegram accounts**: Main (personal use) and secondary (eval testing)
2. **Telegram API credentials**: Register an app at https://my.telegram.org → get `api_id` and `api_hash`
3. **StringSession**: Run a one-time script to generate a session string from the secondary account. Store as env var
4. **The test bot must be started** by the secondary account (send `/start` once, manually or in test setup)

**Implementation sketch for TelegramPlatform:**

```python
from telethon import TelegramClient
from telethon.sessions import StringSession

class TelegramPlatform:
    """Telethon-based platform for eval testing of the Telegram bot."""

    def __init__(self, api_id: int, api_hash: str, session_str: str, bot_username: str):
        self._client = TelegramClient(
            StringSession(session_str), api_id, api_hash,
            sequential_updates=True,
        )
        self._bot_username = bot_username
        self._last_output = ""
        self._conv = None

    async def start(self):
        await self._client.connect()
        await self._client.get_me()
        await self._client.get_dialogs()
        self._conv = self._client.conversation(
            self._bot_username, timeout=120, max_messages=10000
        )
        await self._conv.__aenter__()
        # Send /start to ensure bot can respond
        await self._conv.send_message("/start")
        await self._conv.get_response()

    async def send(self, text: str) -> str:
        await self._conv.send_message(text)
        response = await self._conv.get_response()
        # Bot may split long messages — collect all within a short window
        parts = [response.text]
        while True:
            try:
                extra = await asyncio.wait_for(
                    self._conv.get_response(), timeout=2.0
                )
                parts.append(extra.text)
            except asyncio.TimeoutError:
                break
        self._last_output = "\n".join(parts)
        return self._last_output

    async def send_nowait(self, text: str) -> None:
        await self._conv.send_message(text)

    async def read(self) -> str:
        return self._last_output

    async def wait_for_prompt(self, timeout: int = 120) -> str:
        response = await asyncio.wait_for(
            self._conv.get_response(), timeout=timeout
        )
        parts = [response.text]
        while True:
            try:
                extra = await asyncio.wait_for(
                    self._conv.get_response(), timeout=2.0
                )
                parts.append(extra.text)
            except asyncio.TimeoutError:
                break
        self._last_output = "\n".join(parts)
        return self._last_output

    async def close(self):
        if self._conv:
            await self._conv.__aexit__(None, None, None)
        await self._client.disconnect()
```

**Assessment:**

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Reliability** | HIGH | Uses MTProto directly, no UI layer to break |
| **Setup complexity** | MEDIUM | One-time: register app, generate session string |
| **Maintenance** | LOW | Telethon is stable, sessions persist |
| **Media support** | FULL | Can send/receive all media types |
| **Content verification** | FULL | Direct access to message text, entities, media |
| **Speed** | FAST | Direct protocol, no rendering overhead |
| **Matches our pattern** | PERFECT | Implements Platform protocol directly |

**Gotchas:**
- `StringSession` must be regenerated if the account is logged out or 2FA changes
- Telethon's `Conversation` has a default limit of 100 messages — set `max_messages=10000`
- Need `sequential_updates=True` to avoid race conditions in message ordering
- The secondary Telegram account should NOT be the same as the primary account
- 2FA: if enabled on the test account, the session string handles it (generated once with password)

### 2.2 Approach 2: Playwright + Telegram Web

**How it works:**
- Launch a browser via Playwright
- Navigate to web.telegram.org
- Log in with the secondary account (session cookies persist)
- Use DOM selectors to type messages and read responses

**Assessment:**

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Reliability** | LOW | UI selectors break with Telegram Web updates |
| **Setup complexity** | HIGH | Browser install, cookie management, selector maintenance |
| **Maintenance** | HIGH | Telegram Web UI changes frequently |
| **Media support** | PARTIAL | Can screenshot but hard to extract text from media |
| **Content verification** | MEDIUM | Must parse DOM, fragile |
| **Speed** | SLOW | Browser rendering overhead |
| **Matches our pattern** | POOR | Would need complex wrapper to match Platform protocol |

**Verdict: NOT recommended.** Too fragile, too slow, doesn't match our architecture. Only consider if Telethon somehow doesn't work.

### 2.3 Approach 3: macOS Telegram App + Accessibility APIs

**How it works:**
- Use macOS Accessibility framework (via `pyobjc` or `atomacos`)
- Control the Telegram desktop app programmatically
- Read message content from accessibility tree

**Assessment:**

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Reliability** | LOW | Accessibility tree changes with app updates |
| **Setup complexity** | HIGH | Requires accessibility permissions, app-specific selectors |
| **Maintenance** | VERY HIGH | Breaks with every Telegram Desktop update |
| **Media support** | POOR | Can see elements but extracting content is unreliable |
| **Content verification** | LOW | Accessibility text may not match rendered text |
| **Speed** | MEDIUM | No browser overhead but app launch time |
| **Matches our pattern** | POOR | macOS-only, complex wrapper needed |

**Verdict: NOT recommended.** Even less reliable than Playwright, platform-locked to macOS, and the accessibility API is not designed for this.

### 2.4 Approach 4: Bot-to-Bot Self-Testing

**How it works:**
- Create a second bot (the "tester bot")
- Have the tester bot send messages to the OBS Agent bot

**Critical limitation: BOTS CANNOT MESSAGE BOTS.**

Telegram bots cannot initiate conversations with each other or send messages to each other. A bot can only respond to messages from users. This approach is fundamentally impossible.

**Verdict: IMPOSSIBLE.** Not a viable approach.

### 2.5 Approach 5: tgintegration (Pyrogram-based)

**How it works:**
- Uses Pyrogram (MTProto client) under the hood
- Provides `BotController` abstraction for testing bots
- `collect()` context manager to send commands and gather responses

```python
from tgintegration import BotController

controller = BotController(
    peer="@obs_agent_test_bot",
    client=pyrogram_client,
    max_wait=120,
    wait_consecutive=3,
    raise_no_response=True,
)

async with controller.collect(count=1) as response:
    await controller.send_command("start")
assert response.num_messages >= 1
```

**Assessment:**

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Reliability** | HIGH | Same MTProto foundation as Telethon |
| **Setup complexity** | MEDIUM | Similar to Telethon — need Pyrogram session |
| **Maintenance** | MEDIUM | tgintegration is less actively maintained than Telethon |
| **Media support** | FULL | Pyrogram has full media support |
| **Content verification** | FULL | Direct message access |
| **Speed** | FAST | Direct protocol |
| **Matches our pattern** | GOOD | `BotController` provides send/collect but not exactly Platform |

**Note**: tgintegration recently moved to TypeScript (mtcute-based). The Python version (Pyrogram-based) is at v1.2.0 and may not receive updates. Pyrogram itself has slower maintenance than Telethon.

**Verdict: Viable but Telethon is preferred** due to better maintenance and simpler integration with our Platform protocol.

### 2.6 Approach 6: Telegram Bot API Direct (for unit-level integration tests)

**How it works:**
- Use the Bot API's `getUpdates` endpoint to read what the bot received
- Use `sendMessage` via a user client to send messages
- Cross-reference sent messages with bot's received updates

This is essentially what Telethon does but at a lower level. Not useful as a standalone approach but worth noting: we can use the Bot API to verify the bot's state (e.g., check if webhook is set, get bot info) as part of test setup/teardown.

---

## Part 3: Recommended Architecture for Telegram Evals

### 3.1 The Platform Abstraction Is Key

Our existing eval infrastructure already has the right abstraction:

```python
class Platform(Protocol):
    async def send(self, text: str) -> str: ...
    async def send_nowait(self, text: str) -> None: ...
    async def read(self) -> str: ...
    async def wait_for_prompt(self, timeout: int = 120) -> str: ...
    async def close(self) -> None: ...
```

We need exactly one new implementation: `TelegramPlatform` (Telethon-based). The judge, scenario parser, and test runner remain UNCHANGED. This is the beauty of the existing design.

### 3.2 What Changes for Telegram Evals

| Component | Change | Scope |
|-----------|--------|-------|
| **platform.py** | Add `TelegramPlatform` class | New class, ~80 lines |
| **test_evals.py** | Parametrize by platform (CLI vs Telegram) | Small modification |
| **conftest.py** | Add Telegram fixtures (client, platform) | New fixtures |
| **scenarios/** | Most scenarios work as-is. Some may need longer timeouts | Minor tweaks |
| **Environment** | New env vars: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `TELEGRAM_BOT_USERNAME` | Config |

### 3.3 Unique Telegram Challenges

1. **No "prompt" concept**: CLI has a prompt pattern (OBS_EVAL>). Telegram has no equivalent. `wait_for_prompt` must be reimplemented as "wait for bot to stop sending messages" (timeout-based)

2. **Message splitting**: Bot may split a single response into multiple Telegram messages. `send()` must collect all parts within a time window (e.g., 2-3s after last message)

3. **Slower round-trips**: Telegram has network latency that pexpect doesn't. Scenarios may need longer `Wait:` values for Telegram

4. **Bot startup**: The bot must be running before evals start. This could be:
   - Started manually (simplest for now)
   - Started by the test fixture (like CLIPlatform spawns the CLI)
   - Already running as a service

5. **State isolation**: CLI evals get a fresh process per test. Telegram bot is persistent. Need to either:
   - Send a `/reset` command between tests to clear conversation state
   - Accept that Telegram evals test with persistent state (more realistic but less isolated)

### 3.4 Required Credentials and Accounts

| Item | Purpose | How to Get |
|------|---------|-----------|
| **Bot token (dev)** | Personal use bot | BotFather → `/newbot` |
| **Bot token (test)** | Eval testing bot | BotFather → `/newbot` |
| **Telegram API ID** | Telethon client auth | https://my.telegram.org |
| **Telegram API hash** | Telethon client auth | https://my.telegram.org |
| **Session string** | Persistent Telethon auth | One-time script with secondary account |
| **Secondary Telegram account** | Eval judge user | New phone number or Google Voice |

### 3.5 Environment Variables

```bash
# Bot configuration
OBS_TELEGRAM_BOT_TOKEN=123456789:ABCdef...    # Test bot token
OBS_TELEGRAM_CHAT_ID=987654321                 # Authorized user chat ID

# Eval testing (Telethon client)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef
TELEGRAM_SESSION=1BVtsOH...                    # StringSession from secondary account
TELEGRAM_BOT_USERNAME=obs_agent_test_bot
```

### 3.6 Sequence Diagram: Telegram Eval Flow

```
pytest                   TelegramPlatform         Telethon        Telegram API       OBS Bot
  |                           |                      |                |                |
  |-- run_judge(scenario) --> |                      |                |                |
  |                           |-- connect() -------> |                |                |
  |                           |                      |--- auth -----> |                |
  |                           |-- send("Hello") ---> |                |                |
  |                           |                      |-- sendMsg ---> |                |
  |                           |                      |                |-- update -----> |
  |                           |                      |                |                |-- process
  |                           |                      |                | <-- sendMsg --- |
  |                           |                      | <-- update --- |                |
  |                           | <-- "response" ----- |                |                |
  |                           |                      |                |                |
  | <-- EvalResult ---------- |                      |                |                |
```

---

## Part 4: Summary and Recommendations

### For the Bot Interface
1. **Library**: `python-telegram-bot` v22+ for the bot. Polling mode
2. **Formatting**: Convert agent Markdown to HTML for `parse_mode="HTML"`
3. **Message splitting**: Split at paragraph/sentence boundaries, max 4096 chars per message
4. **Start with text only**, add media support later
5. **Restrict to private chat**, verify `chat_id` matches authorized user

### For Eval Testing
1. **Library**: `Telethon` for the test client
2. **Architecture**: Implement `TelegramPlatform` matching existing `Platform` protocol
3. **Auth**: `StringSession` stored as env var, generated once from secondary account
4. **Same judge, same scenarios**: The judge agent and scenario parser are reusable as-is
5. **Challenges**: Message splitting collection, no prompt pattern (use timeout), longer waits

### For Interface Abstraction
The daemon currently has Telegram-specific logic nowhere — it's a clean FastAPI server with `/chat` and `/chat/stream` endpoints. The Telegram bot would be another client of these endpoints, alongside the CLI. Both implement the same flow:

```
User Input → [CLI or Telegram] → HTTP POST /chat/stream → Daemon → SDK → Agent → Response
                                                                                      ↓
User Output ← [CLI or Telegram] ← SSE stream ← ─────────────────────────────────────┘
```

The `Platform` protocol in the eval system already captures this abstraction perfectly.

### Open Questions for Team Discussion
1. Should the Telegram bot connect via HTTP (like CLI) or share the daemon's session directly?
2. Do we need a `/reset` command for test isolation, or accept persistent state in Telegram evals?
3. Should we run CLI and Telegram evals in parallel or sequentially?
4. How do we handle the one-time Telethon session generation in CI/CD?

---

## Part 5: Cross-Team Findings (from OpenClaw Research)

These patterns were discovered by the openclaw-researcher analyzing the OpenClaw codebase (a reference Telegram bot implementation using the Claude SDK). They represent battle-tested solutions to Telegram-specific problems.

### 5.1 Inbound Text Fragment Reassembly (CRITICAL)

**Problem**: When a user pastes a long message (>4096 chars), Telegram's client app auto-splits it into multiple updates. The bot receives 2-12 separate messages instead of one. Without reassembly, the agent gets truncated input.

**OpenClaw's solution** (from `src/telegram/monitor.ts`):
- Buffer incoming messages where `text.length >= 4000` chars
- Check for consecutive message IDs (gap <= 1) and time proximity (gap <= 1500ms)
- Accumulate up to 12 parts before forwarding to the agent as a single message
- Release buffer on timeout (1500ms since last fragment) or when max parts reached

**Implementation for OBS Agent:**
```python
class FragmentBuffer:
    """Reassemble Telegram text fragments into complete messages."""

    def __init__(self, max_parts=12, time_gap_ms=1500):
        self._buffer: list[str] = []
        self._last_msg_id: int | None = None
        self._last_time: float | None = None
        self._max_parts = max_parts
        self._time_gap = time_gap_ms / 1000.0

    def should_buffer(self, text: str) -> bool:
        return len(text) >= 4000

    def add(self, text: str, msg_id: int, timestamp: float) -> str | None:
        """Add a fragment. Returns assembled text if complete, None if still buffering."""
        if self._buffer and self._last_msg_id is not None:
            id_gap = msg_id - self._last_msg_id
            time_gap = timestamp - (self._last_time or 0)
            if id_gap > 1 or time_gap > self._time_gap:
                # Gap too large — flush previous buffer and start fresh
                result = "\n".join(self._buffer)
                self._buffer = [text]
                self._last_msg_id = msg_id
                self._last_time = timestamp
                return result

        self._buffer.append(text)
        self._last_msg_id = msg_id
        self._last_time = timestamp

        if len(self._buffer) >= self._max_parts:
            return self.flush()
        return None  # Still buffering

    def flush(self) -> str | None:
        if not self._buffer:
            return None
        result = "\n".join(self._buffer)
        self._buffer.clear()
        self._last_msg_id = None
        self._last_time = None
        return result
```

**Eval impact**: The `TelegramPlatform` must also handle this — when the eval judge sends a long message via Telethon, the bot may receive it as fragments. The platform's `send()` should send normally (Telethon handles the MTProto side), but we need to verify that our bot correctly reassembles fragments.

### 5.2 Media Group Buffering

**Problem**: When a user sends multiple images at once, Telegram delivers them as separate updates sharing a `media_group_id`. Processing each individually would create multiple agent invocations for one logical message.

**Solution**: Buffer updates with the same `media_group_id`, release after ~500ms timeout since the last update in the group.

**Priority**: Low — text-only MVP first, media support later.

### 5.3 HTML Formatting with Plain Text Fallback

**Problem**: Agent outputs CommonMark Markdown. Telegram's `parse_mode="HTML"` accepts a limited HTML subset. Malformed HTML causes `sendMessage` to fail entirely (400 Bad Request).

**OpenClaw's approach**:
1. Convert agent Markdown to Telegram HTML (custom IR-based pipeline)
2. If `sendMessage` with HTML fails, retry as plain text (no `parse_mode`)
3. This prevents message delivery failures from edge cases in formatting conversion

**Telegram's supported HTML tags:**
- `<b>`, `<strong>` — bold
- `<i>`, `<em>` — italic
- `<u>`, `<ins>` — underline
- `<s>`, `<strike>`, `<del>` — strikethrough
- `<code>` — inline code
- `<pre>`, `<pre><code class="language-python">` — code blocks
- `<a href="url">` — links
- `<tg-spoiler>` — spoiler
- `<blockquote>` — blockquotes (added recently)

**Implementation**: We can use a lightweight Markdown-to-HTML converter. Key mappings:
- `**bold**` → `<b>bold</b>`
- `*italic*` → `<i>italic</i>`
- `` `code` `` → `<code>code</code>`
- ` ```python\ncode``` ` → `<pre><code class="language-python">code</code></pre>`
- `[text](url)` → `<a href="url">text</a>`
- `> quote` → `<blockquote>quote</blockquote>`

### 5.4 Update Offset Persistence

**Problem**: If the bot restarts, it needs to know which updates it has already processed. Without persisting the offset, it either reprocesses old updates or skips them.

**OpenClaw's solution** (from `src/telegram/update-offset-store.ts`):
- Store `{ version: 1, lastUpdateId: N }` in a JSON file under a state directory
- Atomic writes: write to temp file with UUID name, then `os.rename()` (atomic on POSIX)
- On startup, read the file and resume from `lastUpdateId + 1`

**Implementation for OBS Agent:**
```python
import json
import os
import uuid
from pathlib import Path

class UpdateOffsetStore:
    def __init__(self, state_dir: Path):
        self._path = state_dir / "telegram_offset.json"
        state_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> int | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text())
        return data.get("lastUpdateId")

    def write(self, offset: int) -> None:
        tmp = self._path.parent / f".tmp-{uuid.uuid4()}.json"
        tmp.write_text(json.dumps({"version": 1, "lastUpdateId": offset}))
        os.rename(str(tmp), str(self._path))
```

### 5.5 Sequential Processing Per Chat

**Problem**: If multiple messages arrive rapidly, concurrent processing can cause interleaved agent responses (response to message 2 arrives before response to message 1).

**OpenClaw's solution**: grammY's `sequentialize()` middleware keyed by `chat_id`.

**Our solution**: The daemon's `SessionManager` already serializes via `asyncio.Lock`. The Telegram interface layer should also serialize message processing per chat — use an `asyncio.Lock` keyed by `chat_id` (or a single lock since we only serve one user).

### 5.6 Inbound Message Debouncing

**Problem**: Rapid consecutive messages from the same user (e.g., typing corrections, multi-line input) should be batched into a single agent invocation rather than triggering multiple responses.

**Solution**: Configurable delay (e.g., 1-2s) after the last message before forwarding to the agent. If another message arrives within the delay, reset the timer and append.

**Trade-off**: Adds latency to every message by the debounce duration. For a personal assistant, 1-2s is acceptable.

### 5.7 409 Conflict Error Handling

**Problem**: If two instances of the bot poll simultaneously, Telegram returns 409 Conflict. Only one instance can use `getUpdates` at a time.

**OpenClaw's solution**: Detect 409 errors and restart with exponential backoff.

**Our solution**: Since we run a single daemon, this should be rare. But the polling loop should handle 409 gracefully — log a warning, back off, and retry. Also relevant for dev/test scenarios where dev and test bots might accidentally share a token.
