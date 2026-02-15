"""Fork runner - manages forked Claude sessions for subtasks.

Generic mechanism for forking sessions with preset methods for:
- classify: determine which skills a user message needs
- search: structured vault search with excerpts and temporal context
- extract_memory: persist session learnings to vault

See decision D018 (forking as core primitive) and SDK research.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

from obs_agent.metrics import log_result

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


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
        options = ClaudeAgentOptions(
            resume=self.session_id,
            fork_session=True,
        )
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

    async def classify(self, user_message: str) -> list[str]:
        """Classify what skills a user message requires.

        Forks the session to ask the LLM which skills apply. Returns a list
        of skill name strings (may be empty for simple queries).
        """
        # Build the skill manifest for the classify prompt
        manifest = ForkRunner.build_skill_manifest(self.config)

        prompt = (
            f"Classify this user message and determine which vault skills are needed.\n\n"
            f"## Available Skills\n{manifest}\n\n"
            f"## User Message\n{user_message}\n\n"
            f"Respond with ONLY a JSON array of skill objects, e.g. "
            f'[{{"skill": "file-conventions"}}, {{"skill": "update-context"}}]. '
            f"If no skills are needed, respond with []."
        )

        response = await self.run(prompt, max_turns=1)

        return self._parse_skill_list(response)

    async def search(self, query_text: str) -> dict:
        """Search the vault and return structured results.

        Returns a dict with a 'results' list, each entry having at minimum
        'file', 'excerpt', and optionally 'relevance'.
        """
        prompt = (
            f"Search the vault for information related to: {query_text}\n\n"
            f"Return a JSON object with a 'results' array. Each result must have:\n"
            f'- "file": the vault-relative file path\n'
            f'- "excerpt": a relevant text excerpt\n'
            f'- "relevance": brief explanation of why this is relevant\n\n'
            f'Respond with ONLY the JSON object. Example:\n'
            f'{{"results": [{{"file": "Agent/context.md", "excerpt": "...", "relevance": "..."}}]}}'
        )

        response = await self.run(prompt, max_turns=3)

        return self._parse_search_results(response)

    async def extract_memory(self) -> str:
        """Extract and persist session learnings to the vault.

        Follows the session-offboard procedure: reviews conversation,
        identifies decisions/information/actions/threads, writes daily
        memory log to Agent/memory/YYYY-MM-DD.md, distributes updates
        to context.md and topic files, creates session reference card,
        and commits via git.
        """
        prompt = (
            "Perform session offboard: extract and persist memories from this session.\n\n"
            "Follow these steps:\n"
            "1. Review the conversation and identify: decisions made, information learned, "
            "actions taken, open threads\n"
            "2. Write a daily memory log to Agent/memory/YYYY-MM-DD.md (today's date)\n"
            "3. Update Agent/context.md with any changes to current focus, active threads, "
            "or recent decisions\n"
            "4. Update or create topic files in Agent/topics/ if any section in context.md "
            "has grown too long\n"
            "5. Commit changes via git with a descriptive message\n\n"
            "If there is nothing meaningful to persist, state that and skip."
        )

        return await self.run(prompt, max_turns=10)

    @staticmethod
    def build_skill_manifest(config: OBSConfig) -> str:
        """Build a text manifest of all available skills for classification."""
        from obs_agent.prompt import _read_file, _parse_frontmatter

        lines = []
        skills_dir = config.skills_dir
        if skills_dir.is_dir():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    content = _read_file(skill_file)
                    fm = _parse_frontmatter(content)
                    name = skill_dir.name
                    desc = fm.get("description", "")
                    triggers = fm.get("triggers", "")
                    line = f"- **{name}**"
                    if desc:
                        line += f": {desc}"
                    if triggers:
                        line += f" (trigger: {triggers})"
                    lines.append(line)

        return "\n".join(lines) if lines else "(no skills found)"

    @staticmethod
    def _parse_skill_list(response: str) -> list[str]:
        """Parse a JSON skill list from the LLM response."""
        try:
            # Try to extract JSON from the response
            text = response.strip()
            # Find the JSON array in the response
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1:
                data = json.loads(text[start:end + 1])
                return [item["skill"] for item in data if isinstance(item, dict) and "skill" in item]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return []

    @staticmethod
    def _parse_search_results(response: str) -> dict:
        """Parse structured search results from the LLM response."""
        try:
            text = response.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return {"results": []}


async def classify_without_fork(user_message: str, config: OBSConfig) -> list[str]:
    """Classify skills for first message when no session_id exists yet.

    Uses a standalone (non-forked) query since there's no session to fork from.
    """
    manifest = ForkRunner.build_skill_manifest(config)

    prompt = (
        f"Classify this user message and determine which vault skills are needed.\n\n"
        f"## Available Skills\n{manifest}\n\n"
        f"## User Message\n{user_message}\n\n"
        f"Respond with ONLY a JSON array of skill objects, e.g. "
        f'[{{"skill": "file-conventions"}}, {{"skill": "update-context"}}]. '
        f"If no skills are needed, respond with []."
    )

    options = ClaudeAgentOptions(max_turns=1)

    result_parts: list[str] = []
    last_message = None
    async for message in query(prompt=prompt, options=options):
        last_message = message
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_parts.append(block.text)

    if last_message is not None:
        log_result(last_message, label="classify")

    response = "\n".join(result_parts)
    return ForkRunner._parse_skill_list(response)
