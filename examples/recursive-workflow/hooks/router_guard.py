from __future__ import annotations

BLOCKED_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}


def check(hook_input, tool_use_id=None, context=None):
    """Prevent Router agents from editing files directly."""
    tool_name = hook_input.get("tool_name") or hook_input.get("name")
    if tool_name in BLOCKED_WRITE_TOOLS:
        return {
            "continue": False,
            "message": "Router procedures must not write or edit files directly. Spawn an Executor/Loop agent for implementation work.",
        }
    return {"continue": True}
