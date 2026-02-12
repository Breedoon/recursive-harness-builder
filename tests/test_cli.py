"""Tests for obs_agent.cli."""

from obs_agent.cli import main


def test_main_exists():
    """main function exists and is callable."""
    assert callable(main)
