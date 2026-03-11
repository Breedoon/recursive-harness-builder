"""
Spike 28: Task tool (= Agent tool) interception and usage.

DISCOVERY from spike 24b: SDK agents already have a "Task" tool that spawns agents!
This spike tests:
1. Can we make the SDK agent USE the Task tool to spawn a subagent?
2. Can we intercept Task tool calls via can_use_tool callback?
3. What does the Task tool input look like?
4. Can we deny + replace the Task tool call?

Writes to /tmp/spike_28.log
"""
import asyncio
import json
import os

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_28.log", "w")

def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    ToolUseBlock, ToolResultBlock,
    tool, create_sdk_mcp_server,
)

intercepted_tools = []


async def spy_can_use_tool(tool_name, tool_input, context):
    """Log ALL tool permission requests, especially Task."""
    entry = {"name": tool_name, "input": tool_input}
    intercepted_tools.append(entry)
    log(f"  [SPY] can_use_tool: {tool_name}")
    if tool_name in ("Task", "Agent"):
        log(f"  [SPY] *** TASK/AGENT INTERCEPTED! ***")
        log(f"  [SPY] Input: {json.dumps(tool_input, indent=2)[:1000]}")
    return {"behavior": "allow"}


async def test_task_usage():
    """Test 1: Can the SDK agent use the Task tool?"""
    log("\n=== Test 1: Task Tool Usage ===")

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="default",
        model="haiku",
        max_turns=8,
        can_use_tool=spy_can_use_tool,
    ))
    intercepted_tools.clear()
    tool_uses = []
    tool_results = []

    async with client:
        await client.query(
            "Use the Task tool to spawn a general-purpose agent with the prompt "
            "'What is 2+2? Answer with just the number.' "
            "Use subagent_type='general-purpose' and model='haiku'."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        tool_uses.append(b.name)
                        log(f"  TOOL_USE: {b.name}({json.dumps(b.input)[:200]})")
                    elif isinstance(b, ToolResultBlock):
                        c = b.content if isinstance(b.content, str) else str(b.content)
                        tool_results.append(c[:500])
                        log(f"  TOOL_RESULT: {c[:300]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: sid={msg.session_id[:12]}... cost=${msg.total_cost_usd:.4f}")

    log(f"\n  Tool uses: {tool_uses}")
    log(f"  can_use_tool calls: {[t['name'] for t in intercepted_tools]}")
    task_intercepted = any(t["name"] in ("Task", "Agent") for t in intercepted_tools)
    log(f"  Task intercepted by can_use_tool: {task_intercepted}")

    if task_intercepted:
        task_entries = [t for t in intercepted_tools if t["name"] in ("Task", "Agent")]
        for te in task_entries:
            log(f"  Task input schema: {json.dumps(te['input'], indent=2)[:500]}")

    return task_intercepted, tool_uses


async def test_task_deny_replace():
    """Test 2: Can we deny Task tool and handle it ourselves?"""
    log("\n=== Test 2: Task Tool Deny + Replace ===")

    denied_calls = []

    async def deny_task(tool_name, tool_input, context):
        intercepted_tools.append({"name": tool_name, "input": tool_input})
        if tool_name == "Task":
            log(f"  [DENY] Denying Task tool! Input: {json.dumps(tool_input)[:500]}")
            denied_calls.append(tool_input)
            return {
                "behavior": "deny",
                "message": "Task tool intercepted! The subagent result is: 4 (2+2=4)"
            }
        return {"behavior": "allow"}

    intercepted_tools.clear()
    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="default",
        model="haiku",
        max_turns=8,
        can_use_tool=deny_task,
    ))

    async with client:
        await client.query(
            "Use the Task tool to spawn a general-purpose agent with prompt "
            "'What is 2+2? Answer with just the number.' "
            "Use subagent_type='general-purpose' and model='haiku'. "
            "Report the result."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:300]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    log(f"  Denied calls: {len(denied_calls)}")
    for dc in denied_calls:
        log(f"  Denied input: {json.dumps(dc, indent=2)[:500]}")

    return len(denied_calls) > 0


async def test_bypass_with_callback():
    """Test 3: Does can_use_tool fire in bypassPermissions mode?"""
    log("\n=== Test 3: bypassPermissions + can_use_tool ===")

    intercepted_tools.clear()
    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=5,
        can_use_tool=spy_can_use_tool,
    ))

    async with client:
        await client.query("Use the Bash tool to run: echo test123")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    log(f"  can_use_tool calls in bypass mode: {[t['name'] for t in intercepted_tools]}")
    return len(intercepted_tools) > 0


async def main():
    log("=== Spike 28: Task Tool Interception ===")

    # Test 3 first (quick sanity check)
    bypass_fires = await test_bypass_with_callback()
    log(f"\n  bypass fires can_use_tool: {bypass_fires}")

    # Test 1: Usage
    task_intercepted, tool_uses = await test_task_usage()

    # Test 2: Deny + replace (only if interception works)
    if task_intercepted:
        deny_works = await test_task_deny_replace()
    else:
        deny_works = False
        log("\n  Skipping Test 2 — Task not intercepted in Test 1")

    log(f"\n=== FINAL VERDICT ===")
    log(f"  can_use_tool fires in bypass mode: {bypass_fires}")
    log(f"  Task tool interceptable: {task_intercepted}")
    log(f"  Task tool deniable: {deny_works}")
    log(f"  Tool uses seen: {tool_uses}")
    log(f"  Total can_use_tool calls: {len(intercepted_tools)}")

    LOG.close()


asyncio.run(main())
