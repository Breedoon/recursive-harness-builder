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


@pytest.fixture
def e2e_vault(tmp_path: Path) -> Path:
    """Create a rich fixture vault for E2E tests.

    More complete than the minimal unit test vault - includes
    skills, meeting notes (immutable), and realistic content.
    """
    env_path = os.environ.get("OBS_TEST_VAULT")
    if env_path:
        return Path(env_path)

    vault = tmp_path / "vault"
    agent = vault / "Agent"

    # Core directories
    (agent / "system" / "sessions").mkdir(parents=True)
    (agent / "skills").mkdir(parents=True)
    (agent / "memory").mkdir(parents=True)
    (agent / "topics").mkdir(parents=True)
    (agent / "drafts").mkdir(parents=True)

    # Misc/Meeting Notes (immutable test data)
    meeting_notes = vault / "Misc" / "Meeting Notes"
    meeting_notes.mkdir(parents=True)
    (vault / "Misc" / "Meeting Notes.md").write_text(
        "# Meeting Notes\n\nParent note for transcripts.\n"
    )
    (meeting_notes / "2025-01-15 standup.md").write_text(
        "# 2025-01-15 Standup\n\nDiscussed project timeline.\n"
    )
    (meeting_notes / "2025-02-04 planning.md").write_text(
        "# 2025-02-04 Planning\n\nQuarterly goals review.\n"
    )

    # Vault knowledge dirs
    (vault / "Vault" / "CS").mkdir(parents=True)
    (vault / "Vault" / "CS" / "algorithms.md").write_text(
        "# Algorithms\n\nNotes on algorithms.\n"
    )

    # Agent context
    (agent / "context.md").write_text(
        "# Agent Context\n\n"
        "## Current Focus\n"
        "Setting up the OBS Agent system.\n\n"
        "## Active Threads\n"
        "- Building the agent MVP\n"
        "- Testing vault operations\n\n"
        "## Recent Decisions\n"
        "- Using fork-based architecture (D018)\n"
    )

    # Parent notes
    (agent / "memory.md").write_text("# Memory\n\nParent note for daily memory logs.\n")
    (agent / "skills.md").write_text(
        "# Skills\n\n"
        "## Core Skills\n"
        "- file-conventions\n"
        "- update-context\n"
        "- manage-summaries\n"
        "- create-reference\n\n"
        "## Operational Skills\n"
        "- session-offboard\n"
        "- vault-search\n"
        "- git-commit\n"
    )
    (agent / "system.md").write_text("# System\n\nParent note for system docs.\n")

    # Core skill files
    for skill_name, desc in [
        ("file-conventions", "Master reference for vault file operations"),
        ("update-context", "Persist learnings to context.md and topics"),
        ("manage-summaries", "Lazy-append one-line summaries to parent notes"),
        ("create-reference", "Create reference cards for external content"),
    ]:
        skill_dir = agent / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n"
            f"description: {desc}\n"
            f"metadata:\n  priority: core\n  triggers: always\n"
            f"---\n# {skill_name}\n\n{desc}.\n"
        )

    # Operational skill files
    for skill_name, desc in [
        ("session-offboard", "End-of-session context persistence"),
        ("vault-search", "Search strategies for finding vault content"),
        ("git-commit", "When and how to make meaningful vault commits"),
        ("daily-planning", "Planning with journal hierarchy"),
        ("process-meeting", "Meeting transcript handling"),
        ("ingest-content", "Process external content"),
        ("split-document", "Split growing files into directories"),
        ("proactive-behavior", "Connect dots and anticipate needs"),
    ]:
        skill_dir = agent / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n"
            f"description: {desc}\n"
            f"metadata:\n  priority: operational\n  triggers: on demand\n"
            f"---\n# {skill_name}\n\n{desc}.\n"
        )

    return vault


@pytest.fixture
def e2e_config(e2e_vault: Path) -> OBSConfig:
    """Config pointing at the E2E fixture vault."""
    return OBSConfig(vault_path=e2e_vault)
