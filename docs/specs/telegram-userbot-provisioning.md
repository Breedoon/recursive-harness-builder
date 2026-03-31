# Telegram Userbot Provisioning Specification

**Status**: Approved for implementation
**Date**: 2026-03-31

## Objectives

1. Add a user-facing `/new-group` flow that provisions a fresh Telegram forum supergroup through the userbot account.
2. Keep the current multi-bot sender pool and round-robin behavior unchanged, while ensuring only the primary bot has official Telegram commands registered.
3. Add a profile-scoped userbot credential model for runtime use instead of reusing test-harness-only env names.
4. Treat `/new-bot` as secondary: implement it only if the BotFather automation remains straightforward and robust.

## Confirmed Product Decisions

1. Ownership transfer is out of scope. The userbot remains the creator/owner.
2. Only explicitly whitelisted Telegram users may invoke these commands.
3. The default invited/admined human user is the command sender.
4. A forum supergroup is required, not a plain supergroup.
5. Forum topic selection must use the list layout, not tabs.
6. New runtime env vars must use the existing profile-prefix convention.
7. The current test environment where the Telethon userbot and the main allowed user are the same account is a real edge case and must be handled deliberately.

## Command Surface

### Telegram Command Naming Constraint

Telegram official bot commands may only contain lowercase English letters, digits, and underscores.

Implication:

1. The officially registered commands must be `new_group` and `new_bot`.
2. The runtime should also accept raw-text aliases `/new-group` and `/new-bot` for the preferred user-facing syntax.
3. Secondary bots must not register these commands with Telegram.

## `/new-group`

### User-Facing Behavior

Accepted forms:

1. `/new-group <group title>`
2. `/new_group <group title>`
3. `/new-group --user @handle <group title>`
4. `/new-group --user <numeric_id> <group title>`

Rules:

1. If `--user` is omitted, the command sender is the target human user.
2. The group title is required.
3. The command must work when addressed with `@botusername`.

### Provisioning Flow

1. Validate that the command sender is in `OBS_TELEGRAM_ALLOWED_USERS`.
2. Resolve the target user:
   - default: command sender
   - optional override: `--user @handle` or `--user <numeric_id>`
3. Open the Telethon userbot session from the runtime profile-scoped env.
4. Create a new forum supergroup.
5. Ensure the forum uses list layout by calling the Telegram full API with `tabs=false`.
6. Invite the target human user if the target user is not the creator account.
7. Promote the target human user to admin.
8. Discover all configured bot accounts from `OBS_TELEGRAM_BOT_TOKEN` plus `OBS_TELEGRAM_BOT_TOKENS`.
9. Invite every configured bot to the new group.
10. Promote every configured bot to admin with the permissions needed for topic-based operation.
11. Keep the userbot as owner. Do not attempt ownership transfer.
12. Leave the group only when the creator account and the target human user are different accounts.
13. If the creator account and target human user are the same account, do not leave the group.
14. Reply in the source chat with a concise provisioning summary that includes the new chat title, chat ID, and which bots were added.

### Required Admin Rights

Bots should receive at least the rights needed by the current production/test flows:

1. `change_info`
2. `delete_messages`
3. `invite_users`
4. `pin_messages`
5. `manage_topics`

Avoid unnecessary expansion beyond what the bot needs for forum/topic operation.

### Forum Layout Requirement

The forum must be created or normalized with list layout, not tabs.

Implementation requirement:

1. Use Telegram's full API forum toggle path with `enabled=true` and `tabs=false`.
2. Verify the resulting channel metadata reports the forum-tabs flag as disabled.

## Single-Bot Command Registration

The current sender-pool architecture should remain intact:

1. The primary bot continues to poll updates and register official Telegram commands.
2. Secondary sender bots remain send-only participants in the pool.
3. Startup should explicitly clear commands on secondary bots so Telegram does not show duplicate slash menus from stale prior registration.

## Runtime Environment Model

### New Generic Runtime Keys

Add these generic config/env fields:

1. `OBS_TELEGRAM_USERBOT_API_ID`
2. `OBS_TELEGRAM_USERBOT_API_HASH`
3. `OBS_TELEGRAM_USERBOT_SESSION`

### Profile-Scoped Source of Truth

The `.env` file should store profile-specific values, not generic cross-profile values:

1. `OBS_PROD_TELEGRAM_USERBOT_API_ID`
2. `OBS_PROD_TELEGRAM_USERBOT_API_HASH`
3. `OBS_PROD_TELEGRAM_USERBOT_SESSION`
4. `OBS_TEST_TELEGRAM_USERBOT_API_ID`
5. `OBS_TEST_TELEGRAM_USERBOT_API_HASH`
6. `OBS_TEST_TELEGRAM_USERBOT_SESSION`

Runtime behavior:

1. `bootstrap_runtime_env` keeps mapping `OBS_<PROFILE>_*` keys into generic `OBS_*` keys.
2. Production commands must read only the generic `OBS_TELEGRAM_USERBOT_*` keys after bootstrap.
3. Tests may continue to use the dedicated test harness env names until the harness is migrated.

## `/new-bot`

### Priority

`/new-bot` is a secondary objective. It should only ship if the BotFather flow is clean and reliable enough after `/new-group` is done.

### Target Behavior

1. Use the userbot session to converse with `@BotFather`.
2. Create a new bot with display name `Claudia`.
3. Use a globally unique username pattern to avoid collisions.
4. Configure the bot for OBS group usage:
   - groups enabled
   - privacy disabled if required for current OBS group/forum behavior
5. Append the new token to the active profile's `OBS_<PROFILE>_TELEGRAM_BOT_TOKENS` entry in `.env`.
6. Do not register official Telegram commands on the new secondary bot.
7. It is acceptable for the daemon restart to be manual or required after token changes.

### Username Pattern

Use a deterministic unique pattern, for example:

1. `ClaudiaObsProd<unix_ts>Bot`
2. `ClaudiaObsTest<unix_ts>Bot`

The active profile should be encoded in the username suffix to reduce ambiguity.

## Implementation Approach

1. Extract the reusable Telethon provisioning logic from the test harness into production code or a shared module.
2. Keep the Bot API runtime path separate from the Telethon userbot helper path.
3. Add config parsing for the new generic `OBS_TELEGRAM_USERBOT_*` keys.
4. Wire raw-text alias handling for `/new-group` and `/new-bot`.
5. Register only underscore-based official commands on the primary bot.
6. Clear official commands from secondary bots during startup.

## Edge Cases

1. Creator account equals command sender:
   - do not invite self
   - do not leave the group
   - still add/promote bots
2. No extra sender bots configured:
   - `/new-group` still succeeds with the primary bot only
3. Target user resolution fails:
   - fail clearly without creating a partial chat if possible
4. Bot invite/promotion partly fails:
   - surface which bots were added/promoted and which were not
5. Forum layout normalization fails:
   - treat as provisioning failure because list layout is a requirement
6. Secondary bots have stale Telegram commands:
   - startup cleanup must clear them

## Test Plan

### Deterministic / Unit

1. Command parser accepts both hyphen and underscore aliases.
2. Official command registration uses underscore names only.
3. Secondary-bot command cleanup is invoked for non-primary tokens only.
4. Default target-user resolution selects the command sender.
5. `--user` override resolves both `@handle` and numeric ID.
6. Creator-equals-target path skips the userbot leave step.
7. Sender-token discovery preserves primary-first ordering and dedupes entries.
8. Profile-scoped env mapping resolves `OBS_<PROFILE>_TELEGRAM_USERBOT_*` correctly.

### Live `/new-group`

1. Run the command through the real bot from the whitelisted Telegram account.
2. In the current test environment, validate the creator-equals-target edge case:
   - group is created
   - forum mode is enabled
   - list layout is enabled (`tabs=false`)
   - primary bot is invited/admined
   - userbot does not leave
3. Send a real prompt in the created group and confirm the bot responds.
4. Run an existing smoke-style prompt that exercises normal messaging in the new group.
5. If multiple sender bot tokens are configured, run the existing multi-bot sender-pool smoke path and confirm more than one bot sender ID appears in recent messages.

### Live `/new-bot`

Only if `/new-bot` is implemented:

1. Cap total created bots across all tests to at most 5.
2. Create at most one or two bots during development verification unless re-runs are required.
3. After token append and daemon restart, create a fresh group and verify the new bot can be invited, admined, and used in the sender pool.

## References

1. Telegram Bot API command objects: command names are limited to lowercase English letters, digits, and underscores.
2. Telegram full API forums doc: forums support list or tabbed UI through `channels.toggleForum(..., tabs=...)`.
3. Telegram full API forums doc: user-local "View as messages" is a separate per-account client preference and is not the same as forum tab/list layout.

Official sources:

1. https://core.telegram.org/api/forum
2. https://core.telegram.org/bots/api
