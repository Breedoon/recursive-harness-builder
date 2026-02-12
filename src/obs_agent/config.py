"""Configuration and paths for OBS Agent."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OBSConfig:
    """Central configuration for OBS Agent."""

    vault_path: Path = field(default_factory=lambda: Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "T")
    agent_dir: str = "Agent"

    @property
    def agent_path(self) -> Path:
        return self.vault_path / self.agent_dir
