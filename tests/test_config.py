"""Tests for obs_agent.config - Step 1 TDD (RED phase).

These tests define the Config contract. All should FAIL until config.py is implemented.
"""

import os
from pathlib import Path

import pytest

import obs_agent.config as config_module
from obs_agent.config import OBSConfig


# --- Vault Path Resolution ---


class TestVaultPathResolution:
    """Config resolves the vault path from defaults or environment."""

    def test_default_vault_path(self):
        """Default vault path points to the iCloud Obsidian vault."""
        cfg = OBSConfig()
        expected = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "T"
        assert cfg.vault_path == expected

    def test_vault_path_from_constructor(self, tmp_path):
        """Vault path can be overridden via constructor."""
        cfg = OBSConfig(vault_path=tmp_path)
        assert cfg.vault_path == tmp_path

    def test_vault_path_from_env_var(self, tmp_path, monkeypatch):
        """OBS_VAULT_PATH env var overrides the default vault path."""
        monkeypatch.setenv("OBS_VAULT_PATH", str(tmp_path))
        cfg = OBSConfig.from_env()
        assert cfg.vault_path == tmp_path

    def test_constructor_takes_precedence_over_env(self, tmp_path, monkeypatch):
        """Explicit constructor arg takes precedence over env var."""
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("OBS_VAULT_PATH", str(tmp_path))
        cfg = OBSConfig(vault_path=other)
        assert cfg.vault_path == other


# --- Claude Directory Paths ---


class TestClaudePaths:
    """Config provides paths to all .claude subdirectories and files."""

    def test_claude_path(self, config, fixture_vault):
        """claude_path points to .claude/ inside vault."""
        assert config.claude_path == fixture_vault / ".claude"

    def test_context_path(self, config, fixture_vault):
        """context_path points to CLAUDE.md at vault root."""
        assert config.context_path == fixture_vault / "CLAUDE.md"

    def test_skills_dir(self, config, fixture_vault):
        """skills_dir points to .claude/skills/."""
        assert config.skills_dir == fixture_vault / ".claude" / "skills"

    def test_memory_dir(self, config, fixture_vault):
        """memory_dir points to .claude/memory/."""
        assert config.memory_dir == fixture_vault / ".claude" / "memory"

    def test_system_dir(self, config, fixture_vault):
        """system_dir points to .claude/system/."""
        assert config.system_dir == fixture_vault / ".claude" / "system"

    def test_topics_dir(self, config, fixture_vault):
        """topics_dir points to .claude/topics/."""
        assert config.topics_dir == fixture_vault / ".claude" / "topics"

    def test_drafts_dir(self, config, fixture_vault):
        """drafts_dir points to .claude/drafts/."""
        assert config.drafts_dir == fixture_vault / ".claude" / "drafts"

    def test_memory_parent_note(self, config, fixture_vault):
        """memory_parent_note points to .claude/memory.md."""
        assert config.memory_parent_note == fixture_vault / ".claude" / "memory.md"


# --- Daemon Settings ---


class TestDaemonSettings:
    """Config provides daemon server settings."""

    def test_default_host(self):
        """Default daemon host is localhost."""
        cfg = OBSConfig()
        assert cfg.daemon_host == "127.0.0.1"

    def test_default_port(self):
        """Default daemon port is 7832."""
        cfg = OBSConfig()
        assert cfg.daemon_port == 7832

    def test_port_from_env(self, monkeypatch):
        """OBS_DAEMON_PORT env var overrides default port."""
        monkeypatch.setenv("OBS_DAEMON_PORT", "9999")
        cfg = OBSConfig.from_env()
        assert cfg.daemon_port == 9999

    def test_host_from_env(self, monkeypatch):
        """OBS_DAEMON_HOST env var overrides default host."""
        monkeypatch.setenv("OBS_DAEMON_HOST", "0.0.0.0")
        cfg = OBSConfig.from_env()
        assert cfg.daemon_host == "0.0.0.0"

    def test_base_url(self):
        """base_url composes host and port."""
        cfg = OBSConfig()
        assert cfg.base_url == "http://127.0.0.1:7832"


# --- Session Settings ---


class TestSessionSettings:
    """Config provides session management settings."""

    def test_default_model(self):
        """Default config stores model identity without context suffix."""
        cfg = OBSConfig()
        assert cfg.model == "claude-opus-4-6"

    def test_model_from_env_resolves_shorthand_without_context_suffix(self, monkeypatch):
        """OBS_AGENT_MODEL overrides the default session model identity."""
        monkeypatch.setenv("OBS_AGENT_MODEL", "haiku")
        cfg = OBSConfig.from_env()
        assert cfg.model == "claude-haiku-4-5"

    def test_model_from_env_preserves_explicit_context_suffix(self, monkeypatch):
        monkeypatch.setenv("OBS_AGENT_MODEL", "gpt[200k]")
        cfg = OBSConfig.from_env()
        assert cfg.model == "gpt-5.4-mini[200k]"

    def test_default_cache_window(self):
        """Default cache window is effectively non-expiring for now."""
        cfg = OBSConfig()
        assert cfg.cache_window_seconds == 1000 * 60 * 60

    def test_cache_window_from_env(self, monkeypatch):
        """OBS_CACHE_WINDOW env var overrides cache window."""
        monkeypatch.setenv("OBS_CACHE_WINDOW", "1800")
        cfg = OBSConfig.from_env()
        assert cfg.cache_window_seconds == 1800

    def test_context_probe_cli_default_off(self):
        """Claude CLI context probe is opt-in by default."""
        cfg = OBSConfig()
        assert cfg.context_probe_claude_cli is False

    def test_context_probe_cli_from_env(self, monkeypatch):
        """OBS_CONTEXT_PROBE_CLAUDE_CLI toggles context probe path."""
        monkeypatch.setenv("OBS_CONTEXT_PROBE_CLAUDE_CLI", "1")
        cfg = OBSConfig.from_env()
        assert cfg.context_probe_claude_cli is True


class TestBgForkTimeout:
    """Config provides background fork timeout settings."""

    def test_default_bg_fork_timeout(self):
        """Default background fork timeout is 600 seconds."""
        cfg = OBSConfig()
        assert cfg.bg_fork_timeout == 600.0

    def test_bg_fork_timeout_from_constructor(self):
        """bg_fork_timeout can be overridden via constructor."""
        cfg = OBSConfig(bg_fork_timeout=300.0)
        assert cfg.bg_fork_timeout == 300.0

    def test_bg_fork_timeout_from_env(self, monkeypatch):
        """OBS_BG_FORK_TIMEOUT env var overrides default timeout."""
        monkeypatch.setenv("OBS_BG_FORK_TIMEOUT", "900")
        cfg = OBSConfig.from_env()
        assert cfg.bg_fork_timeout == 900.0


class TestTelegramNotifyUsername:
    def test_notify_username_default_none(self):
        cfg = OBSConfig()
        assert cfg.telegram_notify_username is None

    def test_notify_username_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_NOTIFY_USERNAME", "@breedoon")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_notify_username == "breedoon"


class TestTelegramGroupFolderConfig:
    def test_group_folder_title_default_none(self):
        cfg = OBSConfig()
        assert cfg.telegram_group_folder_title is None

    def test_group_folder_title_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_GROUP_FOLDER_TITLE", "Claudia")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_group_folder_title == "Claudia"

    def test_group_addlist_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_GROUP_ADDLIST_URL", "https://t.me/addlist/sPnRtk8389lhNjQ0")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_group_addlist_url == "https://t.me/addlist/sPnRtk8389lhNjQ0"


# --- Immutable Paths ---


class TestImmutablePaths:
    """Config defines paths that the agent must not modify."""

    def test_immutable_patterns_exist(self):
        """Config has a list of immutable path patterns."""
        cfg = OBSConfig()
        assert isinstance(cfg.immutable_patterns, list)
        assert len(cfg.immutable_patterns) > 0

    def test_meeting_notes_are_immutable(self):
        """Misc/Meeting Notes/ is in the immutable patterns."""
        cfg = OBSConfig()
        assert any("Meeting Notes" in p for p in cfg.immutable_patterns)

    def test_is_immutable_matching(self, config, fixture_vault):
        """is_immutable() returns True for paths matching immutable patterns."""
        meeting_path = fixture_vault / "Misc" / "Meeting Notes" / "2025-01-15 standup.md"
        assert config.is_immutable(meeting_path) is True

    def test_is_immutable_non_matching(self, config, fixture_vault):
        """is_immutable() returns False for normal vault paths."""
        normal_path = fixture_vault / "CLAUDE.md"
        assert config.is_immutable(normal_path) is False


# --- Vault Structure Validation ---


class TestVaultValidation:
    """Config can validate that expected vault structure exists."""

    def test_validate_passes_for_fixture_vault(self, config):
        """validate() succeeds when vault structure exists."""
        # Should not raise
        config.validate()

    def test_validate_fails_for_missing_vault(self, tmp_path):
        """validate() raises when vault path doesn't exist."""
        cfg = OBSConfig(vault_path=tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            cfg.validate()

    def test_validate_fails_for_missing_claude_dir(self, tmp_path):
        """validate() raises when .claude/ directory is missing."""
        vault = tmp_path / "empty_vault"
        vault.mkdir()
        cfg = OBSConfig(vault_path=vault)
        with pytest.raises(FileNotFoundError):
            cfg.validate()

    def test_validate_fails_for_missing_claude_md(self, tmp_path):
        """validate() raises when CLAUDE.md is missing."""
        vault = tmp_path / "vault"
        (vault / ".claude").mkdir(parents=True)
        cfg = OBSConfig(vault_path=vault)
        with pytest.raises(FileNotFoundError):
            cfg.validate()

    def test_validate_fails_when_state_db_inside_temp_root(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / ".claude").mkdir(parents=True)
        (vault / "CLAUDE.md").write_text("# context")
        temp_root = tmp_path / "tg-temp"
        cfg = OBSConfig(
            vault_path=vault,
            telegram_temp_root=temp_root,
            telegram_state_db_path=temp_root / "telegram-state.sqlite3",
        )
        with pytest.raises(ValueError, match="outside OBS_TELEGRAM_TEMP_ROOT"):
            cfg.validate()


# --- Telegram Settings ---


class TestTelegramSettings:
    """Config provides Telegram bot settings."""

    def test_default_token_is_none(self):
        """Default Telegram bot token is None."""
        cfg = OBSConfig()
        assert cfg.telegram_bot_token is None

    def test_default_allowed_users_empty(self):
        """Default allowed user IDs is empty list."""
        cfg = OBSConfig()
        assert cfg.telegram_allowed_user_ids == []

    def test_token_from_env(self, monkeypatch):
        """OBS_TELEGRAM_BOT_TOKEN env var sets the token."""
        monkeypatch.setenv("OBS_TELEGRAM_BOT_TOKEN", "test-token-123")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_bot_token == "test-token-123"

    def test_allowed_users_from_env(self, monkeypatch):
        """OBS_TELEGRAM_ALLOWED_USERS env var sets allowed user IDs."""
        monkeypatch.setenv("OBS_TELEGRAM_ALLOWED_USERS", "111,222,333")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_allowed_user_ids == [111, 222, 333]

    def test_allowed_users_with_spaces(self, monkeypatch):
        """OBS_TELEGRAM_ALLOWED_USERS handles spaces in CSV."""
        monkeypatch.setenv("OBS_TELEGRAM_ALLOWED_USERS", "111, 222, 333")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_allowed_user_ids == [111, 222, 333]

    def test_allowed_users_single(self, monkeypatch):
        """OBS_TELEGRAM_ALLOWED_USERS works with a single user."""
        monkeypatch.setenv("OBS_TELEGRAM_ALLOWED_USERS", "42")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_allowed_user_ids == [42]

    def test_token_from_constructor(self):
        """Token can be set via constructor."""
        cfg = OBSConfig(telegram_bot_token="my-token")
        assert cfg.telegram_bot_token == "my-token"

    def test_allowed_users_from_constructor(self):
        """Allowed users can be set via constructor."""
        cfg = OBSConfig(telegram_allowed_user_ids=[1, 2, 3])
        assert cfg.telegram_allowed_user_ids == [1, 2, 3]

    def test_temp_root_default(self):
        cfg = OBSConfig()
        assert cfg.telegram_temp_root == Path("/tmp/obs-agent")

    def test_temp_root_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OBS_TELEGRAM_TEMP_ROOT", str(tmp_path / "tg-temp"))
        cfg = OBSConfig.from_env()
        assert cfg.telegram_temp_root == tmp_path / "tg-temp"

    def test_state_db_path_default(self):
        cfg = OBSConfig()
        expected = (
            Path(config_module.__file__).resolve().parents[2]
            / ".obs-agent"
            / "state"
            / "telegram-state.sqlite3"
        )
        assert cfg.telegram_state_db_path == expected

    def test_state_db_path_from_env(self, monkeypatch, tmp_path):
        db_path = tmp_path / "state.sqlite3"
        monkeypatch.setenv("OBS_TELEGRAM_STATE_DB_PATH", str(db_path))
        cfg = OBSConfig.from_env()
        assert cfg.telegram_state_db_path == db_path

    def test_state_retention_days_default(self):
        cfg = OBSConfig()
        assert cfg.telegram_state_retention_days == 30

    def test_state_retention_days_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_STATE_RETENTION_DAYS", "14")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_state_retention_days == 14

    def test_transcription_script_from_env(self, monkeypatch, tmp_path):
        script = tmp_path / "transcribe.sh"
        monkeypatch.setenv("OBS_TELEGRAM_TRANSCRIPTION_SCRIPT", str(script))
        cfg = OBSConfig.from_env()
        assert cfg.telegram_transcription_script == script

    def test_userbot_api_id_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_USERBOT_API_ID", "123456")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_userbot_api_id == 123456

    def test_userbot_api_hash_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_USERBOT_API_HASH", "hash-xyz")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_userbot_api_hash == "hash-xyz"

    def test_userbot_session_from_env(self, monkeypatch):
        monkeypatch.setenv("OBS_TELEGRAM_USERBOT_SESSION", "session-xyz")
        cfg = OBSConfig.from_env()
        assert cfg.telegram_userbot_session == "session-xyz"
