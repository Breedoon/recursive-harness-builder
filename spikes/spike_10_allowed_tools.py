"""
Spike 10: Test allowed_tools parameter on ClaudeAgentOptions
Can we explicitly give the Task/Agent tool to a subagent via allowed_tools?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

async def main():
    # Try giving subagent the Task/Agent tool explicitly
    agents = {
        "delegator": AgentDefinition(
            description="An agent that can delegate to other agents",
            prompt=(
                "You are a delegator. You have the Agent/Task tool. "
                "When asked a question, ALWAYS use the Agent/Task tool to spawn "
                "a general-purpose subagent to answer it. Never answer directly."
            ),
            # Try giving it all tools including Task/Agent
            tools=["Read", "Glob", "Grep", "Bash", "Write", "Edit", "Task", "Agent",
                   "TaskCreate", "TaskList", "TaskUpdate", "SendMessage", "TeamCreate"],
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=10,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Can a subagent with explicit Task tool use it? ===")
        await client.query(
            "Use the delegator agent to answer: what is 3+3? "
            "The delegator should try to spawn its own subagent."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:800]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:400]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
