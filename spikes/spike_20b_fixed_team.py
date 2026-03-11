"""
Spike 20b: Fixed team assembly — set CLAUDE_CODE_TASK_LIST_ID so workers
share the same task directory as the team leader.

Env vars needed:
  CLAUDE_CODE_ENABLE_TASKS=1              → enables TaskCreate/TaskList/etc
  CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1  → enables SendMessage/TeamCreate
  CLAUDE_CODE_TASK_LIST_ID=<team-name>    → points task tools to correct dir
  CLAUDE_CODE_TEAM_NAME=<team-name>       → team context for SendMessage
  CLAUDE_CODE_AGENT_NAME=<worker-name>    → worker identity
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)

TEAM_NAME = "spike-20b-team"

# Set env for ALL processes
os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = TEAM_NAME
os.environ["CLAUDE_CODE_TEAM_NAME"] = TEAM_NAME

spawn_results = []


@tool("spawn_worker", "Spawn an SDK worker that uses native task tools", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Task for the worker"},
        "worker_name": {"type": "string", "description": "Name for this worker"},
    },
    "required": ["prompt", "worker_name"],
})
async def spawn_worker(args):
    prompt = args.get("prompt", "")
    worker_name = args.get("worker_name", "worker")

    # Worker-specific env
    os.environ["CLAUDE_CODE_AGENT_NAME"] = worker_name

    try:
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
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    tools_seen = msg.data.get("tools", [])
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_texts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    spawn_results.append({
                        "worker": worker_name,
                        "tools_count": len(tools_seen),
                        "has_TaskList": "TaskList" in tools_seen,
                        "has_TaskUpdate": "TaskUpdate" in tools_seen,
                        "has_SendMessage": "SendMessage" in tools_seen,
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                    })

        response = "\n".join(result_texts) if result_texts else "(no text)"
        return {"content": [{"type": "text", "text": f"[{worker_name}] Tools: {len(tools_seen)}, TaskList={'Y' if 'TaskList' in tools_seen else 'N'}\n{response[:800]}"}]}

    except Exception as e:
        import traceback
        return {"content": [{"type": "text", "text": f"ERROR: {e}\n{traceback.format_exc()}"}], "is_error": True}


custom_server = create_sdk_mcp_server("team_tools", tools=[spawn_worker])


def cleanup():
    for d in [
        Path.home() / ".claude" / "teams" / TEAM_NAME,
        Path.home() / ".claude" / "tasks" / TEAM_NAME,
    ]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Cleaned: {d}")


async def main():
    cleanup()
    print("=== Spike 20b: Native Team with TASK_LIST_ID ===\n")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        mcp_servers={"team_tools": custom_server},
        max_turns=15,
    )

    client = ClaudeSDKClient(options)

    async with client:
        await client.query(
            f"Do these steps IN ORDER:\n\n"
            f"1. Use TeamCreate to create team '{TEAM_NAME}'\n"
            f"2. Use TaskCreate: subject='Add numbers', description='Calculate 17+25 and report the answer (42)', activeForm='Adding numbers'\n"
            f"3. Use TaskCreate: subject='Reverse string', description='Reverse the string hello and report (olleh)', activeForm='Reversing string'\n"
            f"4. Use spawn_worker MCP tool with worker_name='calc-worker' and prompt:\n"
            f"   'Use TaskList to see tasks. Find the \"Add numbers\" task (it should be pending). "
            f"Use TaskUpdate with taskId for that task to set status to in_progress and owner to calc-worker. "
            f"The answer is 17+25=42. Then use TaskUpdate to set status to completed. "
            f"Report: which tasks you saw, what you did.'\n"
            f"5. Use spawn_worker MCP tool with worker_name='str-worker' and prompt:\n"
            f"   'Use TaskList to see tasks. Find the \"Reverse string\" task (it should be pending). "
            f"Use TaskUpdate with taskId for that task to set status to in_progress and owner to str-worker. "
            f"The answer is olleh. Then use TaskUpdate to set status to completed. "
            f"Report: which tasks you saw, what you did.'\n"
            f"6. Use TaskList to show the final task states.\n\n"
            f"Report everything."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:1000]}")
                    elif hasattr(block, 'name'):
                        inp = json.dumps(block.input) if hasattr(block, 'input') else ""
                        print(f"  TOOL: {block.name}({inp[:250]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  RESULT: {c[:600]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  MAIN DONE: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    # Verify results
    print(f"\n=== Worker Spawn Results ===")
    for r in spawn_results:
        print(f"  {json.dumps(r, indent=2)}")

    task_dir = Path.home() / ".claude" / "tasks" / TEAM_NAME
    if task_dir.exists():
        print(f"\n=== Task Files (ground truth) ===")
        for f in sorted(task_dir.iterdir()):
            if f.suffix == ".json":
                data = json.loads(f.read_text())
                print(f"  {f.name}: status={data.get('status')}, owner={data.get('owner')}, subject={data.get('subject')}")

    cleanup()


if __name__ == "__main__":
    asyncio.run(main())
