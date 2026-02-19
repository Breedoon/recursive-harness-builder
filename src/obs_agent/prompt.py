"""System prompt builder - reads CLAUDE.md from vault root.

Previously assembled identity, context, behavior, skills, safety, and vault map
sections in code. Now all of that lives in CLAUDE.md in the vault, following the
OpenClaw SOUL.md pattern (decision D025).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


def _read_file(path: Path) -> str:
    """Read a file, returning empty string if missing."""
    try:
        return path.read_text()
    except (FileNotFoundError, OSError):
        return ""


def build_system_prompt(config: OBSConfig) -> str:
    """Build the system prompt by reading CLAUDE.md from the vault root.

    CLAUDE.md contains all sections: identity, behavior, skills, safety,
    vault map, and dynamic context. Handles missing file gracefully.
    """
    content = _read_file(config.context_path)
    if not content:
        return (
            "You are a personal assistant backed by an Obsidian vault. "
            "Your CLAUDE.md file is missing — ask the user to restore it."
        )
    return content
