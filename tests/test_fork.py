"""Tests for obs_agent.fork."""

from obs_agent.fork import ForkRunner


def test_fork_runner_exists():
    """ForkRunner can be instantiated."""
    runner = ForkRunner()
    assert runner is not None
