"""SSE status events for OBS Agent.

Lightweight event system for streaming status updates (tool use, thinking,
queue delivery, skill classification) to clients via the SSE stream.

Status events use the standard SSE `event:` field:
    event: status
    data: {"type":"tool_use","summary":"Read: Agent/context.md"}

Clients that don't understand `event: status` silently ignore them per the
SSE spec (backward-compatible).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from obs_agent.config import _DEFAULT_VAULT

_SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class StatusEvent:
    """A status event to be sent over the SSE stream."""

    type: str
    summary: str
    count: int | None = None
    messages: list[str] | None = None

    def to_sse(self) -> str:
        """Serialize to SSE wire format.

        Returns a string like:
            event: status\\ndata: {"type":"tool_use","summary":"Read: foo"}\\n\\n
        """
        payload: dict = {"type": self.type, "summary": self.summary}
        if self.count is not None:
            payload["count"] = self.count
        if self.messages is not None:
            payload["messages"] = self.messages
        return f"event: status\ndata: {json.dumps(payload)}\n\n"


def _shorten_path(path: str) -> str:
    """Strip the vault prefix from a path, returning a vault-relative path.

    Example:
        /Users/.../iCloud~md~obsidian/Documents/T/Agent/context.md
        → Agent/context.md
    """
    vault_prefix = str(_DEFAULT_VAULT)
    if path.startswith(vault_prefix):
        relative = path[len(vault_prefix):]
        return relative.lstrip("/")
    return path


def _truncate(text: str, *, limit: int = _SUMMARY_LIMIT) -> str:
    """Truncate tool summaries to a consistent max length."""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def summarize_tool_use(tool_name: str, tool_input: dict) -> str:
    """Create a structured summary of a tool use.

    Returns structured descriptions like:
        "Read: Agent/context.md"
        "Bash: ls -la Agent/"
        "Grep: pattern='skills' path=Agent/"
        "Glob: '**/*.md' in Agent/"
        "SomeTool: arg1=val1 arg2=val2"
    """
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        short = _shorten_path(file_path)
        return _truncate(f"Read: {short}") if short else "Read: file"

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        short_path = _shorten_path(path) if path else ""
        parts = []
        if pattern:
            parts.append(f"pattern='{pattern}'")
        if short_path:
            parts.append(f"path={short_path}")
        return _truncate(f"Grep: {' '.join(parts)}") if parts else "Grep"

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        short_path = _shorten_path(path) if path else ""
        if pattern and short_path:
            return _truncate(f"Glob: '{pattern}' in {short_path}")
        if pattern:
            return _truncate(f"Glob: '{pattern}'")
        return "Glob"

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            return _truncate(f"Bash: {command}")
        return "Bash"

    if tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return _truncate(f"WebSearch: '{query}'") if query else "WebSearch"

    if tool_name == "self_fork":
        task = tool_input.get("task", "")
        background = tool_input.get("background", False)
        prefix = "Fork (bg)" if background else "Fork"
        if task:
            return _truncate(f"{prefix}: {task}")
        return prefix

    if tool_name == "Task":
        prompt = tool_input.get("prompt", "")
        if prompt:
            return _truncate(f"Task: {prompt}")
        return "Task"

    # Unknown tool: dump structured input for observability.
    if tool_input:
        payload = {"tool": tool_name, "input": tool_input}
    else:
        payload = {"tool": tool_name}
    return _truncate(json.dumps(payload, ensure_ascii=True, default=str))
