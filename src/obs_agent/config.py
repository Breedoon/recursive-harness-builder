"""Configuration and paths for OBS Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_VAULT = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "T"

CORE_SKILLS: list[str] = [
    "update-context",
    "manage-summaries",
    "create-reference",
    "file-conventions",
]

IMMUTABLE_PATTERNS: list[str] = [
    "Misc/Meeting Notes",
]


@dataclass
class OBSConfig:
    """Central configuration for OBS Agent."""

    vault_path: Path = field(default_factory=lambda: _DEFAULT_VAULT)
    agent_dir: str = "Agent"
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7832
    cache_window_seconds: int = 3480  # 58 minutes
    classification_threshold: int = 100
    max_queue_continuations: int = 3

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
        if threshold := os.environ.get("OBS_CLASSIFICATION_THRESHOLD"):
            kwargs["classification_threshold"] = int(threshold)
        if max_cont := os.environ.get("OBS_MAX_QUEUE_CONTINUATIONS"):
            kwargs["max_queue_continuations"] = int(max_cont)

        return cls(**kwargs)

    # --- Agent Paths ---

    @property
    def agent_path(self) -> Path:
        return self.vault_path / self.agent_dir

    @property
    def context_path(self) -> Path:
        return self.agent_path / "context.md"

    @property
    def skills_dir(self) -> Path:
        return self.agent_path / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.agent_path / "memory"

    @property
    def system_dir(self) -> Path:
        return self.agent_path / "system"

    @property
    def topics_dir(self) -> Path:
        return self.agent_path / "topics"

    @property
    def drafts_dir(self) -> Path:
        return self.agent_path / "drafts"

    @property
    def memory_parent_note(self) -> Path:
        return self.agent_path / "memory.md"

    @property
    def skills_manifest(self) -> Path:
        return self.agent_path / "skills.md"

    # --- Skills ---

    @property
    def core_skills(self) -> list[str]:
        return list(CORE_SKILLS)

    def skill_path(self, name: str) -> Path:
        """Resolve a skill name to its SKILL.md file path."""
        return self.skills_dir / name / "SKILL.md"

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
        if not self.agent_path.is_dir():
            raise FileNotFoundError(f"Agent directory not found: {self.agent_path}")
        if not self.context_path.is_file():
            raise FileNotFoundError(f"context.md not found: {self.context_path}")
