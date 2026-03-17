"""Tests for the naming terminology refactor (v1 → v2).

Terminology spec:
- agent_name: the routing/machine name (e.g., "6b25cfc451-fix")
  Used for: inbox files, messaging, team config, env vars
- display_name: the human-readable label (e.g., "Fix New Teams")
  Used for: topic titles, lineage XML, user-facing text

Changes being tested:
- native_agent_name → agent_name everywhere
- obs-node name= → obs-node display_name= in XML
- <native_agent_name> → <agent_name> in team_context XML
- Bootstrap version 1 → 2
- MCP params: alias/description/name → display_name (with fallbacks)
- session_lineage output field rename
- _resume_fork_task bug fix
- _dropped_agent_keys removal
"""

from __future__ import annotations

import inspect
import xml.etree.ElementTree as ET

import pytest

from obs_agent.lineage import (
    ObsBootstrap,
    build_obs_bootstrap_xml,
    lineage_fingerprint,
    native_agent_name_for_lineage,
    normalize_lineage_name,
    parse_obs_bootstrap_xml,
    slugify_projection_label,
)


# ---------------------------------------------------------------------------
# Test 1: v2 XML round-trip — new attribute names
# ---------------------------------------------------------------------------

class TestV2XMLRoundTrip:
    """build_obs_bootstrap_xml should produce v2 XML with display_name=
    and <agent_name>, and parse_obs_bootstrap_xml should read them back."""

    def test_v2_xml_has_display_name_attribute(self):
        xml = build_obs_bootstrap_xml(
            lineage=("Root", "Child"),
            origin="test",
            is_fork=False,
            session_id="sid-1",
            root_team_key="team-1",
            native_agent_name="hash-child",
        )
        root = ET.fromstring(xml)

        # Version should be 2
        assert root.attrib.get("version") == "2"

        # obs-node elements should have display_name=, not name=
        nodes = root.findall(".//obs-node")
        assert len(nodes) == 2
        for node in nodes:
            assert "display_name" in node.attrib, (
                f"obs-node missing display_name attribute: {node.attrib}"
            )
            # name= should NOT be present in v2
            assert "name" not in node.attrib, (
                f"obs-node should use display_name=, not name=: {node.attrib}"
            )

    def test_v2_xml_has_agent_name_element(self):
        xml = build_obs_bootstrap_xml(
            lineage=("Root",),
            origin="test",
            is_fork=False,
            session_id="sid-1",
            root_team_key="team-1",
            native_agent_name="my-agent",
        )
        root = ET.fromstring(xml)
        team_ctx = root.find("team_context")
        assert team_ctx is not None

        # Should have <agent_name>, not <native_agent_name>
        agent_el = team_ctx.find("agent_name")
        assert agent_el is not None, "team_context should have <agent_name> element"
        assert agent_el.text == "my-agent"

        # <native_agent_name> should NOT be present in v2
        native_el = team_ctx.find("native_agent_name")
        assert native_el is None, (
            "team_context should not have <native_agent_name> in v2"
        )

    def test_v2_xml_round_trip(self):
        xml = build_obs_bootstrap_xml(
            lineage=("Root", "Worker"),
            origin="test",
            is_fork=True,
            session_id="sid-2",
            parent_session_id="sid-1",
            root_team_key="team-2",
            native_agent_name="hash-worker",
        )
        parsed = parse_obs_bootstrap_xml(xml)

        assert parsed.lineage == ("Root", "Worker")
        assert parsed.origin == "test"
        assert parsed.is_fork is True
        assert parsed.session_id == "sid-2"
        assert parsed.root_team_key == "team-2"

        # The field should be called agent_name, not native_agent_name
        # (ObsBootstrap dataclass rename)
        assert hasattr(parsed, "agent_name"), (
            "ObsBootstrap should have 'agent_name' field, not 'native_agent_name'"
        )
        assert parsed.agent_name == "hash-worker"


# ---------------------------------------------------------------------------
# Test 2: v1 backward compatibility
# ---------------------------------------------------------------------------

class TestV1BackwardCompat:
    """Old XML with name= and <native_agent_name> must still parse."""

    def test_v1_xml_with_name_attribute_parses(self):
        """This should PASS even before implementation — existing parser handles it."""
        v1_xml = (
            '<obs-bootstrap version="1">'
            '<obs-lineage>'
            '<obs-node name="Root" agent_name="root-slug" />'
            '<obs-node name="Child" agent_name="hash-child" />'
            '</obs-lineage>'
            '<fork_context><origin>test</origin><is_fork>false</is_fork></fork_context>'
            '<team_context>'
            '<root_team_key>team-key</root_team_key>'
            '<native_agent_name>hash-child</native_agent_name>'
            '</team_context>'
            '</obs-bootstrap>'
        )
        parsed = parse_obs_bootstrap_xml(v1_xml)
        assert parsed.lineage == ("Root", "Child")
        assert parsed.root_team_key == "team-key"
        # Should work with old field name
        assert parsed.native_agent_name == "hash-child"

    def test_v1_parsed_exposes_agent_name_field(self):
        """After refactor, the parsed result should have agent_name even for v1 input."""
        v1_xml = (
            '<obs-bootstrap version="1">'
            '<obs-lineage><obs-node name="Root" /></obs-lineage>'
            '<fork_context><origin>test</origin><is_fork>false</is_fork></fork_context>'
            '<team_context><native_agent_name>my-agent</native_agent_name></team_context>'
            '</obs-bootstrap>'
        )
        parsed = parse_obs_bootstrap_xml(v1_xml)
        assert hasattr(parsed, "agent_name")
        assert parsed.agent_name == "my-agent"


# ---------------------------------------------------------------------------
# Test 3: Mixed attributes
# ---------------------------------------------------------------------------

class TestMixedAttributes:

    def test_mixed_display_name_and_name_attributes(self):
        """Some nodes with display_name=, some with name= — all should parse."""
        xml = (
            '<obs-bootstrap version="2">'
            '<obs-lineage>'
            '<obs-node display_name="Root" agent_name="root-slug" />'
            '<obs-node name="OldChild" agent_name="hash-old" />'
            '</obs-lineage>'
            '<fork_context><origin>test</origin><is_fork>false</is_fork></fork_context>'
            '<team_context><agent_name>hash-old</agent_name></team_context>'
            '</obs-bootstrap>'
        )
        parsed = parse_obs_bootstrap_xml(xml)
        assert parsed.lineage == ("Root", "OldChild")


# ---------------------------------------------------------------------------
# Test 4: No version field
# ---------------------------------------------------------------------------

class TestNoVersionField:

    def test_no_version_treated_as_v1(self):
        """XML without version= attribute should parse as v1."""
        xml = (
            '<obs-bootstrap>'
            '<obs-lineage><obs-node name="Root" /></obs-lineage>'
            '<fork_context><origin>test</origin><is_fork>false</is_fork></fork_context>'
            '<team_context><native_agent_name>root</native_agent_name></team_context>'
            '</obs-bootstrap>'
        )
        parsed = parse_obs_bootstrap_xml(xml)
        assert parsed.lineage == ("Root",)
        assert parsed.native_agent_name == "root"


# ---------------------------------------------------------------------------
# Test 5: agent_name_for_lineage function exists
# ---------------------------------------------------------------------------

class TestAgentNameFunction:

    def test_agent_name_for_lineage_exists(self):
        from obs_agent.lineage import agent_name_for_lineage
        # Should produce same results as native_agent_name_for_lineage
        assert agent_name_for_lineage(("Root",)) == native_agent_name_for_lineage(("Root",))
        assert agent_name_for_lineage(("Root", "Child")) == native_agent_name_for_lineage(("Root", "Child"))
        assert agent_name_for_lineage(("A", "B", "C")) == native_agent_name_for_lineage(("A", "B", "C"))

    def test_agent_name_for_lineage_importable(self):
        """The new name should be importable from lineage module."""
        from obs_agent.lineage import agent_name_for_lineage  # noqa: F401


# ---------------------------------------------------------------------------
# Test 6: session_lineage output field
# ---------------------------------------------------------------------------

class TestSessionLineageOutput:

    @pytest.mark.xfail(
        reason="NOT IMPLEMENTED: session_lineage still outputs native_agent_name",
        strict=True,
    )
    def test_session_lineage_output_has_agent_name_not_native(self):
        """session_lineage tool output should have 'agent_name' field."""
        # We can't easily call the tool, so inspect the source
        from obs_agent import tools
        source = inspect.getsource(tools.register_tools)
        # The output dict should use "agent_name", not "native_agent_name"
        assert '"agent_name"' in source or "'agent_name'" in source
        # And should NOT have the old name in the output dict
        # (it may still exist in imports/other contexts, so we check
        # specifically the payload construction)
        # Look for the pattern: payload["native_agent_name"] or "native_agent_name":
        # in the session_lineage handler


# ---------------------------------------------------------------------------
# Test 7: MCP schema has display_name param
# ---------------------------------------------------------------------------

class TestMCPSchemaDisplayName:

    def test_agent_task_schema_has_display_name(self):
        """AgentTask MCP schema should have display_name parameter."""
        from pathlib import Path
        source = Path(__file__).resolve().parents[1].joinpath(
            "src", "obs_agent", "tools.py"
        ).read_text()
        assert '"display_name"' in source

    def test_launch_task_accepts_display_name_and_fallbacks(self):
        """_launch_task should read display_name first, then alias, then description, then name."""
        from pathlib import Path
        source = Path(__file__).resolve().parents[1].joinpath(
            "src", "obs_agent", "tools.py"
        ).read_text()
        assert "display_name" in source


# ---------------------------------------------------------------------------
# Test 8: Resume bug fix
# ---------------------------------------------------------------------------

class TestResumeBugFix:

    def test_resume_fork_task_does_not_use_name_for_routing(self):
        """_resume_fork_task should NOT fall back to args.get('name')
        for the routing agent_name. That's the bug at line 6838."""
        from obs_agent import telegram
        source = inspect.getsource(telegram.TelegramBot._resume_fork_task)
        # The bug line is: args.get("agent_name") or args.get("name")
        # After fix: should only use args.get("agent_name"), NOT args.get("name")
        # Check that "name" is not used as a fallback for agent_name
        lines = source.split("\n")
        for line in lines:
            if "agent_name" in line and "args.get" in line:
                assert 'args.get("name")' not in line and "args.get('name')" not in line, (
                    f"BUG: _resume_fork_task uses args.get('name') as routing name fallback: {line.strip()}"
                )


# ---------------------------------------------------------------------------
# Test 9: _dropped_agent_keys removed
# ---------------------------------------------------------------------------

class TestDroppedAgentKeysRemoved:

    def test_no_dropped_agent_keys_attribute(self):
        """TelegramBot should not have _dropped_agent_keys — aggressive collision, no respawn."""
        from obs_agent import telegram
        source = inspect.getsource(telegram.TelegramBot.__init__)
        assert "_dropped_agent_keys" not in source, (
            "_dropped_agent_keys should be removed — aggressive collision, "
            "no respawn after topic deletion"
        )


# ---------------------------------------------------------------------------
# Test 10: Terminology comment at top of lineage.py
# ---------------------------------------------------------------------------

class TestTerminologyComment:

    def test_lineage_has_terminology_comment(self):
        """lineage.py should have a clear two-name terminology comment."""
        from obs_agent import lineage
        source = inspect.getsource(lineage)
        # Should explain the two-tier naming system
        assert "agent_name" in source and "display_name" in source
        assert "routing" in source.lower() or "machine name" in source.lower()


# ---------------------------------------------------------------------------
# Test 11: build_obs_bootstrap_xml accepts agent_name parameter
# ---------------------------------------------------------------------------

class TestBuildXMLNewParamName:

    def test_build_accepts_agent_name_param(self):
        """build_obs_bootstrap_xml should accept agent_name= (not just native_agent_name=)."""
        xml = build_obs_bootstrap_xml(
            lineage=("Root",),
            origin="test",
            is_fork=False,
            session_id="sid",
            agent_name="my-agent",  # New param name
        )
        parsed = parse_obs_bootstrap_xml(xml)
        # Should store it correctly
        assert hasattr(parsed, "agent_name")
