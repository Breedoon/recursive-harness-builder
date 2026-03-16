"""Tests for the agent naming redesign.

Covers:
- Slugification edge cases (unicode, special chars, long names, empty, collisions)
- Fingerprint determinism and uniqueness
- New agent_name format: trunk (no hash prefix), child ({parent_hash}-{slug})
- Timestamp-based team names
- XML enrichment with agent_name attributes
- Collision detection
- General topic → chat name substitution
"""

from __future__ import annotations

import re

import pytest

from obs_agent.lineage import (
    build_obs_bootstrap_xml,
    lineage_fingerprint,
    native_agent_name_for_lineage,
    normalize_lineage_name,
    parse_obs_bootstrap_xml,
    root_team_key_for_lineage,
    slugify_projection_label,
)


# ---------------------------------------------------------------------------
# N1–N5: Slugification
# ---------------------------------------------------------------------------


class TestSlugification:
    """Verify slugify_projection_label handles adversarial inputs."""

    def test_n1_unicode_and_mixed_case(self):
        """Unicode chars are replaced, result is lowercased."""
        slug = slugify_projection_label("Ünïcödé Tëst", fallback="x")
        assert slug  # non-empty
        assert slug == slug.lower()
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)

    def test_n2_whitespace_collapse(self):
        """Multiple spaces collapse to single dashes, no leading/trailing dashes."""
        slug = slugify_projection_label("  lots   of   space  ", fallback="x")
        assert "--" not in slug  # no double dashes
        assert not slug.startswith("-")
        assert not slug.endswith("-")
        assert "lots" in slug
        assert "space" in slug

    def test_n3_empty_and_whitespace_use_fallback(self):
        """Empty, whitespace-only, and dash-only inputs use fallback."""
        assert slugify_projection_label("", fallback="root") == "root"
        assert slugify_projection_label("   ", fallback="root") == "root"
        assert slugify_projection_label("---", fallback="root") == "root"
        assert slugify_projection_label(None, fallback="root") == "root"

    def test_n4_already_clean_passthrough(self):
        """A clean lowercase-dashed name passes through unchanged."""
        assert slugify_projection_label("worker-a", fallback="x") == "worker-a"

    def test_n5_special_chars_and_emoji(self):
        """Special characters and emoji are replaced with dashes."""
        slug = slugify_projection_label("Hello, World! (v2.0) 🚀", fallback="x")
        assert slug  # non-empty
        # Only alphanumeric and dashes
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)
        # Should contain the alphabetic parts
        assert "hello" in slug
        assert "world" in slug

    def test_n5b_long_name_produces_valid_slug(self):
        """A very long name (300 chars) still produces a valid slug."""
        long_name = "A" * 300
        slug = slugify_projection_label(long_name, fallback="x")
        assert len(slug) > 0
        # All lowercase a's and possibly dashes
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)

    def test_n5c_cjk_characters(self):
        """CJK unicode produces a slug (may be empty → fallback)."""
        slug = slugify_projection_label("研究エージェント", fallback="agent")
        assert slug  # non-empty — either transliterated or fallback
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)


# ---------------------------------------------------------------------------
# N6–N8: Fingerprint determinism and uniqueness
# ---------------------------------------------------------------------------


class TestFingerprint:
    """Verify lineage_fingerprint is deterministic and collision-resistant."""

    def test_n6_deterministic_across_calls(self):
        """Same lineage tuple produces identical fingerprint every time."""
        lineage = ("Root", "Child", "Grandchild")
        fp1 = lineage_fingerprint(lineage)
        fp2 = lineage_fingerprint(lineage)
        assert fp1 == fp2
        assert len(fp1) == 10
        assert all(c in "0123456789abcdef" for c in fp1)

    def test_n7_differs_for_reordered_lineage(self):
        """(A, B) and (B, A) produce different fingerprints."""
        fp_ab = lineage_fingerprint(("A", "B"))
        fp_ba = lineage_fingerprint(("B", "A"))
        assert fp_ab != fp_ba

    def test_n8_empty_lineage_no_crash(self):
        """Empty lineage produces a valid 10-char hex fingerprint."""
        fp = lineage_fingerprint(())
        assert len(fp) == 10
        assert all(c in "0123456789abcdef" for c in fp)

    def test_n8b_same_leaf_different_parent_different_fp(self):
        """Two agents named 'research' under different parents get different fingerprints."""
        fp_a = lineage_fingerprint(("Root", "research"))
        fp_b = lineage_fingerprint(("Other", "research"))
        assert fp_a != fp_b

    def test_n8c_slugification_collision_different_fingerprints(self):
        """Names that produce the same slug still get different fingerprints."""
        slug_a = slugify_projection_label("Hello World", fallback="x")
        slug_b = slugify_projection_label("hello_world", fallback="x")
        assert slug_a == slug_b  # both become "hello-world"
        # But fingerprints are based on original names, so they differ
        fp_a = lineage_fingerprint(("Hello World",))
        fp_b = lineage_fingerprint(("hello_world",))
        assert fp_a != fp_b


# ---------------------------------------------------------------------------
# N9–N13: Current projection functions (before redesign)
# These test the CURRENT behavior so the redesign has a baseline.
# After implementation, these will be UPDATED to match new format.
# ---------------------------------------------------------------------------


class TestCurrentProjectionBaseline:
    """Baseline tests for current naming format.

    These document the CURRENT behavior (obs-tree-*, obs-agent-*).
    When the redesign lands, these tests will be updated to assert the NEW format:
    - root_team_key: timestamp-based (YYYY-MM-DD-HH-MM-{slug})
    - native_agent_name: trunk = {slug} (no prefix), child = {parent_hash}-{slug}
    """

    def test_n9_root_team_key_format(self):
        """Root team key has obs-tree- prefix, slug, and hash."""
        key = root_team_key_for_lineage(("My Chat",))
        assert key.startswith("obs-tree-")
        assert "my-chat" in key
        # Format: obs-tree-{slug}-{hash10}
        parts = key.split("-")
        assert len(parts) >= 4  # obs, tree, slug..., hash

    def test_n10_root_team_key_empty_lineage(self):
        """Empty lineage returns sentinel."""
        key = root_team_key_for_lineage(())
        assert key == "obs-tree-root-0000000000"

    def test_n11_native_agent_name_format(self):
        """Native agent name has obs-agent- prefix, leaf slug, and hash."""
        name = native_agent_name_for_lineage(("Root Topic", "Child Topic", "worker-a"))
        assert name.startswith("obs-agent-")
        assert "worker-a" in name

    def test_n12_native_agent_name_empty_lineage(self):
        """Empty lineage returns sentinel."""
        name = native_agent_name_for_lineage(())
        assert name == "obs-agent-root-0000000000"

    def test_n13_same_leaf_different_path_different_name(self):
        """Same leaf name under different parents produces different agent names."""
        name_a = native_agent_name_for_lineage(("A", "worker"))
        name_b = native_agent_name_for_lineage(("B", "worker"))
        assert name_a != name_b
        # Both contain "worker" in the slug
        assert "worker" in name_a
        assert "worker" in name_b


# ---------------------------------------------------------------------------
# N14–N16: Collision detection and edge cases
# ---------------------------------------------------------------------------


class TestCollisionAndEdgeCases:
    """Test naming collision scenarios and edge cases."""

    def test_n14_general_topic_collision_documents_bug(self):
        """Two different chats using 'General' produce IDENTICAL keys (known bug).

        This test documents the current broken behavior. After the General fix
        lands, this test should be UPDATED to verify they're different (using
        chat titles instead of 'General').
        """
        key_chat_a = root_team_key_for_lineage(("General",))
        key_chat_b = root_team_key_for_lineage(("General",))
        assert key_chat_a == key_chat_b  # Bug: both are identical

    def test_n15_sibling_same_name_different_hash(self):
        """Same display name under different parents → different agent names."""
        name_a = native_agent_name_for_lineage(("Root", "Branch-A", "research"))
        name_b = native_agent_name_for_lineage(("Root", "Branch-B", "research"))
        assert name_a != name_b

    def test_n16_deep_lineage_10_levels(self):
        """10-level lineage produces valid, non-empty names."""
        lineage = tuple(f"Level-{i}" for i in range(10))
        key = root_team_key_for_lineage(lineage)
        name = native_agent_name_for_lineage(lineage)
        assert key  # non-empty
        assert name  # non-empty
        assert "level-9" in name  # leaf is Level-9

    def test_n16b_lineage_with_unit_separator_in_name(self):
        """Name containing \\x1f (used as internal delimiter) still works."""
        lineage = ("Root\x1fTricky",)
        fp = lineage_fingerprint(lineage)
        assert len(fp) == 10

    def test_n16c_case_only_difference_produces_different_fingerprint(self):
        """Names differing only in case produce different fingerprints."""
        fp_upper = lineage_fingerprint(("ROOT",))
        fp_lower = lineage_fingerprint(("root",))
        assert fp_upper != fp_lower


# ---------------------------------------------------------------------------
# Redesigned naming format tests (will FAIL until implementation lands)
# ---------------------------------------------------------------------------


class TestRedesignedNamingFormat:
    """Tests for the NEW naming format from the redesign.

    These tests will FAIL until the implementation changes are made.
    They define the target behavior:
    - Trunk agent_name: just the slug (no obs-agent- prefix, no hash)
    - Child agent_name: {parent_lineage_hash}-{slug}
    - Team name: timestamp-based (YYYY-MM-DD-HH-MM-{slug})
    """

    @pytest.mark.xfail(reason="Redesign not implemented yet — new trunk naming format")
    def test_trunk_agent_name_no_prefix(self):
        """Trunk agent has no hash prefix — just the slug."""
        name = native_agent_name_for_lineage(("My Topic",))
        assert name == "my-topic"  # No obs-agent- prefix, no hash

    @pytest.mark.xfail(reason="Redesign not implemented yet — child naming format")
    def test_child_agent_name_has_parent_hash(self):
        """Child agent has {parent_hash}-{slug} format."""
        name = native_agent_name_for_lineage(("Root", "Worker"))
        parent_hash = lineage_fingerprint(("Root",))
        assert name == f"{parent_hash}-worker"

    @pytest.mark.xfail(reason="Redesign not implemented yet — deep child naming")
    def test_deep_child_agent_name(self):
        """Deep child uses hash of parent lineage (not full lineage)."""
        name = native_agent_name_for_lineage(("Root", "A", "B", "C"))
        parent_hash = lineage_fingerprint(("Root", "A", "B"))
        assert name == f"{parent_hash}-c"

    @pytest.mark.xfail(reason="Redesign not implemented yet — timestamp team names")
    def test_timestamp_team_name_format(self):
        """Team name uses timestamp instead of obs-tree- prefix."""
        team_name = root_team_key_for_lineage(("My Topic",))
        # Should NOT start with obs-tree-
        assert not team_name.startswith("obs-tree-")
        # Should match YYYY-MM-DD-HH-MM-{slug} pattern
        assert re.match(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-my-topic", team_name)


# ---------------------------------------------------------------------------
# XML enrichment tests (agent_name attribute on obs-node)
# ---------------------------------------------------------------------------


class TestXMLEnrichment:
    """Tests for adding agent_name attribute to bootstrap XML obs-node elements."""

    @pytest.mark.xfail(reason="Redesign not implemented yet — XML agent_name enrichment")
    def test_obs_node_has_agent_name_attribute(self):
        """Each obs-node in the XML should have an agent_name attribute."""
        lineage = ("Root", "Child")
        xml = build_obs_bootstrap_xml(
            lineage=lineage,
            origin="test",
            is_fork=False,
            session_id="test-sid",
            root_team_key="test-team",
            native_agent_name="test-agent",
        )
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml)
        nodes = root.findall(".//obs-node")
        assert len(nodes) == 2
        for node in nodes:
            assert "agent_name" in node.attrib, f"obs-node missing agent_name: {node.attrib}"

    @pytest.mark.xfail(reason="Redesign not implemented yet — XML agent_name round-trip")
    def test_agent_name_survives_parse_round_trip(self):
        """agent_name attributes survive build → parse → re-read cycle."""
        lineage = ("Root", "Leaf")
        xml = build_obs_bootstrap_xml(
            lineage=lineage,
            origin="test",
            is_fork=False,
            session_id="test-sid",
            root_team_key="test-team",
            native_agent_name="test-agent",
        )
        parsed = parse_obs_bootstrap_xml(xml)
        # The parsed object should expose agent_names somehow
        # (exact API depends on implementation — may be a dict or list)
        assert parsed.lineage == lineage
        # Verify raw XML has the attributes
        import xml.etree.ElementTree as ET

        root = ET.fromstring(parsed.raw_xml)
        nodes = root.findall(".//obs-node")
        for node in nodes:
            assert "agent_name" in node.attrib
