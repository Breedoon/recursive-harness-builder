"""
Spike 24: Can we enable the Agent tool in SDK-spawned agents via env vars?

We know:
- CLAUDE_CODE_ENABLE_TASKS=1 → TaskCreate/TaskList/TaskGet/TaskUpdate
- CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 → TeamCreate/TeamDelete/SendMessage

Question: Is there an env var that enables the Agent tool itself?
Also: Does the init message reveal which tools include Agent?
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)

tool_lists = {}


@tool("check_tools", "Report which tools you have available", {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "Label for this check"},
    },
    "required": ["label"],
})
async def check_tools(args):
    return {"content": [{"type": "text", "text": f"Tools checked for {args.get('label')}"}]}


server = create_sdk_mcp_server("tool_checker", tools=[check_tools])


async def test_env_combo(label, env_overrides):
    """Spawn an SDK agent with specific env vars and check its tools."""
    print(f"\n--- {label} ---")

    # Set env vars
    saved = {}
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model="haiku",
            max_turns=3,
            mcp_servers={"tool_checker": server},
        )
        client = ClaudeSDKClient(options)
        tools_found = []

        async with client:
            await client.query(
                "List ALL tools you have available, one per line. "
                "Include: Read, Write, Edit, Bash, Glob, Grep, Agent, TaskCreate, TaskList, "
                "TaskUpdate, TaskGet, TeamCreate, TeamDelete, SendMessage, EnterPlanMode, "
                "ExitPlanMode, AskUserQuestion, WebFetch, WebSearch, NotebookEdit, "
                "EnterWorktree, Skill, check_tools. "
                "Format: TOOL: <name> for each one you have."
            )
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    tools_found = msg.data.get("tools", [])
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  {block.text[:500]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  cost=${msg.total_cost_usd:.4f}")

        tool_lists[label] = tools_found
        has_agent = "Agent" in tools_found
        has_task = "TaskCreate" in tools_found
        has_team = "TeamCreate" in tools_found
        print(f"  Total tools: {len(tools_found)}")
        print(f"  Has Agent: {has_agent}")
        print(f"  Has TaskCreate: {has_task}")
        print(f"  Has TeamCreate: {has_team}")
        print(f"  Tool list: {sorted(tools_found)}")
        return has_agent

    except Exception as e:
        print(f"  ERROR: {e}")
        return False
    finally:
        # Restore env
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main():
    print("=== Spike 24: Agent Tool via Environment Variables ===")

    # Test 1: Baseline (no special env vars)
    r1 = await test_env_combo("Baseline (no env vars)", {})

    # Test 2: Task + Team env vars
    r2 = await test_env_combo("Tasks + Teams", {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    })

    # Test 3: Try all plausible env vars for Agent tool
    r3 = await test_env_combo("All known + speculative", {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_ENABLE_AGENT": "1",
        "CLAUDE_CODE_ENABLE_AGENTS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENTS": "1",
        "CLAUDE_CODE_ENABLE_SUBAGENTS": "1",
    })

    # Test 4: Try with CLAUDE_AGENT_SDK_SUBAGENT=false to trick CLI into thinking we're main
    r4 = await test_env_combo("Pretend main session", {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_IS_SUBAGENT": "0",
        "CLAUDE_AGENT_SDK_SUBAGENT": "false",
    })

    print(f"\n=== SUMMARY ===")
    print(f"  Baseline: Agent={r1}")
    print(f"  Tasks+Teams: Agent={r2}")
    print(f"  Speculative: Agent={r3}")
    print(f"  Pretend main: Agent={r4}")

    if any([r1, r2, r3, r4]):
        print(f"\n  VERDICT: Agent tool CAN be enabled in SDK agents!")
    else:
        print(f"\n  VERDICT: Agent tool CANNOT be enabled via env vars.")
        print(f"  May need alternative approach (MCP tool replacement).")


if __name__ == "__main__":
    asyncio.run(main())
