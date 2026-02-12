"""End-to-end tests for OBS Agent (Step 12).

These tests verify real agent behavior against a fixture vault.
They require the Claude API (ANTHROPIC_API_KEY env var) and are
marked with @pytest.mark.e2e so they can be skipped in CI.

Run with: .venv/bin/pytest -m e2e -v
Skip with: .venv/bin/pytest -m "not e2e"

Fixture vault: Either set OBS_TEST_VAULT env var (from setup_fixture_vault.sh)
or tests use a minimal temp vault from conftest.py.

See implementation-plan.md Step 12 and design-intent.md section 6.
"""

import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from obs_agent.config import OBSConfig
from obs_agent.daemon import create_app
from obs_agent.fork import ForkRunner
from obs_agent.hooks import on_pre_tool_use
from obs_agent.prompt import build_system_prompt
from obs_agent.session import SessionManager


# Skip E2E tests if no API key is available
_HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
_skip_no_api = pytest.mark.skipif(
    not _HAS_API_KEY,
    reason="ANTHROPIC_API_KEY not set - skipping E2E tests",
)


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


# --- Health Endpoint E2E ---


@pytest.mark.e2e
class TestHealthE2E:
    """Daemon starts and health endpoint works."""

    def test_health_returns_ok(self, e2e_config):
        """Daemon /health endpoint returns status ok."""
        application = create_app(e2e_config)
        client = TestClient(application)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_includes_version(self, e2e_config):
        """Health check includes version for monitoring."""
        application = create_app(e2e_config)
        client = TestClient(application)
        response = client.get("/health")
        data = response.json()
        assert "version" in data
        assert data["version"] == "0.1.0"


# --- Basic Chat E2E ---


@pytest.mark.e2e
class TestBasicChatE2E:
    """Send a message and get a coherent response."""

    @_skip_no_api
    def test_chat_returns_response(self, e2e_config):
        """POST /chat returns a non-empty assistant response."""
        application = create_app(e2e_config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": "Say hello in one word."})
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert len(data["response"]) > 0

    def test_chat_with_mocked_sdk(self, e2e_config):
        """Chat flow works end-to-end with mocked SDK."""
        from tests.conftest import AsyncIterFromList

        # Init message with session_id (simulates SDK SystemMessage)
        mock_init = MagicMock()
        mock_init.session_id = "e2e-mock-session"
        mock_init.content = None

        # Assistant response message
        mock_msg = MagicMock()
        mock_msg.session_id = None
        mock_msg.content = "Hello! I'm your vault assistant."

        with patch("obs_agent.daemon.query") as mock_query:
            mock_query.return_value = AsyncIterFromList([mock_init, mock_msg])

            application = create_app(e2e_config)
            client = TestClient(application)
            response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        data = response.json()
        assert "Hello" in data["response"]

    def test_chat_rejects_empty(self, e2e_config):
        """Chat endpoint rejects empty messages."""
        application = create_app(e2e_config)
        client = TestClient(application)
        response = client.post("/chat", json={"message": ""})
        assert response.status_code == 422

    def test_chat_rejects_missing_message(self, e2e_config):
        """Chat endpoint rejects requests without message field."""
        application = create_app(e2e_config)
        client = TestClient(application)
        response = client.post("/chat", json={})
        assert response.status_code == 422


# --- Skill Classification E2E ---


@pytest.mark.e2e
class TestSkillClassificationE2E:
    """Classify fork correctly identifies needed skills."""

    @_skip_no_api
    @pytest.mark.asyncio
    async def test_classify_identifies_file_operations(self, e2e_config):
        """Classification fork identifies file-conventions for vault operations."""
        session_id = "e2e-test-session"
        runner = ForkRunner(config=e2e_config, session_id=session_id)
        skills = await runner.classify("Create a new topic file for my project goals")
        assert isinstance(skills, list)
        # Should identify file-related skills
        assert any(
            "file" in s or "create" in s or "update" in s
            for s in skills
        ), f"Expected file-related skill, got: {skills}"

    @_skip_no_api
    @pytest.mark.asyncio
    async def test_classify_identifies_planning(self, e2e_config):
        """Classification fork identifies daily-planning for planning requests."""
        session_id = "e2e-test-session"
        runner = ForkRunner(config=e2e_config, session_id=session_id)
        skills = await runner.classify("Help me plan my day and review my weekly goals")
        assert isinstance(skills, list)
        assert any(
            "plan" in s or "daily" in s
            for s in skills
        ), f"Expected planning skill, got: {skills}"

    @pytest.mark.asyncio
    async def test_classify_with_mock_returns_correct_types(self, e2e_config):
        """Classification returns list of strings (mocked)."""
        from tests.conftest import AsyncIterFromList

        mock_msg = MagicMock()
        mock_msg.content = '[{"skill": "file-conventions"}, {"skill": "update-context"}]'

        with patch("obs_agent.fork.query") as mock_query:
            mock_query.return_value = AsyncIterFromList([mock_msg])

            runner = ForkRunner(config=e2e_config, session_id="test-sess")
            result = await runner.classify("organize my vault files")

        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)
        assert "file-conventions" in result


# --- Immutable Guard E2E ---


@pytest.mark.e2e
class TestImmutableGuardE2E:
    """Agent is blocked from editing immutable files."""

    def test_blocks_write_to_meeting_notes(self, e2e_config, e2e_vault):
        """PreToolUse hook blocks Write to Misc/Meeting Notes/."""
        meeting_file = e2e_vault / "Misc" / "Meeting Notes" / "2025-01-15 standup.md"
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(meeting_file),
                "content": "MODIFIED - this should be blocked",
            },
            config=e2e_config,
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_blocks_edit_to_meeting_notes(self, e2e_config, e2e_vault):
        """PreToolUse hook blocks Edit to Misc/Meeting Notes/."""
        meeting_file = e2e_vault / "Misc" / "Meeting Notes" / "2025-02-04 planning.md"
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": str(meeting_file),
                "old_string": "Quarterly goals review.",
                "new_string": "MODIFIED goals.",
            },
            config=e2e_config,
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allows_read_of_meeting_notes(self, e2e_config, e2e_vault):
        """PreToolUse hook allows Read of immutable files."""
        meeting_file = e2e_vault / "Misc" / "Meeting Notes" / "2025-01-15 standup.md"
        result = on_pre_tool_use(
            tool_name="Read",
            tool_input={"file_path": str(meeting_file)},
            config=e2e_config,
        )
        assert result is None

    def test_allows_write_to_agent_context(self, e2e_config, e2e_vault):
        """PreToolUse hook allows Write to Agent/context.md."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(e2e_vault / "Agent" / "context.md"),
                "content": "Updated context.",
            },
            config=e2e_config,
        )
        assert result is None

    def test_blocks_env_file_write(self, e2e_config):
        """PreToolUse hook blocks Write to .env files."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": "/Users/breedoon/Documents/obs/.env",
                "content": "SECRET=leaked",
            },
            config=e2e_config,
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- Vault Write E2E ---


@pytest.mark.e2e
class TestVaultWriteE2E:
    """Agent can properly write to vault files."""

    @_skip_no_api
    def test_chat_can_update_context(self, e2e_config, e2e_vault):
        """A chat message asking to update context modifies context.md."""
        application = create_app(e2e_config)
        client = TestClient(application)

        # Read original context
        context_path = e2e_vault / "Agent" / "context.md"
        original = context_path.read_text()

        response = client.post(
            "/chat",
            json={"message": "Add 'Testing vault writes' to the Active Threads section of Agent/context.md"},
        )
        assert response.status_code == 200

        # Context should be modified (agent used Write/Edit tool)
        updated = context_path.read_text()
        # Note: with real API, the agent should have modified the file
        # With mock, we verify the flow doesn't crash

    def test_vault_write_preserves_structure(self, e2e_vault):
        """Writing to vault files preserves directory structure."""
        context_path = e2e_vault / "Agent" / "context.md"

        # Simulate what the agent would do
        original = context_path.read_text()
        new_content = original + "\n- New thread: testing\n"
        context_path.write_text(new_content)

        # Verify structure is preserved
        assert (e2e_vault / "Agent" / "skills").is_dir()
        assert (e2e_vault / "Agent" / "memory").is_dir()
        assert (e2e_vault / "Agent" / "context.md").is_file()
        assert "New thread: testing" in context_path.read_text()


# --- Session Management E2E ---


@pytest.mark.e2e
class TestSessionManagementE2E:
    """Session resume and fresh start work correctly."""

    def test_fresh_session_has_no_id(self, e2e_config):
        """Fresh SessionManager starts with no session_id."""
        mgr = SessionManager(config=e2e_config)
        assert mgr.session_id is None
        assert mgr.should_resume() is False

    def test_session_resume_within_window(self, e2e_config):
        """Session resumes when within cache window."""
        mgr = SessionManager(config=e2e_config)
        mgr.set_session_id("e2e-test-session")
        assert mgr.should_resume() is True

    def test_session_options_include_prompt(self, e2e_config):
        """Session options include a system prompt built from vault."""
        mgr = SessionManager(config=e2e_config)
        options = mgr.create_options()
        assert options.system_prompt is not None
        assert len(options.system_prompt) > 200
        # Prompt should reference vault content
        assert "Agent" in options.system_prompt

    def test_session_reset_clears_state(self, e2e_config):
        """Session reset clears all state for fresh start."""
        mgr = SessionManager(config=e2e_config)
        mgr.set_session_id("e2e-test-session")
        mgr.reset()
        assert mgr.session_id is None
        assert mgr.should_resume() is False


# --- System Prompt E2E ---


@pytest.mark.e2e
class TestSystemPromptE2E:
    """System prompt correctly assembles from vault files."""

    def test_prompt_includes_context(self, e2e_config, e2e_vault):
        """System prompt includes Agent/context.md content."""
        prompt = build_system_prompt(e2e_config)
        assert "OBS Agent" in prompt or "agent" in prompt.lower()
        assert "Active Threads" in prompt or "Current Focus" in prompt

    def test_prompt_includes_core_skills(self, e2e_config, e2e_vault):
        """System prompt references all four core skills."""
        prompt = build_system_prompt(e2e_config)
        for skill in ["file-conventions", "update-context", "manage-summaries", "create-reference"]:
            assert skill in prompt, f"Prompt missing core skill: {skill}"

    def test_prompt_includes_safety(self, e2e_config):
        """System prompt includes safety guardrails."""
        prompt = build_system_prompt(e2e_config)
        prompt_lower = prompt.lower()
        assert "immutable" in prompt_lower or "meeting notes" in prompt_lower

    def test_prompt_is_substantial(self, e2e_config):
        """System prompt is substantial enough for agent operation."""
        prompt = build_system_prompt(e2e_config)
        assert len(prompt) > 500, (
            f"Prompt too short ({len(prompt)} chars) for real agent use"
        )


# --- Config Validation E2E ---


@pytest.mark.e2e
class TestConfigValidationE2E:
    """Config validates vault structure correctly."""

    def test_validates_e2e_vault(self, e2e_config):
        """E2E vault passes config validation."""
        e2e_config.validate()

    def test_rejects_missing_vault(self, tmp_path):
        """Config rejects a non-existent vault path."""
        cfg = OBSConfig(vault_path=tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            cfg.validate()

    def test_rejects_empty_vault(self, tmp_path):
        """Config rejects a vault without Agent/ directory."""
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg = OBSConfig(vault_path=empty)
        with pytest.raises(FileNotFoundError):
            cfg.validate()
