"""Shared runtime environment bootstrap for CLI, daemon, and Telegram entrypoints."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

_DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            loaded[key] = value
    return loaded


def _filter_env_file_values(values: dict[str, str], profile: str) -> dict[str, str]:
    allowed_profile_prefix = f"OBS_{profile.upper()}_"
    return {
        key: value
        for key, value in values.items()
        if not key.startswith("OBS_PROD_") or key.startswith(allowed_profile_prefix)
    }


def _apply_env_defaults(values: dict[str, str]) -> set[str]:
    existing_keys = set(os.environ)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return existing_keys


def _resolve_profile(argv: Iterable[str]) -> tuple[str | None, bool, list[str]]:
    explicit_profile: str | None = None
    explicit_prod = False
    filtered: list[str] = []
    args = list(argv)
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in {"--test", "--test-instance"}:
            explicit_profile = "test"
        elif arg == "--prod":
            explicit_profile = "prod"
            explicit_prod = True
        elif arg == "--profile":
            if idx + 1 >= len(args):
                raise SystemExit("--profile requires a value")
            explicit_profile = args[idx + 1].strip().lower()
            idx += 1
        elif arg.startswith("--profile="):
            explicit_profile = arg.partition("=")[2].strip().lower()
        else:
            filtered.append(arg)
        idx += 1
    return explicit_profile, explicit_prod, filtered


def _apply_profile_prefix(profile: str, explicit_env_keys: set[str]) -> None:
    prefix = f"OBS_{profile.upper()}_"
    for key, value in list(os.environ.items()):
        if not key.startswith(prefix):
            continue
        generic_key = "OBS_" + key[len(prefix):]
        if generic_key not in explicit_env_keys:
            os.environ[generic_key] = value


def _apply_profile_defaults(profile: str) -> None:
    if profile == "test":
        os.environ.setdefault("OBS_AGENT_MODEL", "haiku")


def bootstrap_runtime_env(
    *,
    argv: Iterable[str] | None = None,
    env_path: Path | None = None,
    mutate_argv: bool = True,
) -> str:
    """Load repo .env and resolve runtime profile into generic env vars.

    The bootstrap is intentionally conservative:
    - existing explicit environment variables win
    - profile-specific values override generic vars loaded from .env
    - existing explicit environment variables win over .env and profile mapping
    - test profile defaults the model to Haiku; prod remains the config default
    """

    provided_args = list(sys.argv[1:] if argv is None else argv)
    explicit_profile, explicit_prod, filtered_args = _resolve_profile(provided_args)

    env_values = _read_env_file(env_path or _DEFAULT_ENV_PATH)
    env_profile = (os.environ.get("OBS_PROFILE") or env_values.get("OBS_PROFILE") or "").strip().lower()
    requested_profile = explicit_profile or env_profile
    profile = "prod" if explicit_prod else ("test" if requested_profile == "prod" else requested_profile or "test")

    explicit_env_keys = _apply_env_defaults(_filter_env_file_values(env_values, profile))
    os.environ["OBS_PROFILE"] = profile

    _apply_profile_prefix(profile, explicit_env_keys)
    _apply_profile_defaults(profile)

    if argv is None and mutate_argv:
        sys.argv[:] = [sys.argv[0], *filtered_args]

    return profile
