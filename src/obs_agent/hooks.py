"""SDK hooks for OBS Agent - offboard, guard, compact."""


def on_pre_tool_use(tool_name: str, tool_input: dict) -> dict | None:
    """Guard hook: validate tool usage before execution."""
    pass


def on_post_tool_use(tool_name: str, tool_input: dict, tool_output: str) -> None:
    """Post-tool hook: track operations for offboard."""
    pass


def on_stop(response: str) -> None:
    """Stop hook: trigger session offboard."""
    pass


def on_pre_compact(messages: list) -> list:
    """Pre-compact hook: extract memories before context compression."""
    pass
