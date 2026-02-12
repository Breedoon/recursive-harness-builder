"""Tests for obs_agent.config - Step 1 TDD (RED phase).

These tests define the Config contract. All should FAIL until config.py is implemented.
"""

import os
from pathlib import Path

import pytest

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


# --- Agent Directory Paths ---


class TestAgentPaths:
    """Config provides paths to all Agent subdirectories and files."""

    def test_agent_path(self, config, fixture_vault):
        """agent_path points to Agent/ inside vault."""
        assert config.agent_path == fixture_vault / "Agent"

    def test_context_path(self, config, fixture_vault):
        """context_path points to Agent/context.md."""
        assert config.context_path == fixture_vault / "Agent" / "context.md"

    def test_skills_dir(self, config, fixture_vault):
        """skills_dir points to Agent/skills/."""
        assert config.skills_dir == fixture_vault / "Agent" / "skills"

    def test_memory_dir(self, config, fixture_vault):
        """memory_dir points to Agent/memory/."""
        assert config.memory_dir == fixture_vault / "Agent" / "memory"

    def test_system_dir(self, config, fixture_vault):
        """system_dir points to Agent/system/."""
        assert config.system_dir == fixture_vault / "Agent" / "system"

    def test_topics_dir(self, config, fixture_vault):
        """topics_dir points to Agent/topics/."""
        assert config.topics_dir == fixture_vault / "Agent" / "topics"

    def test_drafts_dir(self, config, fixture_vault):
        """drafts_dir points to Agent/drafts/."""
        assert config.drafts_dir == fixture_vault / "Agent" / "drafts"

    def test_memory_parent_note(self, config, fixture_vault):
        """memory_parent_note points to Agent/memory.md."""
        assert config.memory_parent_note == fixture_vault / "Agent" / "memory.md"

    def test_skills_manifest(self, config, fixture_vault):
        """skills_manifest points to Agent/skills.md."""
        assert config.skills_manifest == fixture_vault / "Agent" / "skills.md"


# --- Skill Paths ---


class TestSkillPaths:
    """Config resolves paths to individual skill SKILL.md files."""

    CORE_SKILLS = [
        "update-context",
        "manage-summaries",
        "create-reference",
        "file-conventions",
    ]

    def test_core_skill_names(self, config):
        """core_skills returns the list of always-loaded skill names."""
        assert config.core_skills == self.CORE_SKILLS

    def test_skill_path_resolution(self, config, fixture_vault):
        """skill_path() resolves a skill name to its SKILL.md path."""
        path = config.skill_path("file-conventions")
        assert path == fixture_vault / "Agent" / "skills" / "file-conventions" / "SKILL.md"

    def test_all_core_skill_paths(self, config, fixture_vault):
        """All core skill paths resolve correctly."""
        for name in self.CORE_SKILLS:
            path = config.skill_path(name)
            assert path.parent.name == name
            assert path.name == "SKILL.md"

    def test_deeper_skill_path(self, config, fixture_vault):
        """Deeper skills (not core) also resolve via skill_path()."""
        path = config.skill_path("session-offboard")
        assert path == fixture_vault / "Agent" / "skills" / "session-offboard" / "SKILL.md"


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

    def test_default_cache_window(self):
        """Default cache window is 58 minutes (3480 seconds)."""
        cfg = OBSConfig()
        assert cfg.cache_window_seconds == 3480

    def test_cache_window_from_env(self, monkeypatch):
        """OBS_CACHE_WINDOW env var overrides cache window."""
        monkeypatch.setenv("OBS_CACHE_WINDOW", "1800")
        cfg = OBSConfig.from_env()
        assert cfg.cache_window_seconds == 1800


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
        normal_path = fixture_vault / "Agent" / "context.md"
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

    def test_validate_fails_for_missing_agent_dir(self, tmp_path):
        """validate() raises when Agent/ directory is missing."""
        vault = tmp_path / "empty_vault"
        vault.mkdir()
        cfg = OBSConfig(vault_path=vault)
        with pytest.raises(FileNotFoundError):
            cfg.validate()

    def test_validate_fails_for_missing_context(self, tmp_path):
        """validate() raises when Agent/context.md is missing."""
        vault = tmp_path / "vault"
        (vault / "Agent").mkdir(parents=True)
        cfg = OBSConfig(vault_path=vault)
        with pytest.raises(FileNotFoundError):
            cfg.validate()
