"""
Spike 19: Do CLAUDE_CODE_ENABLE_TASKS=1 and CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
inject native task/team tools into SDK-spawned agents?

Tests:
1. Set CLAUDE_CODE_ENABLE_TASKS=1 → check if TaskCreate/TaskList appear
2. Set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 → check if SendMessage/TeamCreate appear
3. Set BOTH → check if ALL tools appear
4. Try to actually USE TaskCreate to create a task
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

async def test_with_env(env_vars: dict, label: str):
    """Spawn a ClaudeSDKClient with given env vars and report tools."""
    print(f"\n=== {label} ===")
    print(f"  Env vars: {env_vars}")

    # Save and set env vars
    saved = {}
    for k, v in env_vars.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model="haiku",
            max_turns=5,
        )

        client = ClaudeSDKClient(options)
        tools_found = []

        async with client:
            await client.query(
                "List ALL tools you have access to. Be exhaustive. "
                "Specifically tell me if you have: TaskCreate, TaskList, TaskUpdate, TaskGet, "
                "SendMessage, TeamCreate, TeamDelete, Task (the agent spawning tool), "
                "EnterPlanMode, ExitPlanMode. "
                "Format as a numbered list of tool names only."
            )

            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        tools = msg.data.get("tools", [])
                        tools_found = tools
                        print(f"  SYSTEM INIT tools ({len(tools)}): {sorted(tools)}")
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  TEXT: {block.text[:1500]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

        return tools_found

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_use_task_create(env_vars: dict):
    """Try to actually CREATE a task using the native TaskCreate tool."""
    print(f"\n=== Test: Actually USE TaskCreate ===")

    saved = {}
    for k, v in env_vars.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model="haiku",
            max_turns=5,
        )

        client = ClaudeSDKClient(options)

        async with client:
            await client.query(
                "Use the TaskCreate tool to create a task with subject 'spike-19-test' "
                "and description 'Testing if SDK-spawned agents can use native task tools'. "
                "Then use TaskList to list all tasks. Report what happened."
            )

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  TEXT: {block.text[:1500]}")
                        elif hasattr(block, 'name'):
                            print(f"  TOOL: {block.name}({json.dumps(block.input)[:300]})")
                        elif hasattr(block, 'content'):
                            c = block.content if isinstance(block.content, str) else str(block.content)
                            print(f"  TOOL_RESULT: {c[:500]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main():
    # Test 1: ENABLE_TASKS only
    tools1 = await test_with_env(
        {"CLAUDE_CODE_ENABLE_TASKS": "1"},
        "Test 1: CLAUDE_CODE_ENABLE_TASKS=1 only"
    )

    # Test 2: EXPERIMENTAL_AGENT_TEAMS only
    tools2 = await test_with_env(
        {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "Test 2: CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 only"
    )

    # Test 3: BOTH env vars
    tools3 = await test_with_env(
        {
            "CLAUDE_CODE_ENABLE_TASKS": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        },
        "Test 3: BOTH env vars set"
    )

    # Summary
    print("\n=== SUMMARY ===")
    task_tools = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
    team_tools = {"SendMessage", "TeamCreate", "TeamDelete"}

    for label, tools in [("ENABLE_TASKS only", tools1), ("AGENT_TEAMS only", tools2), ("BOTH", tools3)]:
        found_task = task_tools.intersection(set(tools))
        found_team = team_tools.intersection(set(tools))
        print(f"  {label}: {len(tools)} tools | task={found_task or 'NONE'} | team={found_team or 'NONE'}")

    # Test 4: Actually try to use TaskCreate
    if task_tools.intersection(set(tools3)):
        await test_use_task_create({
            "CLAUDE_CODE_ENABLE_TASKS": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        })
    else:
        print("\n  SKIPPING TaskCreate test — tools not available")


if __name__ == "__main__":
    asyncio.run(main())
