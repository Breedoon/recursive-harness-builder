"""Eval judge: MCP-tool-equipped agent that runs eval scenarios.

Creates an SDK agent with two MCP tools (send_message, read_output)
that wrap a Platform instance. The agent follows scenario steps,
interacts with the real CLI, and renders a PASS/FAIL verdict.

For sequential scenarios, the judge drives interaction via MCP tools.
For timing-sensitive scenarios (send_nowait, sleep), the harness drives
pexpect directly and the judge only evaluates the captured transcript.

MCP tools verified working in SDK v0.1.35 via spike_mcp_client.py:
- @tool("name", "desc", {params}) + create_sdk_mcp_server("name", tools=[...])
- mcp_servers={"name": server} dict format
- Agent receives tools as mcp__name__toolname
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from tests.evals.platform import Platform
from tests.evals.scenario import EvalScenario, EvalStep

_DEFAULT_CONTINUATION_TIMEOUTS = [60, 30]
_REQUIRED_JUDGMENT_SECTIONS = [
    "CRITERIA CHECK",
    "INTENT CHECK",
    "NOTES",
    "VERDICT",
]


@dataclass
class EvalResult:
    """Result of an eval scenario run."""

    scenario: str
    passed: bool
    judgment: str


def _build_mcp_tools(platform: Platform) -> dict:
    """Create an MCP server with send_message and read_output tools."""

    @tool(
        "send_message",
        "Send a message to the OBS Agent CLI and wait for the response",
        {"message": str},
    )
    async def send_message(args: dict) -> dict:
        output = await platform.send(args["message"])
        return {"content": [{"type": "text", "text": output}]}

    @tool(
        "read_output",
        "Read the most recent output from the OBS Agent CLI without sending anything",
        {},
    )
    async def read_output(args: dict) -> dict:
        output = await platform.read()
        return {"content": [{"type": "text", "text": output}]}

    return create_sdk_mcp_server("eval", tools=[send_message, read_output])


def _is_sequential(scenario: EvalScenario) -> bool:
    """Check if all steps are sequential (send only, no send_nowait/sleep)."""
    return all(step.action == "send" for step in scenario.steps)


def _build_judge_prompt_sequential(scenario: EvalScenario) -> str:
    """Build prompt for sequential scenarios (judge drives interaction)."""
    steps_text = ""
    for i, step in enumerate(scenario.steps, 1):
        steps_text += f'{i}. Send this message: "{step.message}"\n'
        steps_text += f"   (Wait up to {step.wait_seconds}s for response)\n"

    criteria_text = "\n".join(f"- {c}" for c in scenario.criteria)
    intent_text = (
        "\n".join(f"- {item}" for item in scenario.intent)
        if scenario.intent
        else "- No extra intent notes provided."
    )
    explore_raw = scenario.metadata.get("exploration_messages", "0")
    try:
        exploration_budget = max(0, int(explore_raw))
    except ValueError:
        exploration_budget = 0
    exploration_text = ""
    if exploration_budget > 0:
        exploration_text = (
            f"\nAfter completing the required steps, you MAY send up to {exploration_budget} additional "
            "probing messages to try to falsify the behavior. Use this budget to stress suspicious "
            "patterns (e.g., impossible token math, inconsistent context trends, or contradictory tool outputs)."
        )

    return f"""You are evaluating the OBS Agent by running a test scenario.

## Scenario: {scenario.name}

## Instructions
Use the send_message tool to interact with the agent. Follow these steps IN ORDER:

{steps_text}
{exploration_text}

## Intent Context
{intent_text}

After completing all steps, evaluate the agent's responses against these criteria:

## Pass Criteria
{criteria_text}

## Verdict
After evaluating, you MUST output exactly these headings in this order:
CRITERIA CHECK:
- One bullet per pass criterion, each marked PASS or FAIL with a short reason.
INTENT CHECK:
- State whether behavior matched scenario intent beyond literal criteria.
NOTES:
- suspicious behavior, unclear evidence, odd output, or quality concerns even if you still pass.
- If there are no concerns, write exactly: NOTES: none
VERDICT:
- Write exactly one of: PASS or FAIL

If ALL criteria are met: VERDICT must be PASS.
If ANY criterion is not met: VERDICT must be FAIL.

Be strict and adversarial: try to find failure modes, not just check boxes.
If output is technically passing but behavior seems off for the intent, call it out in NOTES.
"""


def _is_timeout_output(output: str) -> bool:
    """Detect explicit timeout markers returned by platform adapters."""
    return output.strip().lower().startswith("(timeout:")


def _continuation_timeouts(scenario: EvalScenario) -> list[int]:
    """Continuation prompt timeouts for concurrent scenarios."""
    if scenario.continuation_timeouts:
        return scenario.continuation_timeouts
    return list(_DEFAULT_CONTINUATION_TIMEOUTS)


def _build_judge_prompt_transcript(
    scenario: EvalScenario, transcript: str
) -> str:
    """Build prompt for transcript-based judging (harness already drove interaction)."""
    criteria_text = "\n".join(f"- {c}" for c in scenario.criteria)
    intent_text = (
        "\n".join(f"- {item}" for item in scenario.intent)
        if scenario.intent
        else "- No extra intent notes provided."
    )

    return f"""You are evaluating an OBS Agent CLI interaction transcript.

## Scenario: {scenario.name}

## Transcript of the interaction:
```
{transcript}
```

## Pass Criteria
{criteria_text}

## Intent Context
{intent_text}

## Instructions
Read the transcript carefully. Evaluate whether each criterion is met based on
what actually happened in the interaction.

You MUST output exactly these headings in this order:
CRITERIA CHECK:
- One bullet per pass criterion, each marked PASS or FAIL with a short reason.
INTENT CHECK:
- State whether behavior matched scenario intent beyond literal criteria.
NOTES:
- suspicious behavior, unclear evidence, odd output, or quality concerns even if you still pass.
- If there are no concerns, write exactly: NOTES: none
VERDICT:
- Write exactly one of: PASS or FAIL

If ALL criteria are met: VERDICT must be PASS.
If ANY criterion is not met: VERDICT must be FAIL.

Be strict: if output is technically passing but behavior seems off for the intent, call it out in NOTES.
"""


async def _drive_concurrent_steps(
    scenario: EvalScenario, platform: Platform
) -> str:
    """Drive timing-sensitive steps directly via pexpect, return transcript."""
    transcript_parts: list[str] = []

    for step in scenario.steps:
        if step.action == "send":
            transcript_parts.append(f"USER: {step.message}")
            output = await platform.send(step.message)
            transcript_parts.append(f"AGENT: {output}")
        elif step.action == "send_nowait":
            transcript_parts.append(f"USER (nowait): {step.message}")
            await platform.send_nowait(step.message)
        elif step.action == "sleep":
            transcript_parts.append(f"[sleep {step.wait_seconds}s]")
            await asyncio.sleep(step.wait_seconds)

    # After all steps, collect continuation outputs with bounded per-attempt
    # timeouts. This avoids long idle stalls when no more output is coming.
    for attempt, timeout in enumerate(_continuation_timeouts(scenario)):
        try:
            output = await platform.wait_for_prompt(timeout=timeout)
            if _is_timeout_output(output):
                break
            if output:
                label = "AGENT (final)" if attempt == 0 else f"AGENT (continuation {attempt})"
                transcript_parts.append(f"{label}: {output}")
        except Exception:
            # Timeout is expected — no more prompts coming
            break

    return "\n\n".join(transcript_parts)


async def _run_sdk_judge(prompt: str, use_mcp: bool = False, platform: Platform | None = None) -> str:
    """Run the SDK judge agent and collect its verdict text."""
    if use_mcp:
        system_prompt = (
            "You are a test evaluator. Follow the scenario steps exactly, "
            "using the send_message tool for each step. After all steps, "
            "analyze the responses and output the required sections exactly: "
            "CRITERIA CHECK, INTENT CHECK, NOTES, VERDICT. "
            "Be thorough but concise in your analysis."
        )
    else:
        system_prompt = (
            "You are a test evaluator. Read the provided transcript and "
            "evaluate it against the criteria. Output the required sections "
            "exactly: CRITERIA CHECK, INTENT CHECK, NOTES, VERDICT. "
            "Be thorough but concise in your analysis."
        )

    options_kwargs: dict = {
        "permission_mode": "bypassPermissions",
        "max_turns": 30,
        "system_prompt": system_prompt,
    }

    if use_mcp and platform is not None:
        mcp_server = _build_mcp_tools(platform)
        options_kwargs["mcp_servers"] = {"eval": mcp_server}

    options = ClaudeAgentOptions(**options_kwargs)

    judgment_parts: list[str] = []
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if hasattr(message, "content") and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        judgment_parts.append(block.text)

    return "\n".join(judgment_parts)


def _has_section(judgment: str, section: str) -> bool:
    return bool(re.search(rf"(?im)^\s*{re.escape(section)}\s*:", judgment))


def _extract_verdict(judgment: str) -> str | None:
    match = re.search(r"(?im)^\s*VERDICT\s*:\s*(PASS|FAIL)\b", judgment)
    if not match:
        return None
    return match.group(1).upper()


def _enforce_judgment_structure(judgment: str) -> tuple[bool, str]:
    """Validate required judgment sections and verdict format.

    Returns (is_valid, normalized_judgment_with_harness_notes_if_any).
    """
    # The judge sometimes wraps section headers in markdown bold.
    # Normalize for parsing so we accept either plain or bold headings.
    parsing_text = judgment.replace("**", "")

    issues: list[str] = []
    for section in _REQUIRED_JUDGMENT_SECTIONS:
        if not _has_section(parsing_text, section):
            issues.append(f"missing required section: {section}:")

    verdict = _extract_verdict(parsing_text)
    if verdict is None:
        issues.append("missing parseable verdict line (expected 'VERDICT: PASS|FAIL')")

    for section in ("CRITERIA CHECK", "INTENT CHECK", "NOTES"):
        if not _has_section(parsing_text, section):
            continue
        block = re.search(
            rf"(?is)^\s*{re.escape(section)}\s*:(.*?)(?=^\s*(?:CRITERIA CHECK|INTENT CHECK|NOTES|VERDICT)\s*:|\Z)",
            parsing_text,
            re.MULTILINE,
        )
        if block and not block.group(1).strip():
            issues.append(f"empty section: {section}:")

    if not issues:
        return True, judgment

    harness_notes = ["HARNESS FORMAT CHECK:", *[f"- {i}" for i in issues]]
    normalized = judgment.rstrip() + "\n\n" + "\n".join(harness_notes)
    return False, normalized


async def run_judge(scenario: EvalScenario, platform: Platform) -> EvalResult:
    """Run the judge agent against a scenario using the given platform.

    For sequential scenarios: judge drives CLI via MCP tools.
    For concurrent scenarios: harness drives CLI directly, judge evaluates transcript.
    """
    if _is_sequential(scenario):
        # Judge drives interaction via MCP tools
        prompt = _build_judge_prompt_sequential(scenario)
        judgment = await _run_sdk_judge(prompt, use_mcp=True, platform=platform)
    else:
        # Harness drives interaction, judge evaluates transcript
        transcript = await _drive_concurrent_steps(scenario, platform)
        prompt = _build_judge_prompt_transcript(scenario, transcript)
        judgment = await _run_sdk_judge(prompt, use_mcp=False)

    is_structured, judgment_with_checks = _enforce_judgment_structure(judgment)
    verdict = _extract_verdict(judgment.replace("**", ""))
    passed = bool(is_structured and verdict == "PASS")

    return EvalResult(
        scenario=scenario.name,
        passed=passed,
        judgment=judgment_with_checks,
    )
