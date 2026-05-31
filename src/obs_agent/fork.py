"""Fork runner - manages forked Claude sessions for subtasks.

Generic mechanism for forking sessions with preset methods for:
- run: execute an arbitrary prompt in a forked session
- extract_memory: persist session learnings to vault

See decision D018 (forking as core primitive) and SDK research.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

from obs_agent.metrics import log_result
from obs_agent import tracing

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


@tracing.traced_op
async def _fork_run(
    session_id: str,
    prompt: str,
    model: str,
    *,
    system_prompt: str | None = None,
    max_turns: int | None = None,
) -> str:
    """Execute a prompt in a forked session. Args are serializable for Weave tracing."""
    options = ClaudeAgentOptions(resume=session_id, fork_session=True)
    if system_prompt is not None:
        options.system_prompt = system_prompt
    if max_turns is not None:
        options.max_turns = max_turns

    result_parts: list[str] = []
    last_message = None
    async for message in query(prompt=prompt, options=options):
        last_message = message
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_parts.append(block.text)

    if last_message is not None:
        log_result(last_message, label="fork")

    return "\n".join(result_parts)


class ForkRunner:
    """Runs subtasks in forked Claude sessions."""

    def __init__(self, *, config: OBSConfig, session_id: str) -> None:
        self.config = config
        self.session_id = session_id

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_turns: int | None = None,
    ) -> str:
        """Fork the current session and run a task prompt.

        Returns the text content of the forked session's response.
        """
        return await _fork_run(
            self.session_id,
            prompt,
            self.config.model,
            system_prompt=system_prompt,
            max_turns=max_turns,
        )

    async def extract_memory(self) -> str:
        """Extract and persist session learnings to the vault.

        Follows the session-offboard procedure: reviews conversation,
        identifies decisions/information/actions/threads, writes daily
        memory log to .claude/memory/YYYY-MM-DD.md, distributes updates
        to CLAUDE.md and topic files, creates session reference card,
        and commits via git.
        """
        prompt = (
            "Perform session offboard: extract and persist memories from this session.\n\n"
            "Follow these steps:\n"
            "1. Review the conversation and identify: decisions made, information learned, "
            "actions taken, open threads\n"
            "2. Write a daily memory log to .claude/memory/YYYY-MM-DD.md (today's date)\n"
            "3. Update CLAUDE.md with any changes to current focus, active threads, "
            "or recent decisions\n"
            "4. Update or create topic files in .claude/topics/ if any section in CLAUDE.md "
            "has grown too long\n"
            "5. Commit changes via git with a descriptive message\n\n"
            "If there is nothing meaningful to persist, state that and skip."
        )

        return await self.run(prompt, max_turns=10)
