"""MCP tools for OBS Agent.

Provides in-process MCP tools that the Claude Agent SDK exposes to the model.
Currently: find_skills - on-demand skill classification and loading.

See decisions D018 (forking) and D019 (skill classification).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from claude_agent_sdk import tool, create_sdk_mcp_server

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


def create_obs_tools(config: OBSConfig, get_session_id: Callable[[], str | None]):
    """Create the OBS Agent MCP tool server.

    get_session_id is a callable that returns the current session_id
    (closure over daemon state).
    """

    @tool(
        "find_skills",
        "Search for vault skills relevant to a complex query. Use when you need "
        "specific instructions for vault operations like searching, editing, or "
        "reasoning over different content types.",
        {"query": str},
    )
    async def find_skills(args: dict) -> dict:
        query_text = args["query"]
        session_id = get_session_id()

        if session_id:
            from obs_agent.fork import ForkRunner

            runner = ForkRunner(config=config, session_id=session_id)
            skill_names = await runner.classify(query_text)
        else:
            from obs_agent.fork import classify_without_fork

            skill_names = await classify_without_fork(query_text, config)

        if not skill_names:
            return {
                "content": [
                    {"type": "text", "text": "No specific skills needed for this query."}
                ]
            }

        from obs_agent.prompt import _read_file

        parts: list[str] = []
        for name in skill_names:
            content = _read_file(config.skill_path(name))
            if content:
                parts.append(f"## Skill: {name}\n\n{content}")

        result = (
            "\n\n---\n\n".join(parts)
            if parts
            else "Skills identified but files not found."
        )
        return {"content": [{"type": "text", "text": result}]}

    server = create_sdk_mcp_server("obs-agent", tools=[find_skills])
    return server
