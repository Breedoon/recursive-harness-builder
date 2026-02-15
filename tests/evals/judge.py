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

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from tests.evals.platform import Platform
from tests.evals.scenario import EvalScenario, EvalStep


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

    return f"""You are evaluating the OBS Agent by running a test scenario.

## Scenario: {scenario.name}

## Instructions
Use the send_message tool to interact with the agent. Follow these steps IN ORDER:

{steps_text}

After completing all steps, evaluate the agent's responses against these criteria:

## Pass Criteria
{criteria_text}

## Verdict
After evaluating, output your analysis and then a final verdict line:
- If ALL criteria are met: VERDICT: PASS
- If ANY criterion is not met: VERDICT: FAIL

Include a brief explanation of why each criterion passed or failed.
"""


def _build_judge_prompt_transcript(
    scenario: EvalScenario, transcript: str
) -> str:
    """Build prompt for transcript-based judging (harness already drove interaction)."""
    criteria_text = "\n".join(f"- {c}" for c in scenario.criteria)

    return f"""You are evaluating an OBS Agent CLI interaction transcript.

## Scenario: {scenario.name}

## Transcript of the interaction:
```
{transcript}
```

## Pass Criteria
{criteria_text}

## Instructions
Read the transcript carefully. Evaluate whether each criterion is met based on
what actually happened in the interaction. Output your analysis and then a final
verdict line:
- If ALL criteria are met: VERDICT: PASS
- If ANY criterion is not met: VERDICT: FAIL

Include a brief explanation of why each criterion passed or failed.
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

    # After all steps, wait for any remaining output
    try:
        final_output = await platform.wait_for_prompt(timeout=120)
        if final_output:
            transcript_parts.append(f"AGENT (final): {final_output}")
    except Exception:
        # Timeout waiting for prompt is OK for interrupt scenarios
        pass

    return "\n\n".join(transcript_parts)


async def _run_sdk_judge(prompt: str, use_mcp: bool = False, platform: Platform | None = None) -> str:
    """Run the SDK judge agent and collect its verdict text."""
    if use_mcp:
        system_prompt = (
            "You are a test evaluator. Follow the scenario steps exactly, "
            "using the send_message tool for each step. After all steps, "
            "analyze the responses and output VERDICT: PASS or VERDICT: FAIL. "
            "Be thorough but concise in your analysis."
        )
    else:
        system_prompt = (
            "You are a test evaluator. Read the provided transcript and "
            "evaluate it against the criteria. Output VERDICT: PASS or "
            "VERDICT: FAIL. Be thorough but concise in your analysis."
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

    passed = "VERDICT: PASS" in judgment.upper()

    return EvalResult(
        scenario=scenario.name,
        passed=passed,
        judgment=judgment,
    )
