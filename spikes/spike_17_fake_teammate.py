"""
Spike 17: Can we make an SDK-spawned agent behave as a team member?
Strategy:
1. Create team config files manually
2. Spawn a ClaudeSDKClient with team env vars set
3. See if it gets TaskCreate/TaskList/SendMessage tools
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

TEAM_NAME = "spike-fake-team"
TEAM_DIR = Path.home() / ".claude" / "teams" / TEAM_NAME
TASK_DIR = Path.home() / ".claude" / "tasks" / TEAM_NAME

def setup_team_files():
    """Create team config manually, as if TeamCreate had been called."""
    TEAM_DIR.mkdir(parents=True, exist_ok=True)
    (TEAM_DIR / "inboxes").mkdir(exist_ok=True)
    TASK_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "name": TEAM_NAME,
        "description": "Fake team for testing SDK teammate",
        "createdAt": 1772568000000,
        "leadAgentId": f"team-lead@{TEAM_NAME}",
        "leadSessionId": "fake-lead-session",
        "members": [
            {
                "agentId": f"team-lead@{TEAM_NAME}",
                "name": "team-lead",
                "agentType": "team-lead",
                "model": "claude-haiku-4-5-20251001",
                "joinedAt": 1772568000000,
                "tmuxPaneId": "",
                "cwd": str(Path.cwd()),
                "subscriptions": [],
            },
            {
                "agentId": f"worker@{TEAM_NAME}",
                "name": "worker",
                "agentType": "general-purpose",
                "model": "claude-haiku-4-5-20251001",
                "prompt": "You are a team worker. Report your available tools.",
                "color": "blue",
                "planModeRequired": False,
                "joinedAt": 1772568001000,
                "tmuxPaneId": "in-process",
                "cwd": str(Path.cwd()),
                "subscriptions": [],
                "backendType": "in-process",
            },
        ],
    }

    (TEAM_DIR / "config.json").write_text(json.dumps(config, indent=2))

    # Create inbox files
    (TEAM_DIR / "inboxes" / "team-lead.json").write_text("[]")
    (TEAM_DIR / "inboxes" / "worker.json").write_text("[]")

    # Create a task
    task = {
        "id": "1",
        "subject": "Report available tools",
        "description": "List all tools you have access to",
        "activeForm": "Reporting tools",
        "status": "pending",
        "blocks": [],
        "blockedBy": [],
    }
    (TASK_DIR / "1.json").write_text(json.dumps(task, indent=2))
    (TASK_DIR / ".lock").write_text("")
    (TASK_DIR / ".highwatermark").write_text("1")

    print(f"Created team files at {TEAM_DIR}")
    print(f"Created task files at {TASK_DIR}")

def cleanup():
    for d in [TEAM_DIR, TASK_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"Cleaned up: {d}")

async def test_with_env_vars():
    """Try spawning a client with team env vars set."""
    print("\n=== Test: SDK client with team env vars ===")

    # Set team-related env vars that the CLI normally sets for teammates
    team_env = {
        "CLAUDE_CODE_TEAM_NAME": TEAM_NAME,
        "CLAUDE_CODE_AGENT_ID": f"worker@{TEAM_NAME}",
        "CLAUDE_CODE_AGENT_NAME": "worker",
        "CLAUDE_CODE_AGENT_TYPE": "general-purpose",
        "CLAUDE_CODE_AGENT_COLOR": "blue",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    }

    # Save and set
    saved = {}
    for k, v in team_env.items():
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
                "List ALL tools you have access to. Be exhaustive. "
                "Specifically check for: TaskCreate, TaskList, TaskUpdate, TaskGet, "
                "SendMessage, TeamCreate, Task (the agent spawning tool). "
                "Also tell me if you know what team you're on."
            )

            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        tools = msg.data.get("tools", [])
                        print(f"  SYSTEM INIT tools ({len(tools)}): {tools}")
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  TEXT: {block.text[:1500]}")
                elif isinstance(msg, ResultMessage):
                    print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    finally:
        # Restore env
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

async def main():
    setup_team_files()
    try:
        await test_with_env_vars()
    finally:
        cleanup()

if __name__ == "__main__":
    asyncio.run(main())
