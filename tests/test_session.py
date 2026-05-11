"""Tests for obs_agent.session SessionManager behavior."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError

from obs_agent.hooks import HookPipeline, HookState
from obs_agent.prompt import build_obs_platform_appendix
from obs_agent.session import SessionManager


@pytest.fixture(autouse=True)
def _mock_obs_tools():
    """Patch create_obs_tools by default to avoid MCP server construction."""
    with patch("obs_agent.session.create_obs_tools", return_value=MagicMock()):
        yield


class TestSessionManagerInit:
    def test_starts_empty(self, config):
        mgr = SessionManager(config=config)
        assert mgr.config is config
        assert mgr.session_id is None
        assert mgr.last_activity is None
        assert mgr._client is None
        assert mgr._connected is False


class TestSessionTracking:
    def test_set_session_id_tracks_activity(self, config):
        mgr = SessionManager(config=config)
        before = time.time()
        mgr.set_session_id("sess-1")
        after = time.time()
        assert mgr.session_id == "sess-1"
        assert mgr.last_activity is not None
        assert before <= mgr.last_activity <= after

    def test_touch_updates_activity(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        first = mgr.last_activity
        time.sleep(0.01)
        mgr.touch()
        assert mgr.last_activity > first


class TestResumeWindow:
    def test_should_resume_within_window(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        assert mgr.should_resume() is True

    def test_should_not_resume_without_session(self, config):
        mgr = SessionManager(config=config)
        assert mgr.should_resume() is False

    def test_should_not_resume_without_activity(self, config):
        mgr = SessionManager(config=config)
        mgr._session_id = "sess-1"
        mgr.last_activity = None
        assert mgr.should_resume() is False

    def test_should_not_resume_at_or_after_boundary(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        mgr.last_activity = time.time() - config.cache_window_seconds
        assert mgr.should_resume() is False


class TestCreateOptions:
    def test_appends_obs_platform_instructions_to_system_prompt(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        assert options.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": build_obs_platform_appendix(),
        }

    def test_includes_hooks(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        assert options.hooks is not None
        assert "PreToolUse" in options.hooks
        assert "PostToolUse" in options.hooks

    def test_pre_tool_use_pipeline_shape(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        pre = options.hooks["PreToolUse"]
        assert len(pre) == 1
        assert len(pre[0].hooks) == 1
        assert isinstance(pre[0].hooks[0], HookPipeline)

    def test_resume_option_set_with_recent_session(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        options = mgr.create_options()
        assert options.resume == "sess-1"

    def test_resume_option_unset_after_timeout(self, config):
        config.cache_window_seconds = 60
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-old")
        mgr.last_activity = time.time() - 3600
        options = mgr.create_options()
        assert options.resume is None

    def test_sets_cwd_and_project_setting_source(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        assert options.cwd == str(config.vault_path)
        assert options.setting_sources == ["project"]

    def test_sets_obs_agent_mcp_server(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        assert options.mcp_servers is not None
        assert "obs-agent" in options.mcp_servers

    def test_uses_configured_model(self, config):
        config.model = "haiku"
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        assert options.model == "haiku"

    def test_exposes_effective_model_to_hook_state(self, config):
        state = HookState()
        mgr = SessionManager(config=config, hook_state=state)
        mgr.model_override = "gpt-5.4-mini[200k]"
        mgr.create_options()
        assert state.effective_model == "gpt-5.4-mini[200k]"

    def test_passes_hook_state_to_obs_tools(self, config):
        state = HookState()
        mgr = SessionManager(config=config, hook_state=state)
        with patch("obs_agent.session.create_obs_tools", return_value=MagicMock()) as mock_create:
            mgr.create_options()
        assert mock_create.call_count == 1
        assert mock_create.call_args.kwargs.get("hook_state") is state

    def test_includes_session_sdk_env_overrides(self, config):
        mgr = SessionManager(config=config)
        mgr.set_sdk_env_overrides(
            {
                "CLAUDE_CODE_ENABLE_TASKS": "1",
                "CLAUDE_CODE_TASK_LIST_ID": "team-alpha",
            }
        )
        options = mgr.create_options()
        assert options.env["CLAUDE_CODE_ENABLE_TASKS"] == "1"
        assert options.env["CLAUDE_CODE_TASK_LIST_ID"] == "team-alpha"

    def test_exposes_sdk_env_overrides_to_hook_state(self, config):
        state = HookState()
        mgr = SessionManager(config=config, hook_state=state)
        mgr.set_sdk_env_overrides({"CLAUDE_CODE_TEAM_NAME": "team-alpha"})
        assert state.sdk_env_overrides == {"CLAUDE_CODE_TEAM_NAME": "team-alpha"}
        assert state.vault_path == config.vault_path

        mgr.create_options()
        assert state.sdk_env_overrides == {"CLAUDE_CODE_TEAM_NAME": "team-alpha"}
        assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in state.sdk_env_overrides

    def test_includes_default_sdk_hardening_env(self, config):
        mgr = SessionManager(config=config)
        options = mgr.create_options()
        # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC was removed on 2026-04-04
        # (breaks GrowthBook / 1h cache TTL). Verify it is NOT set.
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in options.env

    def test_model_override_sets_context_window_envs(self, config):
        mgr = SessionManager(config=config)
        mgr.model_override = "gpt-5.4-mini"
        options = mgr.create_options()
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
        assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
        assert options.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "92"
        assert options.env["ANTHROPIC_API_KEY"] == config.cli_proxy_api_key

    def test_set_sdk_env_overrides_filters_empty_values(self, config):
        mgr = SessionManager(config=config)
        mgr.set_sdk_env_overrides({"A": "1", "B": "", "": "x"})
        assert mgr.sdk_env_overrides == {"A": "1"}


class TestResetBehavior:
    def test_reset_clears_session_and_client_refs(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        mgr._client = MagicMock()
        mgr._connected = True
        mgr.reset()
        assert mgr.session_id is None
        assert mgr.last_activity is None
        assert mgr._client is None
        assert mgr._connected is False

    @pytest.mark.asyncio
    async def test_async_reset_clears_session(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-1")
        await mgr.async_reset()
        assert mgr.session_id is None
        assert mgr.last_activity is None


@pytest.mark.real_get_client
class TestClientLifecycle:
    @pytest.mark.asyncio
    async def test_get_client_creates_and_connects(self, config):
        mgr = SessionManager(config=config)
        mock_client = AsyncMock()
        with patch("obs_agent.session.ClaudeSDKClient", return_value=mock_client):
            client = await mgr.get_client()
        assert client is mock_client
        assert mgr._connected is True
        mock_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_client_reuses_connected_client_within_window(self, config):
        mgr = SessionManager(config=config)
        mock_client = AsyncMock()
        with patch("obs_agent.session.ClaudeSDKClient", return_value=mock_client):
            first = await mgr.get_client()
            second = await mgr.get_client()
        assert first is second
        assert mock_client.connect.call_count == 1

    @pytest.mark.asyncio
    async def test_get_client_recreates_after_cache_expiry(self, config):
        config.cache_window_seconds = 60
        mgr = SessionManager(config=config)
        mock_client1 = AsyncMock()
        mock_client2 = AsyncMock()
        created = 0

        def _make_client(*args, **kwargs):
            nonlocal created
            created += 1
            return mock_client1 if created == 1 else mock_client2

        with patch("obs_agent.session.ClaudeSDKClient", side_effect=_make_client):
            first = await mgr.get_client()
            mgr.set_session_id("sess-old")
            mgr.last_activity = time.time() - 3600
            second = await mgr.get_client()

        assert first is not second
        mock_client1.disconnect.assert_called_once()
        mock_client2.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_client_retries_connect_failures(self, config):
        mgr = SessionManager(config=config)
        failing_client = AsyncMock()
        failing_client.connect.side_effect = ProcessError("connect failed", exit_code=1)
        succeeding_client = AsyncMock()

        with (
            patch(
                "obs_agent.session.ClaudeSDKClient",
                side_effect=[failing_client, succeeding_client],
            ),
            patch("obs_agent.session.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            client = await mgr.get_client()

        assert client is succeeding_client
        failing_client.connect.assert_called_once()
        failing_client.disconnect.assert_called_once()
        succeeding_client.connect.assert_called_once()
        mock_sleep.assert_awaited_once()
        assert mgr._connected is True

    @pytest.mark.asyncio
    async def test_get_client_raises_after_exhausting_connect_retries(self, config):
        mgr = SessionManager(config=config)
        clients = [AsyncMock() for _ in range(3)]
        for client in clients:
            client.connect.side_effect = ProcessError("connect failed", exit_code=1)

        with (
            patch("obs_agent.session.ClaudeSDKClient", side_effect=clients),
            patch("obs_agent.session.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(ProcessError, match="connect failed"):
                await mgr.get_client()

        assert mock_sleep.await_count == 2
        for client in clients:
            client.disconnect.assert_called_once()
        assert mgr._client is None
        assert mgr._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self, config):
        mgr = SessionManager(config=config)
        mock_client = AsyncMock()
        with patch("obs_agent.session.ClaudeSDKClient", return_value=mock_client):
            await mgr.get_client()
        await mgr.disconnect()
        assert mgr._client is None
        assert mgr._connected is False
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_swallows_client_errors(self, config):
        mgr = SessionManager(config=config)
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = Exception("disconnect failed")
        with patch("obs_agent.session.ClaudeSDKClient", return_value=mock_client):
            await mgr.get_client()
        await mgr.disconnect()
        assert mgr._client is None

    @pytest.mark.asyncio
    async def test_reconnect_requires_session_id(self, config):
        mgr = SessionManager(config=config)
        with pytest.raises(RuntimeError, match="no session_id"):
            await mgr.reconnect()

    @pytest.mark.asyncio
    async def test_reconnect_preserves_session_id(self, config):
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-42")
        mock_client = AsyncMock()
        with patch("obs_agent.session.ClaudeSDKClient", return_value=mock_client):
            client = await mgr.reconnect()
        assert client is mock_client
        assert mgr.session_id == "sess-42"
        assert mgr._connected is True
        mock_client.connect.assert_called_once()
