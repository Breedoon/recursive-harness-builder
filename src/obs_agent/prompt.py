"""System prompt helpers.

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


def build_obs_platform_appendix() -> str:
    """Return OBS-specific runtime instructions appended to every session."""
    return (
        "OBS platform notes:\n"
        "- Native Task and ForkTask are disabled in this environment. "
        "Use AgentTask to launch subagents, AgentTaskOutput to inspect them, and "
        "AgentTaskStop to stop or interrupt them.\n"
        "- Teams are enabled by default for all agents in the same lineage tree. "
        "Use SendInboxMessage to message teammates, ReadInbox to read teammate messages, "
        "and search_team to discover parent/children/siblings/ancestors/descendants/tree members.\n"
        "- Your lineage defines your location in the team tree. "
        "Lineage is the ordered list of agent names from the trunk agent to you. "
        "Example: [Trunk, Research, Parser] means you are the Parser agent inside "
        "Research inside Trunk, and you can message any other agent in that same tree.\n"
        "- Use the session_lineage tool when you need your exact lineage, root team key, "
        "or agent name.\n"
        "- If you are resumed or woken because teammate messages may have arrived, "
        "check ReadInbox before assuming there is nothing to do."
    )
