"""Tests for obs_agent.session - Step 5 TDD (RED phase).

These tests define the SessionManager contract for:
- Session lifecycle (init, resume, fresh start)
- Cache window management (58 min resume window)
- SDK options assembly (hooks + system prompt + resume)
- Context loading on fresh sessions

See implementation-plan.md Step 5 and decisions:
- D014: SDK cache for continuity (~58 min window)
- D022: No compaction - flush and restart
- D025: context.md as orientation document
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.config import OBSConfig
from obs_agent.session import SessionManager


# --- Initialization ---


class TestSessionManagerInit:
    """SessionManager requires config to operate."""

    def test_takes_config(self, config):
        """SessionManager initializes with a config object."""
        mgr = SessionManager(config=config)
        assert mgr.config is config

    def test_starts_with_no_session(self, config):
        """Fresh SessionManager has no active session."""
        mgr = SessionManager(config=config)
        assert mgr.session_id is None

    def test_starts_with_no_timestamp(self, config):
        """Fresh SessionManager has no last-activity timestamp."""
        mgr = SessionManager(config=config)
        assert mgr.last_activity is None


# --- Session ID Tracking ---


class TestSessionTracking:
    """SessionManager tracks session_id from SDK init messages."""

    def test_tracks_session_id(self, config):
        """set_session_id stores the ID from SDK init."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        assert mgr.session_id == "sess-xyz-789"

    def test_updates_last_activity(self, config):
        """Setting session_id also records the activity timestamp."""
        mgr = SessionManager(config=config)
        before = time.time()
        mgr.set_session_id("sess-xyz-789")
        after = time.time()
        assert mgr.last_activity is not None
        assert before <= mgr.last_activity <= after

    def test_touch_updates_activity(self, config):
        """touch() updates last_activity to current time."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        first = mgr.last_activity

        time.sleep(0.01)
        mgr.touch()

        assert mgr.last_activity > first


# --- Resume Window Logic ---


class TestResumeWindow:
    """SessionManager decides resume vs fresh based on cache window."""

    def test_should_resume_within_window(self, config):
        """Returns True when last activity is within the cache window."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        # Just set it - should be well within the 58-minute window
        assert mgr.should_resume() is True

    def test_should_not_resume_after_timeout(self, config):
        """Returns False when last activity exceeds the cache window."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        # Simulate an old timestamp (1 hour ago)
        mgr.last_activity = time.time() - 3600
        assert mgr.should_resume() is False

    def test_should_not_resume_no_session(self, config):
        """Returns False when no session_id exists."""
        mgr = SessionManager(config=config)
        assert mgr.should_resume() is False

    def test_should_not_resume_no_timestamp(self, config):
        """Returns False when session_id exists but no timestamp."""
        mgr = SessionManager(config=config)
        mgr._session_id = "sess-xyz-789"  # Bypass set_session_id
        mgr.last_activity = None
        assert mgr.should_resume() is False

    def test_boundary_at_cache_window(self, config):
        """Exactly at cache window boundary is NOT resumable (conservative)."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        # Set to exactly cache_window_seconds ago
        mgr.last_activity = time.time() - config.cache_window_seconds
        assert mgr.should_resume() is False


# --- SDK Options Assembly ---


class TestCreateOptions:
    """SessionManager builds ClaudeAgentOptions with hooks and prompt."""

    def test_options_include_system_prompt(self, config, fixture_vault):
        """Options include the system prompt built from vault files."""
        mgr = SessionManager(config=config)

        with patch("obs_agent.session.build_system_prompt") as mock_prompt:
            mock_prompt.return_value = "You are a vault assistant."
            options = mgr.create_options()

        assert options.system_prompt == "You are a vault assistant."

    def test_options_include_hooks(self, config):
        """Options include all required hooks (pre_tool_use, stop, pre_compact)."""
        mgr = SessionManager(config=config)

        with patch("obs_agent.session.build_system_prompt", return_value="prompt"):
            options = mgr.create_options()

        assert options.hooks is not None
        hooks = options.hooks
        # Verify the critical hooks are registered
        assert "PreToolUse" in hooks or "pre_tool_use" in hooks or hasattr(hooks, "pre_tool_use")

    def test_resume_mode_includes_session_id(self, config):
        """When resuming, options include resume=session_id."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")

        with patch("obs_agent.session.build_system_prompt", return_value="prompt"):
            options = mgr.create_options()

        assert options.resume == "sess-xyz-789"

    def test_fresh_mode_no_resume(self, config):
        """When starting fresh, options do not include resume."""
        mgr = SessionManager(config=config)
        # No session_id set, so should be fresh

        with patch("obs_agent.session.build_system_prompt", return_value="prompt"):
            options = mgr.create_options()

        assert options.resume is None or not hasattr(options, "resume")

    def test_fresh_mode_after_timeout(self, config):
        """After timeout, options start a fresh session (no resume)."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-old")
        # Simulate timeout
        mgr.last_activity = time.time() - 7200  # 2 hours ago

        with patch("obs_agent.session.build_system_prompt", return_value="prompt"):
            options = mgr.create_options()

        # Should NOT resume the old session
        assert options.resume is None


# --- Context Loading ---


class TestContextLoading:
    """Fresh sessions load the latest context from vault."""

    def test_fresh_session_loads_updated_context(self, config, fixture_vault):
        """Fresh start reads the latest context.md content."""
        # Write specific content to context.md
        context_file = fixture_vault / "Agent" / "context.md"
        context_file.write_text("# Context\n\nFresh session test marker ABC.\n")

        mgr = SessionManager(config=config)

        with patch("obs_agent.session.build_system_prompt") as mock_prompt:
            mock_prompt.return_value = "prompt"
            mgr.create_options()

        # build_system_prompt should be called with the config
        mock_prompt.assert_called_once_with(config)

    def test_resume_still_uses_same_prompt(self, config):
        """Resumed sessions still use the system prompt (for cache match)."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")

        with patch("obs_agent.session.build_system_prompt") as mock_prompt:
            mock_prompt.return_value = "You are a vault assistant."
            options = mgr.create_options()

        # Even in resume mode, system_prompt must be set for cache matching
        assert options.system_prompt is not None
        assert len(options.system_prompt) > 0


# --- Session Reset ---


class TestSessionReset:
    """SessionManager can reset for a fresh start after flush."""

    def test_reset_clears_session_id(self, config):
        """reset() clears the active session_id."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        mgr.reset()
        assert mgr.session_id is None

    def test_reset_clears_timestamp(self, config):
        """reset() clears the last_activity timestamp."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        mgr.reset()
        assert mgr.last_activity is None

    def test_reset_makes_should_resume_false(self, config):
        """After reset, should_resume returns False."""
        mgr = SessionManager(config=config)
        mgr.set_session_id("sess-xyz-789")
        mgr.reset()
        assert mgr.should_resume() is False
