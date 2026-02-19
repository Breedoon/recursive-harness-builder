"""Eval scenario parser.

Parses markdown scenario files into structured EvalScenario objects.

Step types:
    Send: "message"      — send and wait for response (sequential)
    SendNowait: "message" — send without waiting (for concurrent tests)
    Sleep: N             — pause N seconds (for timing-sensitive tests)

Format:
    # Scenario Name

    ## Intent
    - What the scenario is trying to validate beyond literal criteria
    - What should be flagged as suspicious even if criteria pass

    ## Steps
    1. Send: "message here"
       Wait: 60
    2. SendNowait: "fire and forget"
    3. Sleep: 5
    4. Send: "follow up"
       Wait: 120

    ## Criteria
    - criterion one
    - criterion two
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EvalStep:
    """A single step in an eval scenario."""

    action: str  # "send", "send_nowait", "sleep"
    message: str = ""
    wait_seconds: int = 60


@dataclass
class EvalScenario:
    """Parsed eval scenario."""

    name: str
    intent: list[str] = field(default_factory=list)
    steps: list[EvalStep] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)


def parse_scenario(path: Path) -> EvalScenario:
    """Parse a scenario markdown file into an EvalScenario."""
    text = path.read_text()
    lines = text.strip().splitlines()

    # Extract name from first heading
    name = path.stem
    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            name = line[2:].strip()
            break

    steps: list[EvalStep] = []
    criteria: list[str] = []
    intent: list[str] = []

    section: str | None = None
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if "step" in heading:
                section = "steps"
            elif "criter" in heading:
                section = "criteria"
            elif "intent" in heading:
                section = "intent"
            else:
                section = None
            continue

        if section == "steps":
            # Send: "message" (sequential — wait for response)
            send_match = re.match(r'^\d+\.\s+Send:\s*"(.+)"', stripped)
            if send_match:
                steps.append(EvalStep(action="send", message=send_match.group(1)))
                continue

            # SendNowait: "message" (fire and forget)
            nowait_match = re.match(r'^\d+\.\s+SendNowait:\s*"(.+)"', stripped)
            if nowait_match:
                steps.append(EvalStep(action="send_nowait", message=nowait_match.group(1)))
                continue

            # Sleep: N
            sleep_match = re.match(r'^\d+\.\s+Sleep:\s*(\d+)', stripped)
            if sleep_match:
                steps.append(EvalStep(action="sleep", wait_seconds=int(sleep_match.group(1))))
                continue

            # Wait: N on a step (modifies previous step)
            wait_match = re.match(r'^Wait:\s*(\d+)', stripped)
            if wait_match and steps:
                steps[-1].wait_seconds = int(wait_match.group(1))

        elif section == "criteria":
            # Match lines like: - criterion text
            crit_match = re.match(r'^[-*]\s+(.+)', stripped)
            if crit_match:
                criteria.append(crit_match.group(1).strip())
        elif section == "intent":
            intent_match = re.match(r'^[-*]\s+(.+)', stripped)
            if intent_match:
                intent.append(intent_match.group(1).strip())

    return EvalScenario(name=name, intent=intent, steps=steps, criteria=criteria)
