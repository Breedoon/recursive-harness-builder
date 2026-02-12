"""Tests for obs_agent.prompt - Step 2 TDD (RED phase).

These tests define the System Prompt Builder contract.
All should FAIL until prompt.py is implemented.

The system prompt assembles identity, context, behavior, skills, safety,
and vault map sections from vault files. See implementation-plan.md Step 2
and decision D025 (context.md as orientation document).
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
        # A real prompt should be at least a few hundred chars
        assert len(prompt) > 200


# --- Identity Section ---


class TestPromptIdentity:
    """Prompt includes agent identity and personality."""

    def test_includes_identity_marker(self, config):
        """Prompt contains an identity/personality section."""
        prompt = build_system_prompt(config)
        # Should have some identity marker - the agent needs to know who it is
        prompt_lower = prompt.lower()
        assert any(
            term in prompt_lower
            for term in ["identity", "personality", "you are", "your role", "assistant"]
        ), "Prompt must contain identity/personality instructions"

    def test_includes_obsidian_reference(self, config):
        """Prompt mentions Obsidian vault context."""
        prompt = build_system_prompt(config)
        assert "obsidian" in prompt.lower() or "vault" in prompt.lower(), (
            "Prompt must reference Obsidian or vault"
        )


# --- Context Section ---


class TestPromptContext:
    """Prompt includes content from Agent/context.md."""

    def test_includes_context_content(self, config, fixture_vault):
        """Prompt includes the actual text from context.md."""
        # The fixture vault has "Test context." in context.md
        prompt = build_system_prompt(config)
        assert "Test context" in prompt, (
            "Prompt must include content from Agent/context.md"
        )

    def test_includes_custom_context(self, config, fixture_vault):
        """Prompt reflects whatever is in context.md, not hardcoded text."""
        context_file = fixture_vault / "Agent" / "context.md"
        context_file.write_text("# Agent Context\n\nCustom unique marker XYZ123.\n")
        prompt = build_system_prompt(config)
        assert "XYZ123" in prompt, (
            "Prompt must dynamically include context.md content"
        )


# --- Skills Section ---


class TestPromptSkills:
    """Prompt references core skills by name and describes their triggers."""

    CORE_SKILL_NAMES = [
        "file-conventions",
        "update-context",
        "manage-summaries",
        "create-reference",
    ]

    def test_references_core_skill_names(self, config, fixture_vault):
        """Prompt mentions all four core skill names."""
        # Create minimal skill files so the builder can find them
        for skill_name in self.CORE_SKILL_NAMES:
            skill_dir = fixture_vault / "Agent" / "skills" / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {skill_name}\n---\n# {skill_name}\nSkill content.\n"
            )

        prompt = build_system_prompt(config)
        for name in self.CORE_SKILL_NAMES:
            assert name in prompt, f"Prompt must reference core skill '{name}'"

    def test_includes_skill_descriptions(self, config, fixture_vault):
        """Prompt includes brief descriptions or triggers for skills."""
        # Create a skill with a known description
        skill_dir = fixture_vault / "Agent" / "skills" / "file-conventions"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: file-conventions\n"
            "description: Master reference for vault file operations\n"
            "---\n# file-conventions\n"
        )

        prompt = build_system_prompt(config)
        # The prompt should include skill descriptions or content, not just names
        assert "file" in prompt.lower() and "convention" in prompt.lower(), (
            "Prompt must include skill descriptions, not just names"
        )


# --- Safety Section ---


class TestPromptSafety:
    """Prompt includes safety guardrails."""

    def test_mentions_immutable_files(self, config):
        """Prompt warns about immutable files (Meeting Notes)."""
        prompt = build_system_prompt(config)
        prompt_lower = prompt.lower()
        assert "meeting notes" in prompt_lower or "immutable" in prompt_lower, (
            "Prompt must mention immutable files or Meeting Notes"
        )

    def test_mentions_oversight(self, config):
        """Prompt includes oversight or safety guardrails."""
        prompt = build_system_prompt(config)
        prompt_lower = prompt.lower()
        assert any(
            term in prompt_lower
            for term in ["guard", "protect", "immutable", "never edit", "do not modify", "block"]
        ), "Prompt must include safety/oversight instructions"


# --- Behavior Section ---


class TestPromptBehavior:
    """Prompt includes behavioral guidance for the agent."""

    def test_mentions_resourcefulness(self, config):
        """Prompt instructs agent to be resourceful and proactive."""
        prompt = build_system_prompt(config)
        prompt_lower = prompt.lower()
        assert any(
            term in prompt_lower
            for term in ["resourceful", "proactive", "connect", "anticipate", "initiative"]
        ), "Prompt must include proactive/resourceful behavior instructions"

    def test_mentions_connecting_dots(self, config):
        """Prompt instructs agent to connect information across vault."""
        prompt = build_system_prompt(config)
        prompt_lower = prompt.lower()
        assert any(
            term in prompt_lower
            for term in ["connect", "relate", "cross-reference", "link", "pattern"]
        ), "Prompt must include instructions about connecting information"


# --- Vault Map ---


class TestPromptVaultMap:
    """Prompt includes the vault directory structure."""

    def test_includes_vault_map(self, config):
        """Prompt contains the vault directory structure overview."""
        prompt = build_system_prompt(config)
        prompt_lower = prompt.lower()
        # The vault map should reference key top-level directories
        assert any(
            term in prompt_lower
            for term in ["vault/", "agent/", "directory", "structure", "map"]
        ), "Prompt must include vault directory structure"

    def test_vault_map_shows_key_dirs(self, config):
        """Vault map mentions the main directories: Agent, Misc, Vault."""
        prompt = build_system_prompt(config)
        # These are the core top-level vault directories
        assert "Agent" in prompt, "Vault map must reference Agent/"
        assert "Misc" in prompt or "misc" in prompt.lower(), (
            "Vault map must reference Misc/"
        )


# --- Graceful Fallbacks ---


class TestPromptFallbacks:
    """Prompt builder handles missing files gracefully."""

    def test_missing_context_file(self, tmp_path):
        """Builder returns valid prompt even when context.md is missing."""
        vault = tmp_path / "vault"
        agent = vault / "Agent"
        agent.mkdir(parents=True)
        # No context.md created
        cfg = OBSConfig(vault_path=vault)

        prompt = build_system_prompt(cfg)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, "Prompt must still be valid without context.md"

    def test_missing_skill_files(self, tmp_path):
        """Builder returns valid prompt even when skill files are missing."""
        vault = tmp_path / "vault"
        agent = vault / "Agent"
        agent.mkdir(parents=True)
        (agent / "context.md").write_text("# Context\nSome context.\n")
        # No skills/ directory
        cfg = OBSConfig(vault_path=vault)

        prompt = build_system_prompt(cfg)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, "Prompt must still be valid without skill files"

    def test_empty_context_file(self, tmp_path):
        """Builder handles an empty context.md without crashing."""
        vault = tmp_path / "vault"
        agent = vault / "Agent"
        agent.mkdir(parents=True)
        (agent / "context.md").write_text("")
        cfg = OBSConfig(vault_path=vault)

        prompt = build_system_prompt(cfg)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, "Prompt must still be valid with empty context.md"
