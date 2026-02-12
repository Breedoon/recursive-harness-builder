"""Tests for obs_agent.hooks."""

from obs_agent.hooks import on_pre_tool_use, on_post_tool_use, on_stop, on_pre_compact


def test_hooks_importable():
    """All hook functions are importable and callable."""
    assert callable(on_pre_tool_use)
    assert callable(on_post_tool_use)
    assert callable(on_stop)
    assert callable(on_pre_compact)
