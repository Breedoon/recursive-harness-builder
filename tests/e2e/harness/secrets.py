"""Secret resolution for the E2E harness. Values are never printed.

Profiles (tests/e2e/profiles/*.toml) hold secret *references*, never values:

- ``env:NAME``      the value of NAME in the secrets file (default /workspace/obs/.env)
- ``env:NAME#i``    item ``i`` of the comma-separated list NAME (bot-token pools)
- ``file:/path``    the stripped contents of a file

Everything that leaves this module for logs, reports or the run directory is a
:func:`fingerprint` (sha256 prefix), a bot id/username, or a variable name.

INVARIANT(credential-selection): production bot tokens never reach a lane
(design-v3 §2.1 item 6, ADR-0036). :func:`production_bot_tokens` is the set every
lane token is checked against before a lane starts; only the runner-side
``prod-send`` profile may resolve one. Breaking it lets a lane poll a production
bot (the 2026-03-17 freeze: 1,983 getUpdates conflicts, new sessions frozen).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Profiles#Secrets
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DEFAULT_SECRETS_FILE = Path("/workspace/obs/.env")
HARNESS_BOTS_FILE = Path("/workspace/runtime/e2e/secrets/bots.env")


class SecretError(RuntimeError):
    """A secret reference could not be resolved (the message never holds a value)."""


def secrets_file() -> Path:
    return Path(os.environ.get("OBS_E2E_SECRETS_FILE") or DEFAULT_SECRETS_FILE)


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines exactly like obs_agent.runtime_env._read_env_file."""
    paths = [Path(path)] if path is not None else [secrets_file(), HARNESS_BOTS_FILE]
    loaded: dict[str, str] = {}
    for candidate in paths:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and value:
                loaded[key] = value
    return loaded


def resolve(ref: str, env: dict[str, str] | None = None) -> str:
    """Resolve one secret reference; raise SecretError naming only the reference."""
    if not isinstance(ref, str) or ":" not in ref:
        raise SecretError(f"bad secret reference {ref!r}: expected env:NAME[#i] or file:/path")
    kind, _, body = ref.partition(":")
    if kind == "file":
        try:
            value = Path(body).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretError(f"{ref}: unreadable ({exc.__class__.__name__})") from None
        if not value:
            raise SecretError(f"{ref}: empty")
        return value
    if kind != "env":
        raise SecretError(f"bad secret reference kind {kind!r} in {ref!r}")
    name, _, index = body.partition("#")
    values = read_env_file() if env is None else env
    raw = values.get(name)
    if not raw:
        raise SecretError(f"{ref}: {name} is not set in {secrets_file()}")
    if not index:
        return raw
    items = [item.strip() for item in raw.split(",") if item.strip()]
    try:
        return items[int(index)]
    except (ValueError, IndexError):
        raise SecretError(f"{ref}: {name} has {len(items)} item(s)") from None


def fingerprint(value: str) -> str:
    """A short, non-reversible identifier safe to print and store."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def production_bot_tokens(env: dict[str, str] | None = None) -> set[str]:
    """Every production bot token named in the secrets file (never printed)."""
    values = read_env_file() if env is None else env
    tokens: set[str] = set()
    for name in ("OBS_PROD_TELEGRAM_BOT_TOKEN", "OBS_PROD_TELEGRAM_BOT_TOKENS", "OBS_TELEGRAM_BOT_TOKEN", "OBS_TELEGRAM_BOT_TOKENS"):
        for item in (values.get(name) or "").split(","):
            if item.strip():
                tokens.add(item.strip())
    return tokens


def bot_id_of(token: str) -> str:
    """The numeric bot id prefix of a token (public; the part before ':')."""
    return token.split(":", 1)[0] if ":" in token else "?"
