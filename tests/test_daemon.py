"""Tests for obs_agent.daemon."""

from obs_agent.daemon import app


def test_app_exists():
    """FastAPI app exists."""
    assert app is not None
    assert app.title == "OBS Agent"
