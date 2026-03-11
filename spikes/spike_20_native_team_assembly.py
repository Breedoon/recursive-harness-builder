"""
Spike 20: Assemble a full team of SDK-spawned processes using native tools.

Approach:
1. Main agent creates a team with TeamCreate
2. Main agent creates tasks with TaskCreate
3. Spawn subagents (via MCP tool) with ENABLE_TASKS + AGENT_TEAMS env vars
4. Subagents use native TaskList/TaskUpdate to claim and complete tasks
5. Subagents use native SendMessage to communicate results
6. Main agent reads results

This proves we can have SDK-spawned agents using the full native team infrastructure.
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)

# Ensure team env vars are set for ALL spawned processes
os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

TEAM_NAME = "spike-20-native"

# Track spawn results
spawn_results = []


@tool("spawn_worker", "Spawn an SDK worker that joins the team", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Task for the worker"},
        "worker_name": {"type": "string", "description": "Name for this worker"},
    },
    "required": ["prompt", "worker_name"],
})
async def spawn_worker(args):
    """Spawn a ClaudeSDKClient worker with team env vars."""
    prompt = args.get("prompt", "")
    worker_name = args.get("worker_name", "worker")

    try:
        # Set worker-specific env vars
        os.environ["CLAUDE_CODE_AGENT_NAME"] = worker_name
        os.environ["CLAUDE_CODE_TEAM_NAME"] = TEAM_NAME

        options = ClaudeAgentOptions(
            model="haiku",
            permission_mode="bypassPermissions",
            max_turns=8,
        )
        client = ClaudeSDKClient(options)
        result_texts = []
        tools_seen = []

        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        tools_seen = msg.data.get("tools", [])
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_texts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    spawn_results.append({
                        "worker": worker_name,
                        "tools": len(tools_seen),
                        "has_TaskCreate": "TaskCreate" in tools_seen,
                        "has_SendMessage": "SendMessage" in tools_seen,
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                    })

        response = "\n".join(result_texts) if result_texts else "(no text)"
        return {"content": [{"type": "text", "text": f"Worker '{worker_name}' result:\nTools: {len(tools_seen)} (TaskCreate={'YES' if 'TaskCreate' in tools_seen else 'NO'}, SendMessage={'YES' if 'SendMessage' in tools_seen else 'NO'})\n\nResponse:\n{response[:500]}"}]}

    except Exception as e:
        import traceback
        return {"content": [{"type": "text", "text": f"ERROR spawning {worker_name}: {e}\n{traceback.format_exc()}"}], "is_error": True}


custom_server = create_sdk_mcp_server("team_tools", tools=[spawn_worker])


def cleanup():
    """Clean up team files."""
    for d in [
        Path.home() / ".claude" / "teams" / TEAM_NAME,
        Path.home() / ".claude" / "tasks" / TEAM_NAME,
    ]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Cleaned up: {d}")


async def main():
    cleanup()  # Clean any leftover state

    print("=== Spike 20: Native Team Assembly via SDK ===\n")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        mcp_servers={"team_tools": custom_server},
        max_turns=15,
    )

    client = ClaudeSDKClient(options)

    async with client:
        # Step 1: Create a team and tasks, then spawn workers
        print("--- Step 1: Create team, tasks, and spawn workers ---")
        await client.query(
            f"You have TeamCreate, TaskCreate, and a spawn_worker MCP tool. Do these steps IN ORDER:\n\n"
            f"1. Use TeamCreate to create a team named '{TEAM_NAME}'\n"
            f"2. Use TaskCreate to create a task: subject='Calculate fibonacci', description='Calculate fibonacci(10) and report the answer'\n"
            f"3. Use TaskCreate to create a second task: subject='Count vowels', description='Count vowels in the word supercalifragilistic and report the count'\n"
            f"4. Use the spawn_worker MCP tool to spawn a worker named 'math-worker' with prompt:\n"
            f"   'You are a team worker. Use TaskList to find pending tasks. Pick the fibonacci task, use TaskUpdate to mark it in_progress (set your owner to math-worker), calculate fibonacci(10)=55, then use TaskUpdate to mark it completed. Report what you did.'\n"
            f"5. Use the spawn_worker MCP tool to spawn a worker named 'string-worker' with prompt:\n"
            f"   'You are a team worker. Use TaskList to find pending tasks. Pick the vowels task, use TaskUpdate to mark it in_progress (set your owner to string-worker), count vowels in supercalifragilistic (answer: 12), then use TaskUpdate to mark it completed. Report what you did.'\n"
            f"6. After both workers complete, use TaskList to show final task states.\n\n"
            f"Report the full results."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:800]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  MAIN RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    # Check results
    print(f"\n=== Spawn Results ({len(spawn_results)} workers) ===")
    for r in spawn_results:
        print(f"  {json.dumps(r, indent=2)}")

    # Check task files
    task_dir = Path.home() / ".claude" / "tasks" / TEAM_NAME
    if task_dir.exists():
        print(f"\n=== Task Files ===")
        for f in sorted(task_dir.iterdir()):
            if f.suffix == ".json":
                data = json.loads(f.read_text())
                print(f"  {f.name}: status={data.get('status')}, owner={data.get('owner')}, subject={data.get('subject')}")
    else:
        print(f"\n  No task directory found at {task_dir}")

    # Check team config
    team_config = Path.home() / ".claude" / "teams" / TEAM_NAME / "config.json"
    if team_config.exists():
        config = json.loads(team_config.read_text())
        print(f"\n=== Team Config ===")
        print(f"  Members: {[m.get('name') for m in config.get('members', [])]}")

    cleanup()


if __name__ == "__main__":
    asyncio.run(main())
