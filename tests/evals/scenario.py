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
    lane: str | None = None
    profiles: list[str] = field(default_factory=list)
    first_message_timeout: float | None = None
    done_timeout: float | None = None
    idle_quiescence_timeout: float | None = None
    response_timeout: float | None = None
    continuation_timeouts: list[int] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse optional simple YAML-like frontmatter.

    Supported format at top of file:

    ---
    key: value
    key2: value2
    ---
    """
    if not lines or lines[0].strip() != "---":
        return {}, lines

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, lines

    raw_meta = lines[1:end]
    content = lines[end + 1 :]
    metadata: dict[str, str] = {}
    for line in raw_meta:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        metadata[key.strip().lower()] = value.strip()
    return metadata, content


def _parse_csv_list(value: str) -> list[str]:
    """Parse simple comma-separated list syntax."""
    cleaned = value.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return [item.strip().strip("'\"") for item in cleaned.split(",") if item.strip()]


def _parse_float(metadata: dict[str, str], key: str) -> float | None:
    value = metadata.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_int_list(metadata: dict[str, str], key: str) -> list[int]:
    value = metadata.get(key)
    if not value:
        return []
    out: list[int] = []
    for item in _parse_csv_list(value):
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out


def parse_scenario(path: Path) -> EvalScenario:
    """Parse a scenario markdown file into an EvalScenario."""
    text = path.read_text()
    raw_lines = text.splitlines()
    metadata, lines = _parse_frontmatter(raw_lines)

    # For parsing body sections, trim trailing/leading empty lines only.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

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

    lane = metadata.get("lane")
    if lane:
        lane = lane.lower()
    profiles = _parse_csv_list(metadata.get("profiles", ""))
    continuation_timeouts = _parse_int_list(metadata, "continuation_timeouts")
    first_message_timeout = _parse_float(metadata, "first_message_timeout")
    done_timeout = _parse_float(metadata, "done_timeout")
    idle_quiescence_timeout = _parse_float(metadata, "idle_quiescence_timeout")
    response_timeout = _parse_float(metadata, "response_timeout")

    return EvalScenario(
        name=name,
        intent=intent,
        steps=steps,
        criteria=criteria,
        lane=lane,
        profiles=profiles,
        first_message_timeout=first_message_timeout,
        done_timeout=done_timeout,
        idle_quiescence_timeout=idle_quiescence_timeout,
        response_timeout=response_timeout,
        continuation_timeouts=continuation_timeouts,
        metadata=metadata,
    )
