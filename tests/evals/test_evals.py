"""Parametrized eval tests.

Each .md file in tests/evals/scenarios/ is a scenario. The judge agent
drives the real CLI via pexpect and evaluates responses against criteria.

Run with: .venv/bin/pytest tests/evals/ -m eval -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.judge import run_judge
from tests.evals.platform import CLIPlatform
from tests.evals.scenario import parse_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def scenario_ids() -> list[str]:
    """Discover scenario files and return their stem names."""
    if not SCENARIO_DIR.is_dir():
        return []
    return [p.stem for p in sorted(SCENARIO_DIR.glob("*.md"))]


@pytest.mark.eval
@pytest.mark.parametrize("scenario_name", scenario_ids() or ["_no_scenarios_found"])
async def test_eval(scenario_name: str, eval_vault: Path, eval_config) -> None:
    """Run a single eval scenario through the judge agent."""
    if scenario_name == "_no_scenarios_found":
        pytest.skip("No scenario files found in tests/evals/scenarios/")

    scenario_path = SCENARIO_DIR / f"{scenario_name}.md"
    scenario = parse_scenario(scenario_path)

    platform = CLIPlatform(
        vault_path=eval_vault,
        daemon_port=eval_config.daemon_port,
    )
    try:
        result = await run_judge(scenario, platform)
    finally:
        await platform.close()

    assert result.passed, f"EVAL FAILED: {scenario_name}\n\n{result.judgment}"
