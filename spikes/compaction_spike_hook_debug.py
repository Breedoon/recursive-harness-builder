"""
Spike: Debug why PreCompact hook doesn't fire.

From fast2 spike: compaction happened (SystemMessage compact_boundary emitted)
but PreCompact hook callback was never called.

Hypotheses:
1. Hook registration format is wrong
2. PreCompact hooks only fire for certain tool configurations
3. PreCompact hooks need to be registered differently than PreToolUse hooks
4. The Python SDK doesn't support PreCompact hooks (even though types exist)

Test: Register PreToolUse alongside PreCompact, enable tools, and verify
PreToolUse fires (proves hook registration works) while checking PreCompact.

Also reuse session cec20d87 (already at 167K tokens) to trigger compaction fast.
"""
import asyncio
import json
import sys
import os

os.environ.pop("CLAUDECODE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    TextBlock,
    SystemMessage,
    AssistantMessage,
    ResultMessage,
)

hook_calls = {"PreCompact": [], "PreToolUse": [], "PostToolUse": []}


async def on_pre_compact(hook_input, tool_use_id, context):
    """PreCompact hook."""
    hook_calls["PreCompact"].append(dict(hook_input))
    print(f"\n  ** PreCompact FIRED! trigger={hook_input.get('trigger')}")
    print(f"     custom_instructions={hook_input.get('custom_instructions')}")
    print(f"     all keys: {list(hook_input.keys())}")
    return {"continue_": True}


async def on_pre_tool_use(hook_input, tool_use_id, context):
    """PreToolUse hook — proves hook registration works."""
    hook_calls["PreToolUse"].append(hook_input.get("tool_name", "?"))
    print(f"  [PreToolUse] tool={hook_input.get('tool_name')}")
    return {"continue_": True}


async def on_post_tool_use(hook_input, tool_use_id, context):
    """PostToolUse hook — for completeness."""
    hook_calls["PostToolUse"].append(hook_input.get("tool_name", "?"))
    return {}


async def main():
    print("=== Spike: Debug PreCompact hook firing ===\n")

    # Fork the session that's already near compaction threshold
    NEAR_COMPACT_SESSION = "cec20d87-81f4-4ada-afcd-567f61b98091"
    padding = "The quick brown fox jumps over the lazy dog. " * 450

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="You are a test assistant. Follow instructions precisely.",
        permission_mode="bypassPermissions",
        max_turns=3,  # Allow tool use turns
        resume=NEAR_COMPACT_SESSION,
        fork_session=True,
        hooks={
            "PreCompact": [
                {"matcher": None, "hooks": [on_pre_compact]}
            ],
            "PreToolUse": [
                {"matcher": None, "hooks": [on_pre_tool_use]}
            ],
            "PostToolUse": [
                {"matcher": None, "hooks": [on_post_tool_use]}
            ],
        },
        thinking={"type": "disabled"},
    )

    print(f"Forking session {NEAR_COMPACT_SESSION} (already at ~167K tokens)")
    print("Sending messages with padding to push past compaction threshold...\n")

    async with ClaudeSDKClient(options) as client:
        # Turn 1: Use a tool to prove PreToolUse hook works
        prompt1 = "Read the file /etc/hostname or respond with 'no file'. Then say OK."
        print(f"Turn 1 (tool use test): {prompt1}")
        await client.query(prompt1)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:200]}")
            elif isinstance(msg, SystemMessage):
                if msg.subtype != "init":
                    print(f"  SystemMsg: {msg.subtype} -> {json.dumps(msg.data, default=str)[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

        # Turns 2+: Push toward compaction with padding
        for i in range(20):
            prompt = f"[MSG-{i+1}] Say 'OK {i+1}'. Padding: {padding}"
            print(f"\nTurn {i+2}: ~{len(prompt)//1000}K chars of padding")
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  -> {block.text.strip()[:100]}")
                elif isinstance(msg, SystemMessage):
                    if msg.subtype != "init":
                        print(f"  ** SystemMsg: {msg.subtype} -> {json.dumps(msg.data, default=str)[:300]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  Cost: ${msg.total_cost_usd:.4f}")
                    if msg.is_error:
                        print(f"  ERROR: {msg.result}")

    # Report
    print(f"\n{'='*60}")
    print("HOOK CALL SUMMARY")
    print(f"  PreToolUse calls: {len(hook_calls['PreToolUse'])} — {hook_calls['PreToolUse']}")
    print(f"  PostToolUse calls: {len(hook_calls['PostToolUse'])} — {hook_calls['PostToolUse']}")
    print(f"  PreCompact calls: {len(hook_calls['PreCompact'])}")
    if hook_calls['PreCompact']:
        for evt in hook_calls['PreCompact']:
            print(f"    {json.dumps(evt, indent=2, default=str)}")
    else:
        print("  *** PreCompact NEVER FIRED despite compaction occurring ***")
        print("  This suggests PreCompact hooks may not work via Python SDK hooks dict")


if __name__ == "__main__":
    asyncio.run(main())
