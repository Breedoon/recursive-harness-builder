"""Shared runtime environment bootstrap for CLI, daemon, and Telegram entrypoints."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

_DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
FORMAL_TEST_REDIRECT = (
    "legacy test-profile live launch is disabled; use the host-preflight command "
    "in docs/testing.md and the dedicated obs-live-test service"
)


class LegacyFormalTestRedirect(RuntimeError):
    """Raised before a live entry point can create any runtime side effect."""


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


def _has_test_profile_argument(args: list[str]) -> bool:
    """Detect any test selector, even when a later option selects production.

    Profile resolution is a compatibility parser, not a safety gate: its final
    value discards earlier options. A live launch must not make a test request
    safe merely by appending ``--prod`` or another ``--profile`` argument.
    """
    for index, argument in enumerate(args):
        if argument in {"--test", "--test-instance"}:
            return True
        if argument == "--profile" and index + 1 < len(args):
            if args[index + 1].strip().lower() == "test":
                return True
        if argument.startswith("--profile="):
            if argument.partition("=")[2].strip().lower() == "test":
                return True
    return False


def assert_live_entrypoint_allowed(
    *,
    argv: Iterable[str] | None = None,
    environ: dict[str, str] | None = None,
) -> None:
    """Reject any legacy test selector before .env loading or profile mapping."""
    args = list(sys.argv[1:] if argv is None else argv)
    _resolve_profile(args)  # Preserve validation of incomplete profile options.
    source = os.environ if environ is None else environ
    env_profile = (source.get("OBS_PROFILE") or "").strip().lower()
    if _has_test_profile_argument(args) or env_profile == "test":
        raise LegacyFormalTestRedirect(FORMAL_TEST_REDIRECT)


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
    - production is the default; test profile remains library-only compatibility
    """

    provided_args = list(sys.argv[1:] if argv is None else argv)
    explicit_profile, explicit_prod, filtered_args = _resolve_profile(provided_args)

    explicit_env_keys = _apply_env_defaults(_read_env_file(env_path or _DEFAULT_ENV_PATH))

    env_profile = (os.environ.get("OBS_PROFILE") or "").strip().lower()
    requested_profile = explicit_profile or env_profile
    profile = "prod" if explicit_prod else requested_profile or "prod"
    os.environ["OBS_PROFILE"] = profile

    _apply_profile_prefix(profile, explicit_env_keys)
    _apply_profile_defaults(profile)

    if argv is None and mutate_argv:
        sys.argv[:] = [sys.argv[0], *filtered_args]

    return profile
