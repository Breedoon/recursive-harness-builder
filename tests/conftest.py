"""Shared test fixtures for OBS Agent."""

import os
import tempfile
from pathlib import Path

import pytest

from obs_agent.config import OBSConfig


class AsyncIterFromList:
    """Helper to create async iterables from lists for mocking SDK query()."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


@pytest.fixture
def fixture_vault(tmp_path: Path) -> Path:
    """Path to a fixture vault for testing.

    Uses OBS_TEST_VAULT env var if set (e.g., from setup_fixture_vault.sh),
    otherwise creates a minimal temp vault structure.
    """
    env_path = os.environ.get("OBS_TEST_VAULT")
    if env_path:
        return Path(env_path)

    # Create minimal vault structure for unit tests
    vault = tmp_path / "vault"
    agent = vault / "Agent"
    (agent / "system").mkdir(parents=True)
    (agent / "skills").mkdir(parents=True)
    (agent / "memory").mkdir(parents=True)

    (agent / "context.md").write_text("# Agent Context\n\nTest context.\n")
    (agent / "memory.md").write_text("# Memory\n\nParent note for memory entries.\n")
    (agent / "skills.md").write_text("# Skills\n\nParent note for skills.\n")
    (agent / "system.md").write_text("# System\n\nParent note for system docs.\n")

    return vault


@pytest.fixture
def config(fixture_vault: Path) -> OBSConfig:
    """OBSConfig pointing at the fixture vault."""
    return OBSConfig(vault_path=fixture_vault)
