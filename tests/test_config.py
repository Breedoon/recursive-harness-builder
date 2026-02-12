"""Tests for obs_agent.config."""

from obs_agent.config import OBSConfig


def test_config_exists():
    """OBSConfig can be instantiated."""
    cfg = OBSConfig()
    assert cfg is not None
