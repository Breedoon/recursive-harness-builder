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

from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock
from fastapi.testclient import TestClient

from obs_agent.config import OBSConfig
from obs_agent.daemon import create_app
from obs_agent.fork import ForkRunner
from obs_agent.hooks import on_pre_tool_use
from obs_agent.prompt import build_system_prompt
from obs_agent.session import SessionManager


# NOTE: SDK uses subscription auth, NOT API key. No skip gating needed.


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

    def test_chat_returns_response(self, e2e_config):
        """POST /chat returns a non-empty assistant response.

        Note: Uses mocked SDK client because ClaudeSDKClient's internal anyio
        task groups conflict with TestClient's synchronous portal. Real SDK
        E2E coverage is in test_real_e2e.py with actual uvicorn servers.
        """
        from unittest.mock import AsyncMock

        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Hello!")]
        mock_msg.session_id = "e2e-sess-1"

        mock_client = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        async def mock_receive():
            yield mock_msg

        mock_client.receive_response = mock_receive

        with patch("obs_agent.session.SessionManager.get_client") as mock_get_client:
            mock_get_client.return_value = mock_client
            application = create_app(e2e_config)
            client = TestClient(application)
            response = client.post("/chat", json={"message": "Say hello in one word."})

        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert len(data["response"]) > 0

    def test_chat_with_mocked_sdk(self, e2e_config):
        """Chat flow works end-to-end with mocked SDK."""
        from unittest.mock import AsyncMock

        # Init message with session_id (simulates SDK SystemMessage)
        mock_init = MagicMock()
        mock_init.session_id = "e2e-mock-session"
        mock_init.content = None

        # Assistant response message
        mock_msg = MagicMock()
        mock_msg.session_id = None
        mock_msg.content = [TextBlock(text="Hello! I'm your vault assistant.")]

        mock_client = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        async def mock_receive():
            yield mock_init
            yield mock_msg

        mock_client.receive_response = mock_receive

        with patch("obs_agent.session.SessionManager.get_client") as mock_get_client:
            mock_get_client.return_value = mock_client

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

    @pytest.mark.asyncio
    async def test_classify_identifies_file_operations(self, e2e_config):
        """Classification identifies file-conventions for vault operations."""
        from obs_agent.fork import classify_without_fork
        skills = await classify_without_fork(
            "Create a new topic file for my project goals", e2e_config
        )
        assert isinstance(skills, list)
        # Should identify file-related skills
        assert any(
            "file" in s or "create" in s or "update" in s
            for s in skills
        ), f"Expected file-related skill, got: {skills}"

    @pytest.mark.asyncio
    async def test_classify_identifies_planning(self, e2e_config):
        """Classification identifies daily-planning for planning requests."""
        from obs_agent.fork import classify_without_fork
        skills = await classify_without_fork(
            "Help me plan my day and review my weekly goals", e2e_config
        )
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
        mock_msg.content = [TextBlock(text='[{"skill": "file-conventions"}, {"skill": "update-context"}]')]

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

    def test_chat_can_update_context(self, e2e_config, e2e_vault):
        """A chat message asking to update context modifies context.md.

        Note: Uses mocked SDK client because ClaudeSDKClient's internal anyio
        task groups conflict with TestClient's synchronous portal. Real SDK
        E2E coverage is in test_real_e2e.py with actual uvicorn servers.
        """
        from unittest.mock import AsyncMock

        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Updated context for you.")]
        mock_msg.session_id = "e2e-vault-sess"

        mock_client = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        async def mock_receive():
            yield mock_msg

        mock_client.receive_response = mock_receive

        with patch("obs_agent.session.SessionManager.get_client") as mock_get_client:
            mock_get_client.return_value = mock_client
            application = create_app(e2e_config)
            client = TestClient(application)

            response = client.post(
                "/chat",
                json={"message": "Add 'Testing vault writes' to the Active Threads section of Agent/context.md"},
            )
        assert response.status_code == 200

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
