from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from obs_agent.lineage import (
    agent_name_for_lineage,
    build_obs_bootstrap_xml,
    extract_obs_bootstrap_xml,
    find_latest_obs_bootstrap_in_jsonl,
    parse_obs_bootstrap_xml,
    root_team_key_for_lineage,
)


def test_bootstrap_round_trip_and_projection(tmp_path):
    lineage = ("Root Topic", "Child Topic", "worker-a")
    xml = build_obs_bootstrap_xml(
        lineage=lineage,
        origin="agent_task_fork",
        is_fork=True,
        session_id="sid-child",
        agent_id="task-123",
        parent_session_id="sid-parent",
        root_team_key=root_team_key_for_lineage(lineage),
        agent_name=agent_name_for_lineage(lineage),
        parent_agent_name=agent_name_for_lineage(lineage[:-1], team_key=root_team_key_for_lineage(lineage)),
        parent_display_name="Child Topic",
    )

    parsed = parse_obs_bootstrap_xml(xml)

    assert parsed.lineage == lineage
    assert parsed.origin == "agent_task_fork"
    assert parsed.is_fork is True
    assert parsed.session_id == "sid-child"
    assert parsed.agent_id == "task-123"
    assert parsed.parent_session_id == "sid-parent"
    assert parsed.root_team_key == root_team_key_for_lineage(lineage)
    assert parsed.agent_name == agent_name_for_lineage(lineage)
    assert parsed.parent_agent_name == agent_name_for_lineage(
        lineage[:-1],
        team_key=root_team_key_for_lineage(lineage),
    )
    assert parsed.parent_display_name == "Child Topic"


def test_latest_bootstrap_wins_when_scanning_jsonl(tmp_path):
    path = tmp_path / "session.jsonl"
    first = build_obs_bootstrap_xml(
        lineage=("Root",),
        origin="trunk_start",
        is_fork=False,
        session_id="sid-1",
        root_team_key="team-root",
        agent_name="agent-root",
    )
    second = build_obs_bootstrap_xml(
        lineage=("Root", "Forked"),
        origin="user_fork",
        is_fork=True,
        session_id="sid-2",
        parent_session_id="sid-1",
        root_team_key="team-root",
        agent_name="agent-forked",
    )
    lines = [
        json.dumps(
            {
                "type": "queue-operation",
                "content": f"{first}\n\nReply with only OK",
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": f"{second}\n\nReply with only FORKED",
                },
            }
        ),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    parsed = find_latest_obs_bootstrap_in_jsonl(path)

    assert parsed is not None
    assert parsed.lineage == ("Root", "Forked")
    assert parsed.origin == "user_fork"
    assert parsed.session_id == "sid-2"


def test_descendant_bootstrap_keeps_parent_node_agent_names() -> None:
    lineage = ("Root Topic", "Child Topic", "worker-a")
    team_key = root_team_key_for_lineage(lineage, timestamp=1710561600.0)
    xml = build_obs_bootstrap_xml(
        lineage=lineage,
        origin="agent_task_fork",
        is_fork=True,
        session_id="sid-child",
        root_team_key=team_key,
        agent_name=agent_name_for_lineage(lineage),
    )

    root = ET.fromstring(xml)
    nodes = root.findall(".//obs-node")
    assert [node.attrib["agent_name"] for node in nodes] == [
        team_key,
        agent_name_for_lineage(lineage[:-1]),
        agent_name_for_lineage(lineage),
    ]


def test_extract_bootstrap_with_system_note_prefix():
    """Regression: bootstrap prefixed by <system-note> must still be found.

    In production, telegram.py prepends <system-note>...</system-note> before
    the bootstrap XML. The old anchored regex missed this.
    """
    xml = build_obs_bootstrap_xml(
        lineage=("test",),
        origin="new_trunk",
        is_fork=False,
        session_id="sid-trunk",
        root_team_key="2026-03-18-02-20-test",
        agent_name="2026-03-18-02-20-test",
    )
    # Simulate what telegram.py:4970-4987 produces
    text = (
        "<system-note>\n"
        "Time: 2026-03-17 23:45:07 EDT\n"
        "Context: first turn\n"
        "</system-note>\n\n"
        f"{xml}\n\n"
        "Hello from user"
    )
    extracted = extract_obs_bootstrap_xml(text)
    assert extracted is not None
    parsed = parse_obs_bootstrap_xml(extracted)
    assert parsed.lineage == ("test",)
    assert parsed.origin == "new_trunk"
    assert parsed.session_id == "sid-trunk"


def test_find_bootstrap_in_jsonl_with_system_note_prefix(tmp_path):
    """Regression: JSONL scanning must find bootstrap after <system-note> prefix."""
    path = tmp_path / "session.jsonl"
    xml = build_obs_bootstrap_xml(
        lineage=("test",),
        origin="new_trunk",
        is_fork=False,
        session_id="sid-trunk",
        root_team_key="2026-03-18-02-20-test",
        agent_name="2026-03-18-02-20-test",
    )
    prefixed_content = (
        "<system-note>\n"
        "Time: 2026-03-17 23:45:07 EDT\n"
        "Context: first turn\n"
        "</system-note>\n\n"
        f"{xml}\n\n"
        "Hello"
    )
    lines = [
        json.dumps({"type": "queue-operation", "content": prefixed_content}),
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prefixed_content},
        }),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    parsed = find_latest_obs_bootstrap_in_jsonl(path)
    assert parsed is not None
    assert parsed.lineage == ("test",)
    assert parsed.session_id == "sid-trunk"
