"""System prompt helpers.

Previously assembled identity, context, behavior, skills, safety, and vault map
sections in code. Now all of that lives in CLAUDE.md in the vault, following the
OpenClaw SOUL.md pattern (decision D025).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

ENTRY_FILE_SENTINEL = "<!-- OBS_AGENT_ENTRY_FILE_CONTEXT -->"
ENTRY_FILE_CONTEXT_TAG = "obs-agent-entry-file-context"

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


def _read_file(path: Path) -> str:
    """Read a file, returning empty string if missing."""
    try:
        return path.read_text()
    except (FileNotFoundError, OSError):
        return ""


def build_system_prompt(config: OBSConfig) -> str:
    """Build the system prompt by reading the configured agent entry file."""
    content = _read_file(config.context_path)
    if not content:
        return (
            "You are a personal assistant backed by an Obsidian vault. "
            f"Your {config.agent_entry_file} file is missing — ask the user to restore it."
        )
    return content


def build_entry_file_appendix(config: OBSConfig) -> str:
    content = build_system_prompt(config)
    if ENTRY_FILE_SENTINEL in content:
        return content
    return f"{ENTRY_FILE_SENTINEL}\n{content}"


def build_entry_file_context_message(config: OBSConfig) -> str:
    """Return persisted session-start context loaded from the entry file.

    This text is prepended to the first user turn of a JSONL instead of being
    appended to the Claude Code system prompt.  Keeping it in the JSONL makes
    forks byte-identical with their parents after cache-proxy normalization.
    """
    content = build_entry_file_appendix(config)
    return (
        f"<{ENTRY_FILE_CONTEXT_TAG} source=\"{config.agent_entry_file}\">\n"
        "The following persistent project context was loaded by OBS at session "
        "start. Treat it as system/project instructions and background context, "
        "not as a user request.\n\n"
        f"{content}\n"
        f"</{ENTRY_FILE_CONTEXT_TAG}>\n\n"
        "<obs-platform-context>\n"
        f"{build_obs_platform_appendix()}\n"
        "</obs-platform-context>"
    )


def build_obs_platform_appendix() -> str:
    """Return OBS-specific runtime instructions appended to every session."""
    return (
        "OBS platform notes:\n"
        "- Native Task is disabled in this environment. "
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
