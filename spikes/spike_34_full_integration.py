"""
Spike 34: Full Integration — Hook-Intercept-Fork-Stream pattern.

Combines ALL proven techniques into one end-to-end demo:
1. Main SDK agent with Task tool hooks
2. When Task tool fires → intercept → fork from main session → run under SDK control
3. Stream forked agent output in real-time
4. Return result to main agent
5. Forked agent can itself fork (recursive)

Writes to /tmp/spike_34.log
"""
import asyncio
import json
import os
import time

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_34.log", "w")
def log(msg):
    LOG.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    ToolUseBlock,
)

CWD = "/Users/breedoon/Documents/obs"

# Simulated Telegram message queue
telegram_messages = []

async def fake_telegram_send(text, depth=0):
    """Simulate sending to Telegram."""
    prefix = "  " * depth
    telegram_messages.append({"text": text, "depth": depth, "time": time.time()})
    log(f"{prefix}[TG] {text[:150]}")


class TaskInterceptor:
    """Intercepts Task tool calls, runs replacement agents under full SDK control."""

    def __init__(self, depth=0, max_depth=2):
        self.depth = depth
        self.max_depth = max_depth
        self.intercepted_count = 0
        self.fork_sessions = []

    async def pre_tool_hook(self, hook_input, tool_use_id, context):
        """PreToolUse: intercept Task tool, run our own forked agent."""
        tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
        if tool_name != "Task":
            return {"continue_": True}

        self.intercepted_count += 1
        task_input = hook_input.get("tool_input", {})
        prompt = task_input.get("prompt", "")
        model = task_input.get("model", "haiku")
        main_sid = hook_input.get("session_id")

        prefix = "  " * self.depth
        log(f"{prefix}[INTERCEPT-{self.depth}] Task tool intercepted (#{self.intercepted_count})")
        log(f"{prefix}[INTERCEPT-{self.depth}] Prompt: {prompt[:200]}")
        log(f"{prefix}[INTERCEPT-{self.depth}] Model: {model}")
        log(f"{prefix}[INTERCEPT-{self.depth}] Main SID: {main_sid[:12]}...")

        await fake_telegram_send(f"🔄 Spawning forked agent (depth={self.depth}): {prompt[:80]}...", self.depth)

        # Fork from main session
        fork_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=model,
            max_turns=5,
            cwd=CWD,
            resume=main_sid,
            fork_session=True,
        )

        # If not at max depth, give the fork its own interceptor for recursion
        if self.depth < self.max_depth:
            child_interceptor = TaskInterceptor(depth=self.depth + 1, max_depth=self.max_depth)
            fork_opts = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                model=model,
                max_turns=5,
                cwd=CWD,
                resume=main_sid,
                fork_session=True,
                hooks={
                    "PreToolUse": [HookMatcher(hooks=[child_interceptor.pre_tool_hook])],
                },
            )

        result_text = ""
        fork_sid = None

        try:
            fork_client = ClaudeSDKClient(fork_opts)
            async with fork_client:
                await fork_client.query(prompt)
                async for msg in fork_client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                result_text += block.text
                                await fake_telegram_send(block.text[:200], self.depth)
                    elif isinstance(msg, ResultMessage):
                        fork_sid = msg.session_id
                        self.fork_sessions.append(fork_sid)
                        log(f"{prefix}[INTERCEPT-{self.depth}] Fork SID: {fork_sid[:12]}...")
                        log(f"{prefix}[INTERCEPT-{self.depth}] Cost: ${msg.total_cost_usd:.4f}")

            await fake_telegram_send(f"✅ Fork completed (depth={self.depth})", self.depth)
        except Exception as e:
            log(f"{prefix}[INTERCEPT-{self.depth}] Fork ERROR: {e}")
            result_text = f"Error: {e}"
            await fake_telegram_send(f"❌ Fork error: {e}", self.depth)

        # Block native Task and inject our result
        return {
            "continue_": False,
            "decision": "block",
            "reason": f"[Handled by SDK fork] {result_text[:2000]}",
        }


async def main():
    log("=== Spike 34: Full Integration — Hook-Intercept-Fork-Stream ===")

    interceptor = TaskInterceptor(depth=0, max_depth=2)

    # Create main agent with hooks
    log("\n--- Main Agent ---")
    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=10,
        cwd=CWD,
        hooks={
            "PreToolUse": [HookMatcher(hooks=[interceptor.pre_tool_hook])],
        },
    ))

    main_sid = None

    async with client:
        await client.query(
            "I need you to do two things using the Task tool:\n\n"
            "1. Use Task (subagent_type='general-purpose', model='haiku') to calculate: "
            "What is the sum of the first 5 prime numbers? Show your work.\n\n"
            "2. Use Task (subagent_type='general-purpose', model='haiku') to: "
            "Write a one-sentence summary of what Python is.\n\n"
            "Report both results."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        log(f"[MAIN] {block.text[:300]}")
                    elif isinstance(block, ToolUseBlock):
                        log(f"[MAIN] TOOL_USE: {block.name}")
            elif isinstance(msg, ResultMessage):
                main_sid = msg.session_id
                log(f"[MAIN] SID: {main_sid[:12]}...")
                log(f"[MAIN] Cost: ${msg.total_cost_usd:.4f}")

    # Report
    log(f"\n=== Final Report ===")
    log(f"  Main session: {main_sid}")
    log(f"  Tasks intercepted: {interceptor.intercepted_count}")
    log(f"  Fork sessions: {[s[:12] + '...' for s in interceptor.fork_sessions]}")
    log(f"  Telegram messages: {len(telegram_messages)}")
    for i, tm in enumerate(telegram_messages):
        log(f"    [{i}] depth={tm['depth']}: {tm['text'][:120]}")

    log(f"\n=== VERDICT ===")
    log(f"  Hook-Intercept-Fork-Stream pattern: {'WORKS' if interceptor.intercepted_count > 0 else 'FAILED'}")
    log(f"  Real-time streaming: {'WORKS' if len(telegram_messages) > 0 else 'FAILED'}")
    log(f"  Multiple forks: {'WORKS' if len(interceptor.fork_sessions) > 1 else 'PARTIAL'}")

    LOG.close()


asyncio.run(main())
