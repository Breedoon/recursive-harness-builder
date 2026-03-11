"""
Spike 32: Hook-based Task tool interception and replacement.

PROVEN: PreToolUse/PostToolUse hooks fire for Task tool (spike 31).
This spike tests:
1. Can we BLOCK the Task tool via PreToolUse hook?
2. What does the agent see when we block it?
3. Can we run our own SDK client and inject the result back?
4. What's in the FULL PostToolUse output (subagent session ID)?

Writes to /tmp/spike_32.log
"""
import asyncio
import json
import os

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_32.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    ToolUseBlock, ToolResultBlock,
)

CWD = "/Users/breedoon/Documents/obs"


async def test1_full_post_output():
    """Capture the FULL PostToolUse output for Task tool."""
    log("\n=== Test 1: Full PostToolUse Output ===")

    post_outputs = []

    async def post_hook(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        if tool_name == "Task":
            post_outputs.append(hook_input)
            log(f"  [POST-FULL] tool_use_id={tool_use_id}")
            log(f"  [POST-FULL] FULL INPUT:")
            log(json.dumps(hook_input, indent=2, default=str)[:3000])
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=8,
        cwd=CWD,
        hooks={
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    async with client:
        await client.query(
            "Use the Task tool with subagent_type='general-purpose', "
            "model='haiku', prompt='Say exactly: SUBAGENT_RESPONSE_XYZ'. "
            "Report the result."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolResultBlock):
                        c = b.content if isinstance(b.content, str) else str(b.content)
                        log(f"  TOOL_RESULT (stream): {c[:500]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    log(f"\n  Post outputs captured: {len(post_outputs)}")
    for po in post_outputs:
        # Look for subagent session ID
        tool_result = po.get("tool_result", {})
        log(f"  tool_result keys: {list(tool_result.keys()) if isinstance(tool_result, dict) else 'not dict'}")
        log(f"  tool_result type: {type(tool_result)}")
        if isinstance(tool_result, dict):
            log(f"  tool_result: {json.dumps(tool_result, indent=2, default=str)[:1000]}")
        elif isinstance(tool_result, str):
            log(f"  tool_result (str): {tool_result[:500]}")


async def test2_block_and_replace():
    """Block Task tool and inject our own result."""
    log("\n=== Test 2: Block + Replace ===")

    blocked_tasks = []

    async def pre_block_hook(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        if tool_name == "Task":
            task_input = hook_input.get("tool_input", {})
            blocked_tasks.append(task_input)
            log(f"  [BLOCK] Blocking Task tool!")
            log(f"  [BLOCK] Input: {json.dumps(task_input)[:500]}")

            # Run our own SDK client with the same prompt
            prompt = task_input.get("prompt", "")
            model = task_input.get("model", "haiku")
            log(f"  [BLOCK] Running our own agent: prompt='{prompt[:100]}', model={model}")

            try:
                our_client = ClaudeSDKClient(ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    model=model,
                    max_turns=3,
                    cwd=CWD,
                ))
                result_text = ""
                our_sid = None
                async with our_client:
                    await our_client.query(prompt)
                    async for msg in our_client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for b in msg.content:
                                if isinstance(b, TextBlock):
                                    result_text += b.text
                        elif isinstance(msg, ResultMessage):
                            our_sid = msg.session_id

                log(f"  [BLOCK] Our agent result: {result_text[:200]}")
                log(f"  [BLOCK] Our agent SID: {our_sid}")

                # Block the original and provide our result as a system message
                return {
                    "continue_": False,
                    "reason": f"Task handled by SDK. Result: {result_text[:500]}",
                    "decision": "block",
                }
            except Exception as e:
                log(f"  [BLOCK] Our agent ERROR: {e}")
                return {"continue_": True}  # Fall back to native Task

        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=8,
        cwd=CWD,
        hooks={
            "PreToolUse": [HookMatcher(hooks=[pre_block_hook])],
        },
    ))

    async with client:
        await client.query(
            "Use the Task tool with subagent_type='general-purpose', "
            "model='haiku', prompt='What is 7*8? Answer with just the number.'. "
            "Report the result you get back."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  TEXT: {b.text[:300]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    log(f"\n  Blocked tasks: {len(blocked_tasks)}")
    for bt in blocked_tasks:
        log(f"    {json.dumps(bt)[:300]}")


async def test3_session_fork_debug():
    """Debug why session fork/resume fails — capture CLI stderr."""
    log("\n=== Test 3: Session Fork Debug ===")

    # Create a session
    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=2,
        cwd=CWD,
    ))
    sid = None
    async with client:
        await client.query("Say 'hello world'")
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                sid = msg.session_id
                log(f"  Created session: {sid}")

    if not sid:
        log("  No session ID")
        return

    # Try fork with stderr callback
    log(f"\n  Attempting fork from {sid}...")

    stderr_lines = []

    def stderr_handler(line):
        stderr_lines.append(line)

    try:
        fork_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions", model="haiku", max_turns=2,
            resume=sid, fork_session=True,
            cwd=CWD,
            stderr=stderr_handler,
        )
        fork_client = ClaudeSDKClient(fork_opts)
        async with fork_client:
            await fork_client.query("What did I say before?")
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  Fork: {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  Fork SID: {msg.session_id}")
    except Exception as e:
        log(f"  Fork ERROR: {e}")
        log(f"  Stderr lines: {len(stderr_lines)}")
        for line in stderr_lines[:20]:
            log(f"    STDERR: {line}")


async def main():
    log("=== Spike 32: Hook Intercept + Replace ===")

    await test1_full_post_output()
    await test2_block_and_replace()
    await test3_session_fork_debug()

    LOG.close()


asyncio.run(main())
