"""Tests for obs_agent.prompt."""

from obs_agent.prompt import build_system_prompt


def test_build_system_prompt_importable():
    """build_system_prompt function exists and is callable."""
    assert callable(build_system_prompt)
