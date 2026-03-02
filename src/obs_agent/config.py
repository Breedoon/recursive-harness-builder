"""Configuration and paths for OBS Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_VAULT = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "T"
_DEFAULT_TELEGRAM_TEMP_ROOT = Path("/tmp") / "obs-agent"
_DEFAULT_TELEGRAM_TRANSCRIPTION_SCRIPT = (
    Path("/Users/breedoon/Documents/PATH/transcription/transcribe.sh")
)
_DEFAULT_CACHE_WINDOW_SECONDS = 1000 * 60 * 60  # 1000 hours; effectively no expiry for now

IMMUTABLE_PATTERNS: list[str] = [
    "Misc/Meeting Notes",
]


@dataclass
class OBSConfig:
    """Central configuration for OBS Agent."""

    vault_path: Path = field(default_factory=lambda: _DEFAULT_VAULT)
    claude_dir: str = ".claude"
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7832
    cache_window_seconds: int = _DEFAULT_CACHE_WINDOW_SECONDS
    max_queue_continuations: int = 3
    bg_fork_timeout: float = 600.0  # seconds to wait for background forks
    max_buffer_size: int = 10 * 1024 * 1024  # 10 MB SDK JSON buffer limit
    context_window_estimate_tokens: int = 200_000
    context_probe_claude_cli: bool = False

    # Telegram
    telegram_bot_token: str | None = None
    telegram_allowed_user_ids: list[int] = field(default_factory=list)
    telegram_notify_username: str | None = None
    telegram_temp_root: Path = field(default_factory=lambda: _DEFAULT_TELEGRAM_TEMP_ROOT)
    telegram_transcription_script: Path = field(
        default_factory=lambda: _DEFAULT_TELEGRAM_TRANSCRIPTION_SCRIPT
    )

    # --- Class Methods ---

    @classmethod
    def from_env(cls) -> OBSConfig:
        """Build config from environment variables, falling back to defaults."""
        kwargs: dict = {}

        if vault := os.environ.get("OBS_VAULT_PATH"):
            kwargs["vault_path"] = Path(vault)
        if host := os.environ.get("OBS_DAEMON_HOST"):
            kwargs["daemon_host"] = host
        if port := os.environ.get("OBS_DAEMON_PORT"):
            kwargs["daemon_port"] = int(port)
        if window := os.environ.get("OBS_CACHE_WINDOW"):
            kwargs["cache_window_seconds"] = int(window)
        if max_cont := os.environ.get("OBS_MAX_QUEUE_CONTINUATIONS"):
            kwargs["max_queue_continuations"] = int(max_cont)
        if bg_timeout := os.environ.get("OBS_BG_FORK_TIMEOUT"):
            kwargs["bg_fork_timeout"] = float(bg_timeout)
        if buf_size := os.environ.get("OBS_MAX_BUFFER_SIZE"):
            kwargs["max_buffer_size"] = int(buf_size)
        if context_est := os.environ.get("OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"):
            kwargs["context_window_estimate_tokens"] = int(context_est)
        if probe_cli := os.environ.get("OBS_CONTEXT_PROBE_CLAUDE_CLI"):
            kwargs["context_probe_claude_cli"] = probe_cli.strip().lower() in {"1", "true", "yes", "on"}
        if tg_token := os.environ.get("OBS_TELEGRAM_BOT_TOKEN") or os.environ.get("OBS_TELEGRAM_PROD_BOT_TOKEN"):
            kwargs["telegram_bot_token"] = tg_token
        if tg_users := os.environ.get("OBS_TELEGRAM_ALLOWED_USERS") or os.environ.get("OBS_TELEGRAM_AUTHORIZED_USER_ID"):
            kwargs["telegram_allowed_user_ids"] = [
                int(uid.strip()) for uid in tg_users.split(",") if uid.strip()
            ]
        if tg_username := (
            os.environ.get("OBS_TELEGRAM_NOTIFY_USERNAME")
            or os.environ.get("OBS_TELEGRAM_TEST_NOTIFY_USERNAME")
        ):
            kwargs["telegram_notify_username"] = tg_username.lstrip("@").strip() or None
        if tg_temp_root := os.environ.get("OBS_TELEGRAM_TEMP_ROOT"):
            kwargs["telegram_temp_root"] = Path(tg_temp_root)
        if tg_transcribe := os.environ.get("OBS_TELEGRAM_TRANSCRIPTION_SCRIPT"):
            kwargs["telegram_transcription_script"] = Path(tg_transcribe)

        return cls(**kwargs)

    # --- Claude Paths ---

    @property
    def claude_path(self) -> Path:
        return self.vault_path / self.claude_dir

    @property
    def context_path(self) -> Path:
        return self.vault_path / "CLAUDE.md"

    @property
    def skills_dir(self) -> Path:
        return self.claude_path / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.claude_path / "memory"

    @property
    def system_dir(self) -> Path:
        return self.claude_path / "system"

    @property
    def topics_dir(self) -> Path:
        return self.claude_path / "topics"

    @property
    def drafts_dir(self) -> Path:
        return self.claude_path / "drafts"

    @property
    def memory_parent_note(self) -> Path:
        return self.claude_path / "memory.md"

    # --- Daemon ---

    @property
    def base_url(self) -> str:
        return f"http://{self.daemon_host}:{self.daemon_port}"

    # --- Immutable Paths ---

    @property
    def immutable_patterns(self) -> list[str]:
        return list(IMMUTABLE_PATTERNS)

    def is_immutable(self, path: Path) -> bool:
        """Check if a path matches any immutable pattern."""
        path_str = str(path)
        return any(pattern in path_str for pattern in IMMUTABLE_PATTERNS)

    # --- Validation ---

    def validate(self) -> None:
        """Validate that expected vault structure exists. Raises FileNotFoundError."""
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault not found: {self.vault_path}")
        if not self.claude_path.is_dir():
            raise FileNotFoundError(f".claude directory not found: {self.claude_path}")
        if not self.context_path.is_file():
            raise FileNotFoundError(f"CLAUDE.md not found: {self.context_path}")
