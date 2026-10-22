"""Credential profiles for E2E lanes (impl-plan-v3 §3 E1 WP7; design-v3 §2.2; ADR-0017, ADR-0036).

A profile is a TOML file in ``tests/e2e/profiles/`` holding names of secrets, never
values. ``--profile dev`` gives every lane the same profile; ``--lane-profiles
dev,dev,dev,sub-1,sub-2`` gives one per lane. Each lane takes its own test bot from
the profile's pool by lane index, so two lanes never share a bot.

Shipped: ``dev`` (Claude, the single existing OAuth credential via a lane copy),
``sub-1``/``sub-2`` (GPT through the existing CLIProxyAPI Codex sub-credential),
``prod-send`` (runner-only, update-free production-bot calls; never a lane) and
``claude-2`` (an empty slot until E6 provisions a second Claude credential).

INVARIANT(credential-selection): a lane profile never resolves to a production bot
token; ``prod-send`` is refused as a lane profile. Breaking it puts a second
``getUpdates`` poller on a production bot (the 2026-03-17 freeze).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Profiles#Profiles
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .secrets import SecretError, production_bot_tokens, read_env_file, resolve

PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles"
CREDENTIAL_CLASSES = ("dev", "sub", "claude-2", "prod-send")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    credential_class: str
    route: str = "claude"
    model: str = "haiku"
    claude_credentials: str = "shared-lane-copy"
    bot_pool: str = "env:OBS_TEST_TELEGRAM_BOT_TOKENS"
    bot_token_ref: str = ""
    userbot_id_ref: str = "env:OBS_TEST_TELEGRAM_ALLOWED_USERS#0"
    needs_cliproxy: bool = False
    subcredential: str = ""
    spend_cap_usd: float = 5.0
    available: bool = True
    runner_only: bool = False
    target_forum_chat_id: int | None = None
    allowed_methods: tuple[str, ...] = ()
    lane_env: dict[str, str] = field(default_factory=dict)
    path: str = ""

    def lane_bot_token(self, lane_index: int, env: dict[str, str] | None = None) -> str:
        """This lane's test bot: item ``lane_index`` of the pool (or the pinned ref)."""
        if self.runner_only:
            raise ProfileError(f"profile {self.name!r} is runner-only and never a lane (G11)")
        ref = self.bot_token_ref or f"{self.bot_pool}#{lane_index}"
        token = resolve(ref, env)
        if token in production_bot_tokens(env):
            raise ProfileError(f"profile {self.name!r} resolves lane {lane_index} to a production bot token (G11)")
        return token

    def userbot_id(self, env: dict[str, str] | None = None) -> int:
        return int(resolve(self.userbot_id_ref, env))


def available_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.toml"))


def load_profile(name: str) -> Profile:
    path = PROFILE_DIR / f"{name}.toml"
    if not path.exists():
        raise ProfileError(f"unknown profile {name!r}; available: {', '.join(available_profiles())}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cls = data.get("credential_class")
    if cls not in CREDENTIAL_CLASSES:
        raise ProfileError(f"{path}: credential_class must be one of {CREDENTIAL_CLASSES}")
    lane_env = {str(k): str(v) for k, v in (data.get("lane_env") or {}).items()}
    return Profile(
        name=str(data.get("name") or name),
        credential_class=cls,
        route=str(data.get("route", "claude")),
        model=str(data.get("model", "haiku")),
        claude_credentials=str(data.get("claude_credentials", "shared-lane-copy")),
        bot_pool=str(data.get("bot_pool", "env:OBS_TEST_TELEGRAM_BOT_TOKENS")),
        bot_token_ref=str(data.get("bot_token_ref", "")),
        userbot_id_ref=str(data.get("userbot_id_ref", "env:OBS_TEST_TELEGRAM_ALLOWED_USERS#0")),
        needs_cliproxy=bool(data.get("needs_cliproxy", False)),
        subcredential=str(data.get("subcredential", "")),
        spend_cap_usd=float(data.get("spend_cap_usd", 5.0)),
        available=bool(data.get("available", True)),
        runner_only=bool(data.get("runner_only", False)),
        target_forum_chat_id=data.get("target_forum_chat_id"),
        allowed_methods=tuple(data.get("allowed_methods") or ()),
        lane_env=lane_env,
        path=str(path),
    )


def lane_profiles(
    args=None, *, lanes: int | None = None, profile: str | None = None, lane_profiles: str | None = None
) -> list[Profile]:
    """Resolve ``--profile``/``--lane-profiles``/``--lanes`` into one Profile per lane.

    Accepts the parsed ``obs-e2e run`` namespace or the three values as keywords.
    """
    if args is not None:
        lanes = getattr(args, "lanes", lanes)
        profile = getattr(args, "profile", profile)
        lane_profiles = getattr(args, "lane_profiles", lane_profiles)
    if lane_profiles:
        names = [n.strip() for n in lane_profiles.split(",") if n.strip()]
        if lanes is not None and lanes != len(names):
            raise ProfileError(f"--lanes {lanes} disagrees with {len(names)} --lane-profiles entries")
    else:
        names = [profile or "dev"] * (lanes or 5)
    out: list[Profile] = []
    for name in names:
        prof = load_profile(name)
        if prof.runner_only:
            raise ProfileError(f"{name!r} is runner-only (prod-send never runs a lane); use --prod-send")
        if not prof.available:
            raise ProfileError(f"profile {name!r} is not provisioned yet (claude-2 arrives from E6, edge 13)")
        out.append(prof)
    env = read_env_file()
    pool_size = len([t for t in (env.get("OBS_TEST_TELEGRAM_BOT_TOKENS") or "").split(",") if t.strip()])
    if len(out) > pool_size:
        raise ProfileError(f"{len(out)} lanes requested but the test bot pool has {pool_size} bots")
    for index, prof in enumerate(out):
        try:
            prof.lane_bot_token(index, env)
        except SecretError as exc:
            raise ProfileError(str(exc)) from None
    return out
