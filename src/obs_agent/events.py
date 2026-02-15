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
        return f"Read: {short}" if short else "Read: file"

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        short_path = _shorten_path(path) if path else ""
        parts = []
        if pattern:
            parts.append(f"pattern='{pattern}'")
        if short_path:
            parts.append(f"path={short_path}")
        return f"Grep: {' '.join(parts)}" if parts else "Grep"

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        short_path = _shorten_path(path) if path else ""
        if pattern and short_path:
            return f"Glob: '{pattern}' in {short_path}"
        if pattern:
            return f"Glob: '{pattern}'"
        return "Glob"

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            truncated = command[:80]
            if len(command) > 80:
                truncated += "..."
            return f"Bash: {truncated}"
        return "Bash"

    if tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"WebSearch: '{query}'" if query else "WebSearch"

    # Unknown tool: dump first 3 args
    if tool_input:
        items = list(tool_input.items())[:3]
        args_str = " ".join(f"{k}={v}" for k, v in items)
        truncated = args_str[:80]
        if len(args_str) > 80:
            truncated += "..."
        return f"{tool_name}: {truncated}"
    return tool_name
