"""
Spike 24b: Check if Agent tool is available in SDK agents.
Writes to /tmp/spike_24b.log
"""
import asyncio
import json
import os

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_24b.log", "w")

def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)


async def check_tools(label, env_overrides):
    log(f"\n--- {label} ---")
    saved = {}
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        client = ClaudeSDKClient(ClaudeAgentOptions(
            permission_mode="bypassPermissions", model="haiku", max_turns=3,
        ))
        tools = []
        async with client:
            await client.query(
                "List every tool name you have, one per line. "
                "Just the tool names, nothing else."
            )
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    tools = msg.data.get("tools", [])
                elif isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  {b.text[:400]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  cost=${msg.total_cost_usd:.4f}")

        has_agent = "Agent" in tools
        log(f"  Total tools: {len(tools)}")
        log(f"  Has Agent: {has_agent}")
        log(f"  Has TaskCreate: {'TaskCreate' in tools}")
        log(f"  Has TeamCreate: {'TeamCreate' in tools}")
        log(f"  Has SendMessage: {'SendMessage' in tools}")
        log(f"  All tools: {sorted(tools)}")
        return has_agent
    except Exception as e:
        log(f"  ERROR: {e}")
        return False
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main():
    log("=== Spike 24b: Agent Tool in SDK Agents ===")

    r1 = await check_tools("Baseline", {})
    r2 = await check_tools("Tasks+Teams", {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    })
    r3 = await check_tools("Speculative env vars", {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_ENABLE_AGENT": "1",
        "CLAUDE_CODE_ENABLE_AGENTS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENTS": "1",
    })

    log(f"\n=== SUMMARY ===")
    log(f"  Baseline Agent: {r1}")
    log(f"  Tasks+Teams Agent: {r2}")
    log(f"  Speculative Agent: {r3}")
    LOG.close()


asyncio.run(main())
