"""Declared map of existing Telegram group chats for new-tree launches (bead ops-61 / obs-15.5).

The registry says which existing forum chats a new tree may be created in and what each one
holds, so an entry agent can say ``chat="procedures"`` instead of a raw chat id. OBS never
creates chats: a new tree is a new topic in a chat that already exists.

File format (JSON)::

    {"chats": [{"chat_id": -1003780712622, "name": "procedures",
                "aliases": ["obs", "recording"], "purpose": "...", "projects": ["..."]}]}

The file lives in the vault (purposes are vault knowledge; see Agent Operations/Chat Registry)
and is re-read on every lookup, so edits apply without a restart. A missing or malformed file
is treated as empty: a numeric chat id that OBS already has topics in still works.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REGISTRY_ENV = "OBS_CHAT_REGISTRY"
DEFAULT_RELATIVE_PATH = Path("Projects/Personal Projects/Agentic/Agent Operations/chat-registry.json")


@dataclass(frozen=True)
class ChatEntry:
    chat_id: int
    name: str
    aliases: tuple[str, ...] = ()
    purpose: str = ""
    projects: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, key: str) -> bool:
        wanted = key.strip().lower()
        return wanted == self.name.lower() or wanted in {a.lower() for a in self.aliases}


@dataclass(frozen=True)
class ChatResolution:
    chat_id: int | None
    entry: ChatEntry | None = None
    error: str | None = None


def registry_path(vault_path: Path | None) -> Path | None:
    raw = os.environ.get(REGISTRY_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    if vault_path is None:
        return None
    return Path(vault_path) / DEFAULT_RELATIVE_PATH


def load_registry(path: Path | None) -> list[ChatEntry]:
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("chats") if isinstance(data, dict) else data
    entries: list[ChatEntry] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            chat_id = int(row["chat_id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        entries.append(ChatEntry(
            chat_id=chat_id,
            name=name,
            aliases=tuple(str(a).strip() for a in row.get("aliases") or () if str(a).strip()),
            purpose=str(row.get("purpose") or "").strip(),
            projects=tuple(str(p).strip() for p in row.get("projects") or () if str(p).strip()),
        ))
    return entries


def _as_chat_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def resolve_chat(
    chat: object,
    *,
    known_chat_ids: Iterable[int],
    entries: list[ChatEntry],
) -> ChatResolution:
    """Resolve ``chat`` (an id, a registry name/alias, or None) to an existing forum chat.

    Only chats OBS already has topics in, or chats declared in the registry, are accepted.
    Nothing here can create a chat.
    """
    known = set(known_chat_ids)
    declared = {entry.chat_id: entry for entry in entries}
    names = sorted(entry.name for entry in entries)
    if chat is None or (isinstance(chat, str) and not chat.strip()):
        if len(known) == 1:
            only = next(iter(known))
            return ChatResolution(chat_id=only, entry=declared.get(only))
        hint = f"registry names: {names}" if names else f"forum chats known: {sorted(known)}"
        return ChatResolution(
            chat_id=None,
            error=f"choose a chat: pass --chat/--chat-id (CLI) or new_tree_chat (AgentTask); {hint}",
        )
    chat_id = _as_chat_id(chat)
    if chat_id is not None:
        if chat_id in known or chat_id in declared:
            return ChatResolution(chat_id=chat_id, entry=declared.get(chat_id))
        return ChatResolution(
            chat_id=None,
            error=(
                f"chat {chat_id} is not an existing forum chat OBS knows or the registry declares "
                f"(new chats are never created); known: {sorted(known | set(declared))}"
            ),
        )
    for entry in entries:
        if entry.matches(str(chat)):
            return ChatResolution(chat_id=entry.chat_id, entry=entry)
    return ChatResolution(
        chat_id=None,
        error=f"no chat named {str(chat).strip()!r} in the chat registry; names: {names or 'none declared'}",
    )
