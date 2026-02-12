"""Tests for obs_agent.session."""

from obs_agent.session import SessionManager


def test_session_manager_exists():
    """SessionManager can be instantiated."""
    mgr = SessionManager()
    assert mgr is not None
