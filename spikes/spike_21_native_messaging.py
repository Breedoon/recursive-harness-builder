"""
Spike 21: Test native SendMessage between SDK-spawned workers.

Proves that SDK-spawned workers can:
1. Use native SendMessage to write to inboxes
2. Read messages from inbox JSON files
3. Communicate with the team leader and each other
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

TEAM_NAME = "spike-21-msg"

os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = TEAM_NAME
os.environ["CLAUDE_CODE_TEAM_NAME"] = TEAM_NAME

spawn_results = []


@tool("spawn_worker", "Spawn a worker with team context", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "worker_name": {"type": "string"},
    },
    "required": ["prompt", "worker_name"],
})
async def spawn_worker(args):
    prompt = args.get("prompt", "")
    worker_name = args.get("worker_name", "worker")

    os.environ["CLAUDE_CODE_AGENT_NAME"] = worker_name

    try:
        options = ClaudeAgentOptions(
            model="haiku",
            permission_mode="bypassPermissions",
            max_turns=5,
        )
        client = ClaudeSDKClient(options)
        result_texts = []

        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_texts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    spawn_results.append({
                        "worker": worker_name,
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                    })

        response = "\n".join(result_texts) if result_texts else "(no text)"
        return {"content": [{"type": "text", "text": f"[{worker_name}]: {response[:800]}"}]}

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


async def main():
    cleanup()
    print("=== Spike 21: Native SendMessage between workers ===\n")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        mcp_servers={"team_tools": custom_server},
        max_turns=12,
    )

    client = ClaudeSDKClient(options)

    async with client:
        await client.query(
            f"Do these steps:\n\n"
            f"1. Use TeamCreate to create team '{TEAM_NAME}'\n"
            f"2. Use spawn_worker with worker_name='sender' and prompt:\n"
            f"   'Use the SendMessage tool to send a message. Set type to \"message\", "
            f"recipient to \"team-lead\", content to \"The secret code is ALPHA-7\", "
            f"and summary to \"Secret code delivery\". Report what happened.'\n"
            f"3. After the sender is done, check the inbox file at "
            f"~/.claude/teams/{TEAM_NAME}/inboxes/team-lead.json using the Read tool.\n"
            f"4. Report whether the message was delivered to the inbox file.\n"
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:1200]}")
                    elif hasattr(block, 'name'):
                        inp = json.dumps(block.input) if hasattr(block, 'input') else ""
                        print(f"  TOOL: {block.name}({inp[:300]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  RESULT: {c[:600]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  DONE: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    # Check inbox directly
    inbox_dir = Path.home() / ".claude" / "teams" / TEAM_NAME / "inboxes"
    if inbox_dir.exists():
        print(f"\n=== Inbox Files ===")
        for f in sorted(inbox_dir.iterdir()):
            if f.suffix == ".json":
                data = json.loads(f.read_text())
                print(f"  {f.name}: {len(data)} messages")
                for msg in data:
                    if isinstance(msg, dict):
                        print(f"    from={msg.get('from')}, text={str(msg.get('text', ''))[:100]}")
    else:
        print(f"\n  No inbox dir at {inbox_dir}")

    print(f"\n=== Worker Results ===")
    for r in spawn_results:
        print(f"  {json.dumps(r)}")

    cleanup()


if __name__ == "__main__":
    asyncio.run(main())
