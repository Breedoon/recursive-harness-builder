"""Telethon-backed Telegram provisioning helpers for userbot-only operations."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from obs_agent.runtime_env import _DEFAULT_ENV_PATH

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

try:  # pragma: no cover - optional import path exercised in integration
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import (
        CreateChannelRequest,
        EditAdminRequest,
        InviteToChannelRequest,
        LeaveChannelRequest,
        ToggleForumRequest,
    )
    from telethon.tl.types import Channel, ChatAdminRights
    from telethon.utils import get_peer_id
except Exception:  # pragma: no cover - absence handled at runtime
    TelegramClient = None
    StringSession = None
    CreateChannelRequest = None
    EditAdminRequest = None
    InviteToChannelRequest = None
    LeaveChannelRequest = None
    ToggleForumRequest = None
    Channel = None
    ChatAdminRights = None
    get_peer_id = None


logger = logging.getLogger("obs_agent.telegram_userbot")

_BOT_TOKEN_RE = re.compile(r"(\d{6,}:[A-Za-z0-9_-]{20,})")
_USERNAME_OK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}[Bb]ot$")


@dataclass(frozen=True)
class CreatedBot:
    display_name: str
    username: str
    token: str
    profile: str
    env_key: str
    groups_enabled: bool
    privacy_disabled: bool


@dataclass(frozen=True)
class ProvisionedGroup:
    title: str
    chat_id: int
    target_user_id: int | None
    target_username: str | None
    added_bot_usernames: tuple[str, ...]
    creator_left: bool
    forum_tabs_enabled: bool


def current_obs_profile() -> str:
    profile = (os.environ.get("OBS_PROFILE") or "").strip().lower()
    return profile or "prod"


def profile_env_key(profile: str, suffix: str) -> str:
    normalized = (profile or "prod").strip().upper()
    return f"OBS_{normalized}_{suffix}"


def append_profile_bot_token(
    token: str,
    *,
    profile: str | None = None,
    env_path: Path | None = None,
    primary_token: str | None = None,
) -> tuple[str, list[str]]:
    """Append *token* to the active profile token list in .env and process env."""
    resolved_profile = current_obs_profile() if profile is None else profile.strip().lower() or "prod"
    target_key = profile_env_key(resolved_profile, "TELEGRAM_BOT_TOKENS")
    profile_primary_key = profile_env_key(resolved_profile, "TELEGRAM_BOT_TOKEN")
    path = (env_path or _DEFAULT_ENV_PATH).expanduser()

    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    existing_raw = os.environ.get(target_key, "").strip()
    if not existing_raw:
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == target_key:
                existing_raw = value.strip()
                break

    tokens: list[str] = []
    if existing_raw:
        tokens.extend([item.strip() for item in existing_raw.split(",") if item.strip()])
    else:
        file_primary = ""
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == profile_primary_key:
                file_primary = value.strip()
                break
        seed_primary = (
            primary_token
            or os.environ.get(profile_primary_key, "").strip()
            or file_primary
            or os.environ.get("OBS_TELEGRAM_BOT_TOKEN", "").strip()
        )
        if seed_primary:
            tokens.append(seed_primary)

    if token not in tokens:
        tokens.append(token)

    rendered = ",".join(tokens)
    updated = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        if key.strip() == target_key:
            lines[idx] = f"{target_key}={rendered}"
            updated = True
            break
    if not updated:
        lines.append(f"{target_key}={rendered}")

    if lines:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path.write_text(f"{target_key}={rendered}\n", encoding="utf-8")

    os.environ[target_key] = rendered
    if current_obs_profile() == resolved_profile:
        os.environ["OBS_TELEGRAM_BOT_TOKENS"] = rendered
    return target_key, tokens


class TelegramUserbotProvisioner:
    """Provision chats and bots via a Telethon user session."""

    def __init__(
        self,
        config: "OBSConfig",
        *,
        env_path: Path | None = None,
    ) -> None:
        self._config = config
        self._env_path = env_path or _DEFAULT_ENV_PATH

    async def create_group(
        self,
        *,
        title: str,
        default_user_id: int,
        default_username: str | None = None,
        target_override: str | None = None,
    ) -> ProvisionedGroup:
        self._ensure_available()
        async with self._client() as client:
            me = await client.get_me()
            created = await client(
                CreateChannelRequest(
                    title=title,
                    about="OBS provisioned forum group",
                    megagroup=True,
                    forum=True,
                )
            )
            channel = next((chat for chat in created.chats if isinstance(chat, Channel)), None)
            if channel is None:
                raise RuntimeError("CreateChannelRequest did not return a channel")
            await client(ToggleForumRequest(channel=channel, enabled=True, tabs=False))
            channel = await client.get_entity(channel)

            target_entity = await self._resolve_target_user(
                client,
                default_user_id=default_user_id,
                default_username=default_username,
                target_override=target_override,
            )
            target_user_id = getattr(target_entity, "id", None)
            target_username = getattr(target_entity, "username", None)

            if target_user_id is not None and target_user_id != getattr(me, "id", None):
                await self._safe_invite(client, channel, target_entity)
                await self._promote_admin(client, channel, target_entity, rank="obs-owner-user")

            bot_entities, bot_usernames = await self._discover_bot_entities(client)
            for entity in bot_entities:
                await self._safe_invite(client, channel, entity)
                await self._promote_admin(client, channel, entity, rank="obs-forum-bot")

            refreshed = await client.get_entity(channel)
            forum_tabs_enabled = bool(getattr(refreshed, "forum_tabs", False))
            if forum_tabs_enabled:
                raise RuntimeError("forum list layout requirement failed: forum_tabs is still enabled")

            creator_left = False
            if target_user_id is not None and target_user_id != getattr(me, "id", None):
                await client(LeaveChannelRequest(channel=channel))
                creator_left = True

            return ProvisionedGroup(
                title=title,
                chat_id=int(get_peer_id(refreshed)),
                target_user_id=target_user_id if isinstance(target_user_id, int) else None,
                target_username=target_username if isinstance(target_username, str) and target_username else None,
                added_bot_usernames=tuple(bot_usernames),
                creator_left=creator_left,
                forum_tabs_enabled=forum_tabs_enabled,
            )

    async def create_bot(self, *, display_name: str = "Claudia") -> CreatedBot:
        self._ensure_available()
        profile = current_obs_profile()
        async with self._client() as client:
            username: str | None = None
            token: str | None = None
            botfather = await client.get_entity("BotFather")
            await self._reset_botfather(client, botfather)
            await self._botfather_exchange(client, botfather, "/newbot", timeout=30.0)
            await self._botfather_exchange(client, botfather, display_name, timeout=30.0)

            last_response_text = ""
            for attempt in range(5):
                candidate = self._candidate_username(display_name, profile=profile, attempt=attempt)
                response_text = await self._botfather_exchange(
                    client,
                    botfather,
                    candidate,
                    timeout=45.0,
                )
                last_response_text = response_text
                maybe_token = self._extract_token(response_text)
                if maybe_token is not None:
                    username = candidate
                    token = maybe_token
                    break
                normalized = response_text.lower()
                if "already taken" in normalized or "another username" in normalized:
                    continue
            if token is None or username is None:
                raise RuntimeError(
                    "BotFather did not return a bot token after 5 attempts"
                    + (f": {last_response_text}" if last_response_text else "")
                )

            groups_enabled = await self._configure_botfather_toggle(
                client,
                botfather=botfather,
                command="/setjoingroups",
                bot_username=username,
                choice="Enable",
            )
            privacy_disabled = await self._configure_botfather_toggle(
                client,
                botfather=botfather,
                command="/setprivacy",
                bot_username=username,
                choice="Disable",
            )

        env_key, _ = append_profile_bot_token(
            token,
            profile=profile,
            env_path=self._env_path,
            primary_token=self._config.telegram_primary_bot_token,
        )
        return CreatedBot(
            display_name=display_name,
            username=username,
            token=token,
            profile=profile,
            env_key=env_key,
            groups_enabled=groups_enabled,
            privacy_disabled=privacy_disabled,
        )

    def _ensure_available(self) -> None:
        if TelegramClient is None or StringSession is None:
            raise RuntimeError("Telethon is not installed; userbot provisioning is unavailable")
        if self._config.telegram_userbot_api_id is None:
            raise RuntimeError("OBS_TELEGRAM_USERBOT_API_ID is required for userbot provisioning")
        if not self._config.telegram_userbot_api_hash:
            raise RuntimeError("OBS_TELEGRAM_USERBOT_API_HASH is required for userbot provisioning")
        if not self._config.telegram_userbot_session:
            raise RuntimeError("OBS_TELEGRAM_USERBOT_SESSION is required for userbot provisioning")

    def _client(self):
        provisioner = self

        class _ManagedClient:
            def __init__(self) -> None:
                self.client: TelegramClient | None = None

            async def __aenter__(self) -> TelegramClient:
                self.client = TelegramClient(
                    StringSession(provisioner._config.telegram_userbot_session),
                    provisioner._config.telegram_userbot_api_id,
                    provisioner._config.telegram_userbot_api_hash,
                )
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise RuntimeError("Telethon session not authorized")
                return self.client

            async def __aexit__(self, exc_type, exc, tb) -> None:
                if self.client is not None:
                    await self.client.disconnect()

        return _ManagedClient()

    async def _resolve_target_user(
        self,
        client: TelegramClient,
        *,
        default_user_id: int,
        default_username: str | None,
        target_override: str | None,
    ):
        ref = (target_override or "").strip()
        if ref:
            if ref.startswith("@"):
                return await client.get_entity(ref)
            if ref.isdigit():
                return await client.get_entity(int(ref))
            raise RuntimeError(f"unsupported user reference: {ref}")

        if isinstance(default_username, str) and default_username.strip():
            try:
                return await client.get_entity(f"@{default_username.lstrip('@').strip()}")
            except Exception:
                logger.debug("Failed resolving default username; falling back to numeric user id", exc_info=True)
        return await client.get_entity(int(default_user_id))

    async def _discover_bot_entities(
        self,
        client: TelegramClient,
    ) -> tuple[list[Any], list[str]]:
        entities: list[Any] = []
        usernames: list[str] = []
        seen_ids: set[int] = set()
        for token in self._config.telegram_sender_bot_tokens:
            payload = await self._bot_api_post("getMe", {}, token=token)
            result = payload.get("result", {})
            username = result.get("username")
            if not isinstance(username, str) or not username.strip():
                continue
            entity = await client.get_entity(username.strip())
            user_id = getattr(entity, "id", None)
            if not isinstance(user_id, int) or user_id in seen_ids:
                continue
            entities.append(entity)
            usernames.append(username.strip())
            seen_ids.add(user_id)
        return entities, usernames

    async def _safe_invite(self, client: TelegramClient, channel: Any, entity: Any) -> None:
        try:
            await client(InviteToChannelRequest(channel=channel, users=[entity]))
        except Exception:
            logger.debug(
                "Failed inviting user_id=%s to channel_id=%s",
                getattr(entity, "id", None),
                getattr(channel, "id", None),
                exc_info=True,
            )

    async def _promote_admin(self, client: TelegramClient, channel: Any, entity: Any, *, rank: str) -> None:
        rights = ChatAdminRights(
            change_info=True,
            delete_messages=True,
            invite_users=True,
            pin_messages=True,
            manage_topics=True,
            other=True,
        )
        await client(
            EditAdminRequest(
                channel=channel,
                user_id=entity,
                admin_rights=rights,
                rank=rank,
            )
        )

    async def _bot_api_post(
        self,
        method: str,
        data: dict[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data=data,
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"{method} failed: {payload}")
        return payload

    async def _reset_botfather(self, client: TelegramClient, botfather: Any) -> None:
        try:
            await self._botfather_exchange(
                client,
                botfather,
                "/cancel",
                timeout=8.0,
            )
        except Exception:
            return

    async def _botfather_exchange(
        self,
        client: TelegramClient,
        botfather: Any,
        text: str,
        *,
        timeout: float,
    ) -> str:
        baseline_id = await self._latest_peer_message_id(client, botfather)
        await client.send_message(botfather, text)
        response = await self._wait_for_bot_reply(
            client,
            botfather,
            after_id=baseline_id,
            timeout=timeout,
        )
        return response

    async def _latest_peer_message_id(self, client: TelegramClient, peer: Any) -> int:
        async for message in client.iter_messages(peer, limit=1):
            return int(message.id)
        return 0

    async def _wait_for_bot_reply(
        self,
        client: TelegramClient,
        peer: Any,
        *,
        after_id: int,
        timeout: float,
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            newest_match: tuple[int, str] | None = None
            async for message in client.iter_messages(peer, limit=12):
                if int(message.id) <= after_id:
                    continue
                if bool(getattr(message, "out", False)):
                    continue
                text = str(getattr(message, "message", "") or "")
                if newest_match is None or int(message.id) < newest_match[0]:
                    newest_match = (int(message.id), text)
            if newest_match is not None:
                return newest_match[1]
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for BotFather reply after sending message")
            await asyncio.sleep(1.0)

    async def _configure_botfather_toggle(
        self,
        client: TelegramClient,
        *,
        botfather: Any,
        command: str,
        bot_username: str,
        choice: str,
    ) -> bool:
        try:
            await self._reset_botfather(client, botfather)
            await self._botfather_exchange(client, botfather, command, timeout=20.0)
            await self._botfather_exchange(client, botfather, f"@{bot_username}", timeout=20.0)
            response = await self._botfather_exchange(client, botfather, choice, timeout=20.0)
            text = response.lower()
            return "enabled" in text or "disabled" in text or "success" in text or "updated" in text
        except Exception:
            logger.warning(
                "BotFather toggle failed command=%s bot=%s choice=%s",
                command,
                bot_username,
                choice,
                exc_info=True,
            )
            return False

    def _candidate_username(self, display_name: str, *, profile: str, attempt: int) -> str:
        base = re.sub(r"[^A-Za-z0-9]", "", display_name.title()) or "Claudia"
        profile_part = "Prod" if profile == "prod" else profile.title()
        ts = int(time.time())
        nonce = ""
        if attempt > 0:
            nonce = f"{random.randint(100, 999)}"
        candidate = f"{base}Obs{profile_part}{ts}{nonce}Bot"
        candidate = re.sub(r"[^A-Za-z0-9_]", "", candidate)
        if len(candidate) > 32:
            candidate = candidate[:29] + "Bot"
        if not _USERNAME_OK_RE.match(candidate):
            raise RuntimeError(f"Generated invalid bot username candidate: {candidate}")
        return candidate

    def _extract_token(self, text: str) -> str | None:
        match = _BOT_TOKEN_RE.search(text or "")
        if not match:
            return None
        return match.group(1)
