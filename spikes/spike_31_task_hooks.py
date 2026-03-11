"""
Spike 31: Can hooks intercept the Task tool?

We know can_use_tool doesn't fire. But what about PreToolUse/PostToolUse hooks?
Also tests: Can we see the Task tool result to extract subagent session ID?

Writes to /tmp/spike_31.log
"""
import asyncio
import json
import os

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_31.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    ToolUseBlock, ToolResultBlock,
)

hook_calls = []


async def pre_tool_hook(hook_input, tool_use_id, context):
    """PreToolUse hook — fires before tool execution."""
    tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
    entry = {"type": "pre", "tool": tool_name, "input": hook_input}
    hook_calls.append(entry)
    log(f"  [PRE] tool={tool_name}, id={tool_use_id}")
    if tool_name in ("Task", "Agent"):
        log(f"  [PRE] *** TASK HOOK! *** Input: {json.dumps(hook_input)[:500]}")
    return {"continue_": True}


async def post_tool_hook(hook_input, tool_use_id, context):
    """PostToolUse hook — fires after tool execution."""
    tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
    entry = {"type": "post", "tool": tool_name, "input": hook_input}
    hook_calls.append(entry)
    log(f"  [POST] tool={tool_name}, id={tool_use_id}")
    if tool_name in ("Task", "Agent"):
        log(f"  [POST] *** TASK HOOK! *** Output: {json.dumps(hook_input)[:500]}")
    return {"continue_": True}


async def main():
    log("=== Spike 31: Task Tool Hooks ===")

    # Test 1: Hook on all tools (wildcard matcher)
    log("\n--- Test 1: Hooks on all tools ---")
    hook_calls.clear()

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=8,
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[pre_tool_hook]),  # Match all tools
            ],
            "PostToolUse": [
                HookMatcher(hooks=[post_tool_hook]),  # Match all tools
            ],
        },
    ))

    all_tool_uses = []
    all_tool_results = []

    async with client:
        await client.query(
            "Do these steps:\n"
            "1. Use Bash to run: echo 'step1'\n"
            "2. Use the Task tool with subagent_type='general-purpose', "
            "model='haiku', prompt='What is 3+3? Answer with just the number.'\n"
            "3. Report both results."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:200]}")
                    elif isinstance(b, ToolUseBlock):
                        all_tool_uses.append({"name": b.name, "input": b.input})
                        log(f"  TOOL_USE: {b.name}({json.dumps(b.input)[:200]})")
                    elif isinstance(b, ToolResultBlock):
                        c = b.content if isinstance(b.content, str) else str(b.content)[:300]
                        all_tool_results.append(c)
                        log(f"  TOOL_RESULT: {c[:300]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    log(f"\n  Hook calls: {len(hook_calls)}")
    for h in hook_calls:
        log(f"    {h['type']}: {h['tool']}")

    task_hooked = any(h['tool'] in ('Task', 'Agent') for h in hook_calls)
    bash_hooked = any(h['tool'] == 'Bash' for h in hook_calls)
    log(f"  Task hooked: {task_hooked}")
    log(f"  Bash hooked: {bash_hooked}")
    log(f"  Tool uses seen: {[t['name'] for t in all_tool_uses]}")

    # Test 2: Hook specifically matching "Task"
    if not task_hooked:
        log("\n--- Test 2: Hook matching 'Task' specifically ---")
        hook_calls.clear()

        client2 = ClaudeSDKClient(ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model="haiku",
            max_turns=5,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Task", hooks=[pre_tool_hook]),
                ],
                "PostToolUse": [
                    HookMatcher(matcher="Task", hooks=[post_tool_hook]),
                ],
            },
        ))

        async with client2:
            await client2.query(
                "Use the Task tool with subagent_type='general-purpose', "
                "model='haiku', prompt='Say hello'. Report the result."
            )
            async for msg in client2.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  TEXT: {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

        log(f"  Task-specific hook calls: {len(hook_calls)}")

    log(f"\n=== VERDICT ===")
    log(f"  Hooks fire for Bash: {bash_hooked}")
    log(f"  Hooks fire for Task: {task_hooked}")
    log(f"  Task tool result visible in stream: {'Task' in [t['name'] for t in all_tool_uses]}")

    LOG.close()


asyncio.run(main())
