"""Create the isolated filesystem and explicit environment of an E2E lane."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import secrets

FIXTURE_PATHS = (
    ".claude/settings.json",
    ".mcp.json",
    "Projects/Personal Projects/Agentic/Agent Operations/scripts/",
    "Projects/Personal Projects/Agentic/Agentic Fractals/hooks/",
)
FIXTURE_INSTRUCTIONS = (
    Path(__file__).resolve().parents[3] / "deploy/obs-e2e/fixture-vault/CLAUDE.md"
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NEVER_NAMES = {
    "OBS_PROFILE",
    "OBS_TELEGRAM_BOT_TOKENS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OBS_CLI_RELEASES_FILE",
}
_NEVER_PREFIXES = ("OBS_PROD_", "OBS_TEST_", "OBS_TELEGRAM_USERBOT_", "CLAUDE_")
_LANE_JSON_FIELDS = {
    "run_id", "lane", "index", "profile", "credential_class", "route", "model",
    "code_dir", "harness_root", "cli_path", "lane_dir", "bot_id", "bot_username",
    "proxy_port", "needs_cliproxy", "env_names", "container", "created_at",
}


def _lane_path(run_dir: Path, lane: str) -> Path:
    if not lane or Path(lane).name != lane or lane in {".", ".."}:
        raise ValueError("lane must be a single directory name")
    return Path(run_dir) / "lanes" / lane


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def make_lane_dirs(run_dir: Path, lane: str) -> Path:
    lane_dir = _lane_path(run_dir, lane)
    _private_dir(lane_dir.parent)
    _private_dir(lane_dir)
    for relative in (
        "state", "logs", "home", "vault", "supervisor", "bin",
        "state/obs-cli", "logs/obs-cli", "logs/cache-proxy", "home/.claude",
    ):
        _private_dir(lane_dir / relative)
    return lane_dir


def build_fixture_vault(
    lane_dir: Path,
    vault_src: Path = Path("/workspace/runtime/git/obs-vault-active"),
    extra_claude_md: Path | None = None,
) -> Path:
    vault = Path(lane_dir) / "vault"
    _private_dir(vault)
    # INVARIANT(side-effects): archive only this allow-list; a full clone exposes personal files.
    # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
    archive = subprocess.Popen(
        ["git", "-C", str(vault_src), "archive", "HEAD", "--", *FIXTURE_PATHS],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        extracted = subprocess.run(
            ["tar", "-x", "-C", str(vault)],
            stdin=archive.stdout,
            capture_output=True,
            check=False,
        )
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
        _, archive_error = archive.communicate()
    if archive.returncode:
        raise RuntimeError(
            "fixture git archive failed: " + archive_error.decode("utf-8", "replace").strip()
        )
    if extracted.returncode:
        raise RuntimeError(
            "fixture extraction failed: " + extracted.stderr.decode("utf-8", "replace").strip()
        )
    instructions = (
        FIXTURE_INSTRUCTIONS.read_text(encoding="utf-8")
        if FIXTURE_INSTRUCTIONS.exists()
        else "# E2E fixture vault\n"
    )
    if extra_claude_md is not None:
        instructions = instructions.rstrip() + "\n\n" + Path(extra_claude_md).read_text(encoding="utf-8")
    (vault / "CLAUDE.md").write_text(instructions, encoding="utf-8")
    for relative in (".claude", ".claude/agents", ".claude/skills", ".claude/projects", ".claude/teams"):
        _private_dir(vault / relative)
    git_env = dict(os.environ)
    git_env.update({
        "GIT_AUTHOR_NAME": "obs-e2e", "GIT_AUTHOR_EMAIL": "obs-e2e@localhost",
        "GIT_COMMITTER_NAME": "obs-e2e", "GIT_COMMITTER_EMAIL": "obs-e2e@localhost",
    })
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(vault), *args], env=git_env, check=True, capture_output=True)
    remotes = subprocess.run(
        ["git", "-C", str(vault), "remote"], check=True, capture_output=True, text=True,
    ).stdout
    if remotes.strip():
        raise RuntimeError("fixture vault must not have a Git remote")
    return vault


def _validate_env(env: dict[str, str]) -> None:
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("invalid environment variable name")
        if not isinstance(value, str):
            raise ValueError(f"{name}: environment value must be a string")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"{name}: environment value contains a line break or NUL")


def lane_env(
    profile,
    *,
    run_dir: Path,
    lane: str,
    lane_index: int,
    code_dir: Path,
    harness_root: Path,
    bot_token: str,
    userbot_id: int,
) -> dict[str, str]:
    lane_dir = _lane_path(run_dir, lane)
    # INVARIANT(auth): construct from scratch; inherited env can expose production credentials.
    # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
    env = {
        "PATH": f"{lane_dir}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/home/agent", "LANG": "C.UTF-8", "TZ": "UTC",
        "OBS_STATE_DIR": "/workspace/runtime/state",
        "OBS_TELEGRAM_STATE_DB_PATH": "/workspace/runtime/state/telegram-state.sqlite3",
        "OBS_CONTROL_DIR": "/workspace/runtime/state/obs-control",
        "OBS_SESSION_REPLAY_PROVENANCE_DIR": "/workspace/runtime/state/session-replay",
        "OBS_TELEGRAM_TEMP_ROOT": "/workspace/runtime/state/telegram-tmp",
        "OBS_RUNTIME_LOG_FILE": "/workspace/runtime/logs/telegram-prod.log",
        "OBS_VAULT_PATH": "/workspace/runtime/git/obs-vault-active",
        "CACHE_PROXY_LOG_DIR": "/workspace/runtime/logs/cache-proxy",
        "CACHE_PROXY_REQUEST_LOG": "1", "CACHE_PROXY_REQUEST_LOG_MAX_GB": "2",
        "OBS_CACHE_PROXY_PORT": "29925", "OBS_CACHE_PROXY_ENABLED": "1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:29925",
        "CLI_PROXY_BASE_URL": "http://127.0.0.1:8317",
        "OBS_CLI_PROXY_BASE_URL": "http://127.0.0.1:8317",
        "OBS_CLI_STATE_DIR": "/workspace/runtime/state/obs-cli",
        "OBS_CLI_LOG_DIR": "/workspace/runtime/logs/obs-cli",
        "OBS_TELEGRAM_BOT_TOKEN": bot_token,
        "OBS_TELEGRAM_ALLOWED_USERS": str(userbot_id),
        "OBS_TELEGRAM_PRINCIPAL_USER_ID": str(userbot_id),
        "OBS_TELEGRAM_DROP_PENDING_UPDATES": "1",
        "OBS_E2E_EVENT_LOG": str(lane_dir / "events.jsonl"),
        "OBS_E2E_LANE": lane, "OBS_E2E_RUN_DIR": str(run_dir),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "OBS_E2E_CODE_DIR": str(code_dir), "OBS_E2E_HARNESS_ROOT": str(harness_root),
    }
    env.update(profile.lane_env)
    _validate_env(env)
    forbidden = sorted(name for name in env if name in _NEVER_NAMES or name.startswith(_NEVER_PREFIXES))
    if forbidden:
        raise ValueError("forbidden lane environment names: " + ", ".join(forbidden))
    if env["OBS_TELEGRAM_BOT_TOKEN"] in secrets.production_bot_tokens():
        raise ValueError("OBS_TELEGRAM_BOT_TOKEN resolves to a production bot")
    return env


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), mode)
            out.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_env_file(path: Path, env: dict[str, str]) -> None:
    _validate_env(env)
    _atomic_write(Path(path), "".join(f"{name}={env[name]}\n" for name in sorted(env)), 0o600)


def write_lane_json(lane_dir: Path, data: dict) -> Path:
    data = dict(data)
    if "env" in data:
        data["env_names"] = sorted(data.pop("env"))
    unexpected = set(data) - _LANE_JSON_FIELDS
    if unexpected:
        raise ValueError("unexpected lane.json fields: " + ", ".join(sorted(unexpected)))
    if "env_names" not in data:
        raise ValueError("lane.json requires env_names from the generated env file")
    data["env_names"] = sorted(set(data["env_names"]))
    data.setdefault("lane_dir", str(lane_dir))
    data.setdefault("proxy_port", 29925)
    data.setdefault("cli_path", None)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    path = Path(lane_dir) / "lane.json"
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", 0o600)
    return path


def write_lane_bin(lane_dir: Path, code_dir: Path) -> Path:
    if not Path(code_dir).is_absolute():
        raise ValueError("code_dir must be absolute")
    path = Path(lane_dir) / "bin" / "obs-agentctl"
    _atomic_write(
        path,
        '#!/bin/bash\nexport PYTHONPATH="$OBS_E2E_CODE_DIR/src"\n'
        'exec /workspace/obs/.venv/bin/python -m obs_agent.control "$@"\n',
        0o700,
    )
    return path
