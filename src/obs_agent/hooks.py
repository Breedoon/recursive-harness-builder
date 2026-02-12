"""SDK hooks for OBS Agent.

- PreToolUse: guards immutable files and .env from writes
- Stop: triggers memory extraction via fork
- PreCompact: triggers extraction then denies compaction (D022)
- UserPromptSubmit: classifies skills and injects SKILL.md content

See decisions D018, D019, D022.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig
    from obs_agent.fork import ForkRunner


# Write-mutating tool names that the guard should check
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Read-only tools that are always allowed
_READ_TOOLS = {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"}

# File patterns that are always blocked from writes (beyond config immutable_patterns)
_BLOCKED_FILE_PATTERNS = [".env"]


def _deny(reason: str) -> dict:
    """Return a deny hook response with reason."""
    return {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "reason": reason,
        }
    }


def on_pre_tool_use(
    tool_name: str,
    tool_input: dict,
    *,
    config: OBSConfig,
) -> dict | None:
    """Guard hook: block writes to immutable files and .env.

    Returns None to allow, or a deny dict to block.
    """
    # Only guard write-mutating tools
    if tool_name not in _WRITE_TOOLS:
        return None

    file_path_str = tool_input.get("file_path", "")
    if not file_path_str:
        return None

    file_path = Path(file_path_str)

    # Check .env files
    for pattern in _BLOCKED_FILE_PATTERNS:
        if pattern in file_path.name:
            return _deny(f"Blocked: cannot modify {pattern} files")

    # Check immutable patterns from config
    if config.is_immutable(file_path):
        return _deny(f"Blocked: {file_path} matches an immutable pattern. These files must not be edited.")

    return None


async def on_stop(
    *,
    config: OBSConfig,
    fork_runner: ForkRunner,
) -> None:
    """Stop hook: trigger memory extraction fork."""
    await fork_runner.extract_memory()


async def on_pre_compact(
    *,
    config: OBSConfig,
    fork_runner: ForkRunner,
) -> dict:
    """PreCompact hook: extract memories then deny compaction.

    Per D022: no lossy compaction. Flush memories to vault, then deny
    so the daemon can restart with a fresh session.
    """
    await fork_runner.extract_memory()
    return _deny("Compaction denied: memories flushed, restart with fresh session")


async def on_user_prompt_submit(
    user_message: str,
    *,
    config: OBSConfig,
    fork_runner: ForkRunner,
) -> str | None:
    """UserPromptSubmit hook: classify skills and inject SKILL.md content.

    Per D019: fork to classify, then read SKILL.md files and return
    content for injection into the session.
    """
    # Fork to classify what skills are needed
    skill_names = await fork_runner.classify(user_message)

    if not skill_names:
        return None

    # Read the skill files and assemble content for injection
    from obs_agent.prompt import _read_file

    parts: list[str] = []
    for name in skill_names:
        skill_path = config.skill_path(name)
        content = _read_file(skill_path)
        if content:
            parts.append(f"## Skill: {name}\n\n{content}")

    return "\n\n---\n\n".join(parts) if parts else None
