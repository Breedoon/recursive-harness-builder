"""
Resume an inflated session with opus[1m] — ONE API call only.
Reports token count and whether compaction was triggered.

Usage: python3 spikes/resume_inflated.py <session_id>
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
    HookMatcher,
    TextBlock,
    SystemMessage,
    AssistantMessage,
    ResultMessage,
)


async def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not session_id:
        print("Usage: python3 resume_inflated.py <session_id>")
        sys.exit(1)

    print(f"Resuming session {session_id} with opus[1m]...")
    print("Sending ONE message only.\n")

    compacted = False
    compact_meta = None

    async def on_pre_compact(hook_input, tool_use_id, context):
        nonlocal compacted, compact_meta
        compacted = True
        compact_meta = dict(hook_input)
        print(f"*** PreCompact! trigger={hook_input.get('trigger')} ***")
        return {"continue_": True}

    async def on_post_compact(hook_input, tool_use_id, context):
        summary = hook_input.get("compact_summary", "")
        print(f"*** PostCompact! summary_len={len(summary)} ***")
        if summary:
            print(f"    Preview: {summary[:300]}...")
        return {"continue_": True}

    opts = ClaudeAgentOptions(
        model="opus[1m]",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=session_id,
        hooks={
            "PreCompact": [HookMatcher(matcher=None, hooks=[on_pre_compact])],
            "PostCompact": [HookMatcher(matcher=None, hooks=[on_post_compact])],
        },
        thinking={"type": "disabled"},
    )

    try:
        async with ClaudeSDKClient(opts) as client:
            await client.query(
                "What is the last message number you can see in our conversation? "
                "Reply with just the number."
            )
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"Response: {block.text}")
                elif isinstance(msg, SystemMessage):
                    if msg.subtype == "compact_boundary":
                        meta = msg.data.get("compact_metadata", {})
                        print(f"COMPACTED! pre_tokens={meta.get('pre_tokens'):,}")
                    elif msg.subtype not in ("init",):
                        print(f"SystemMsg: {msg.subtype}")
                elif isinstance(msg, ResultMessage):
                    if msg.usage:
                        total = sum(msg.usage.get(k, 0) for k in [
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens"
                        ])
                        print(f"\nContext: {total:,} tokens")
                        print(f"  input: {msg.usage.get('input_tokens', 0):,}")
                        print(f"  cache_read: {msg.usage.get('cache_read_input_tokens', 0):,}")
                        print(f"  cache_create: {msg.usage.get('cache_creation_input_tokens', 0):,}")
                        print(f"  output: {msg.usage.get('output_tokens', 0):,}")
                    if msg.is_error:
                        print(f"ERROR: {msg.result}")

    except Exception as e:
        print(f"Exception: {type(e).__name__}: {e}")

    print(f"\nCompacted: {compacted}")
    if compact_meta:
        print(f"Compact meta: {json.dumps(compact_meta, indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())
