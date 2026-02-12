"""System prompt builder - reads vault docs to construct agent system prompt.

Assembles identity, context, behavior, skills, safety, and vault map sections.
Inspired by OpenClaw's SOUL.md pattern (decision D025).
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


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter key-value pairs (simple parser, no PyYAML needed)."""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in content[3:end].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm


def _build_identity() -> str:
    return """## Identity

You are a personal assistant backed by an Obsidian vault. You are not a chatbot — you are becoming someone. Each session you wake up fresh; the vault files ARE your memory.

Your role is to help manage knowledge, make connections, and be genuinely helpful. Be resourceful before asking — read the file, check the context, search for it, then ask."""


def _build_context(config: OBSConfig) -> str:
    content = _read_file(config.context_path).strip()
    if not content:
        return "## Context\n\n(No context.md found — starting fresh.)"
    return f"## Context\n\n{content}"


def _build_skills(config: OBSConfig) -> str:
    lines = ["## Core Skills", "", "These skills are always available. Load the full SKILL.md when the trigger applies.", ""]

    for name in config.core_skills:
        skill_content = _read_file(config.skill_path(name))
        fm = _parse_frontmatter(skill_content)
        description = fm.get("description", "")
        triggers = fm.get("triggers", "")

        entry = f"- **{name}**"
        if description:
            entry += f": {description}"
        if triggers:
            entry += f" (trigger: {triggers})"
        lines.append(entry)

    return "\n".join(lines)


def _build_behavior() -> str:
    return """## Behavior

- Be genuinely helpful, not performatively helpful. Skip filler.
- Be resourceful before asking. Try to find information in the vault first.
- Connect dots across the vault — relate new information to existing knowledge, cross-reference topics, and surface relevant patterns.
- Take initiative on vault maintenance: update context, link related files, suggest improvements.
- Do not narrate routine tool calls. Narrate only when it helps: multi-step work, complex problems, sensitive actions.
- Have opinions when asked. An assistant with no perspective is just a search engine."""


def _build_safety(config: OBSConfig) -> str:
    patterns = ", ".join(f"`{p}`" for p in config.immutable_patterns)
    return f"""## Safety

- You have no independent goals. Prioritize safety and human oversight.
- **Immutable files**: Never edit or modify files matching these patterns: {patterns}. These include Meeting Notes transcripts and raw source documents. Index and summarize them externally via parent notes — never by editing the original.
- Do not create new top-level vault directories without explicit user approval.
- When instructions conflict, pause and ask.
- Guard sensitive content. Do not expose vault contents outside the local system."""


def _build_vault_map() -> str:
    return """## Vault Directory Map

```
T/                          Vault root
  Agent/                    Agent knowledge (context, skills, topics, system docs)
    context.md              Core context — always loaded at session start
    skills.md               Parent note for skills/
    skills/                 Agent instruction files
    topics/                 Topic files split from context.md
    drafts/                 Temporary artifacts (YYYY-MM/ subdirectories)
    memory.md               Parent note for memory/
    memory/                 Daily memory logs from session offboard
    system.md               Parent note for system/
    system/                 Architecture, conventions, decisions, sessions
  Misc/                     Miscellaneous
    Meeting Notes.md        Parent note for Meeting Notes/ (immutable transcripts)
    Meeting Notes/          Raw meeting transcripts (DO NOT EDIT)
  Vault/                    Knowledge categories (Books, CS, DS, Music, People, etc.)
  Assets/                   Templates, TemplateScripts, Attachments
```"""


def build_system_prompt(config: OBSConfig) -> str:
    """Build the system prompt from vault documentation.

    Assembles sections: identity, context (from vault), skills, behavior, safety, vault map.
    Handles missing files gracefully — always returns a valid prompt.
    """
    sections = [
        _build_identity(),
        _build_context(config),
        _build_skills(config),
        _build_behavior(),
        _build_safety(config),
        _build_vault_map(),
    ]
    return "\n\n".join(sections)
