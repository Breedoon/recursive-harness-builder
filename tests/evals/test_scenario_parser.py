"""Tests for eval scenario parser metadata support."""

from pathlib import Path

from tests.evals.scenario import parse_scenario


def test_parse_scenario_with_frontmatter(tmp_path: Path):
    scenario_file = tmp_path / "frontmatter.md"
    scenario_file.write_text(
        """---
lane: judge
profiles: smoke, full
first_message_timeout: 45
done_timeout: 70
idle_quiescence_timeout: 12.5
continuation_timeouts: 20,10
response_timeout: 240
---
# Frontmatter Scenario

## Steps
1. Send: \"hello\"
   Wait: 30

## Criteria
- responds
"""
    )

    scenario = parse_scenario(scenario_file)

    assert scenario.name == "Frontmatter Scenario"
    assert scenario.lane == "judge"
    assert scenario.profiles == ["smoke", "full"]
    assert scenario.first_message_timeout == 45.0
    assert scenario.done_timeout == 70.0
    assert scenario.idle_quiescence_timeout == 12.5
    assert scenario.response_timeout == 240.0
    assert scenario.continuation_timeouts == [20, 10]
    assert len(scenario.steps) == 1
    assert scenario.steps[0].message == "hello"


def test_parse_scenario_without_frontmatter(tmp_path: Path):
    scenario_file = tmp_path / "plain.md"
    scenario_file.write_text(
        """# Plain Scenario

## Steps
1. SendNowait: \"hello\"
2. Sleep: 5

## Criteria
- works
"""
    )

    scenario = parse_scenario(scenario_file)

    assert scenario.name == "Plain Scenario"
    assert scenario.lane is None
    assert scenario.profiles == []
    assert scenario.first_message_timeout is None
    assert scenario.done_timeout is None
    assert scenario.idle_quiescence_timeout is None
    assert scenario.response_timeout is None
    assert scenario.continuation_timeouts == []
    assert [step.action for step in scenario.steps] == ["send_nowait", "sleep"]
