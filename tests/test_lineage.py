from __future__ import annotations

import json

from obs_agent.lineage import (
    build_obs_bootstrap_xml,
    find_latest_obs_bootstrap_in_jsonl,
    native_agent_name_for_lineage,
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
        native_agent_name=native_agent_name_for_lineage(lineage),
    )

    parsed = parse_obs_bootstrap_xml(xml)

    assert parsed.lineage == lineage
    assert parsed.origin == "agent_task_fork"
    assert parsed.is_fork is True
    assert parsed.session_id == "sid-child"
    assert parsed.agent_id == "task-123"
    assert parsed.parent_session_id == "sid-parent"
    assert parsed.root_team_key == root_team_key_for_lineage(lineage)
    assert parsed.native_agent_name == native_agent_name_for_lineage(lineage)


def test_latest_bootstrap_wins_when_scanning_jsonl(tmp_path):
    path = tmp_path / "session.jsonl"
    first = build_obs_bootstrap_xml(
        lineage=("Root",),
        origin="trunk_start",
        is_fork=False,
        session_id="sid-1",
        root_team_key="team-root",
        native_agent_name="agent-root",
    )
    second = build_obs_bootstrap_xml(
        lineage=("Root", "Forked"),
        origin="user_fork",
        is_fork=True,
        session_id="sid-2",
        parent_session_id="sid-1",
        root_team_key="team-root",
        native_agent_name="agent-forked",
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

