"""Tests for obs_agent.prompt - System Prompt Builder.

The system prompt is now read directly from CLAUDE.md at the vault root.
CLAUDE.md contains all sections: identity, behavior, skills, safety,
vault map, and dynamic context. See decision D025.
"""

from pathlib import Path

import pytest

from obs_agent.config import OBSConfig
from obs_agent.prompt import build_system_prompt


# --- Basic Contract ---


class TestBuildPromptContract:
    """build_system_prompt returns a well-formed prompt string."""

    def test_returns_string(self, config):
        """build_system_prompt returns a non-empty string."""
        prompt = build_system_prompt(config)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_returns_substantial_content(self, config):
        """Prompt is substantial - not just a placeholder line."""
        prompt = build_system_prompt(config)
        # A real prompt should be at least a few characters from CLAUDE.md
        assert len(prompt) > 10


# --- CLAUDE.md Content ---


class TestPromptFromClaudeMd:
    """Prompt is loaded directly from CLAUDE.md at vault root."""

    def test_includes_claude_md_content(self, config, fixture_vault):
        """Prompt includes the actual text from CLAUDE.md."""
        # The fixture vault has "Test context." in CLAUDE.md
        prompt = build_system_prompt(config)
        assert "Test context" in prompt, (
            "Prompt must include content from CLAUDE.md"
        )

    def test_includes_custom_content(self, config, fixture_vault):
        """Prompt reflects whatever is in CLAUDE.md, not hardcoded text."""
        context_file = fixture_vault / "CLAUDE.md"
        context_file.write_text("# OBS Agent\n\nCustom unique marker XYZ123.\n")
        prompt = build_system_prompt(config)
        assert "XYZ123" in prompt, (
            "Prompt must dynamically include CLAUDE.md content"
        )

    def test_full_claude_md_is_returned(self, config, fixture_vault):
        """The entire CLAUDE.md content is used as the system prompt."""
        content = "# OBS Agent\n\n## Identity\nI am an agent.\n\n## Safety\nBe safe.\n"
        (fixture_vault / "CLAUDE.md").write_text(content)
        prompt = build_system_prompt(config)
        assert prompt == content


# --- Graceful Fallbacks ---


class TestPromptFallbacks:
    """Prompt builder handles missing files gracefully."""

    def test_missing_claude_md(self, tmp_path):
        """Builder returns fallback prompt when CLAUDE.md is missing."""
        vault = tmp_path / "vault"
        vault.mkdir(parents=True)
        # No CLAUDE.md created
        cfg = OBSConfig(vault_path=vault)

        prompt = build_system_prompt(cfg)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, "Prompt must still be valid without CLAUDE.md"
        assert "missing" in prompt.lower(), "Fallback prompt should mention CLAUDE.md is missing"

    def test_empty_claude_md(self, tmp_path):
        """Builder returns fallback prompt when CLAUDE.md is empty."""
        vault = tmp_path / "vault"
        vault.mkdir(parents=True)
        (vault / "CLAUDE.md").write_text("")
        cfg = OBSConfig(vault_path=vault)

        prompt = build_system_prompt(cfg)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, "Prompt must still be valid with empty CLAUDE.md"
        assert "missing" in prompt.lower(), "Fallback prompt should mention CLAUDE.md is missing"
