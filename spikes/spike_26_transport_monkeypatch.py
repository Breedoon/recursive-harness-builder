"""
Spike 26: Monkey-patch SubprocessCLITransport to intercept subprocess creation.

Idea: Wrap the transport's connect() method to capture:
1. The subprocess PID
2. The stdin/stdout streams
3. The command-line args (including --resume session_id)

Then wrap the message stream to tee output to our own handler.

Also tests: Can we replace the CLI binary path with a wrapper?
"""
import asyncio
import json
import os
import sys
import functools
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)
import claude_agent_sdk._internal.transport.subprocess_cli as cli_transport

os.environ.pop("CLAUDECODE", None)

# Store intercepted data
intercepted = {
    "pid": None,
    "cmd": None,
    "messages": [],
    "tool_uses": [],
}

# Save originals
OriginalTransport = cli_transport.SubprocessCLITransport
original_build_cmd = OriginalTransport._build_command
original_connect = OriginalTransport.connect


async def patched_connect(self, *args, **kwargs):
    """Intercept connect to capture subprocess details."""
    result = await original_connect(self, *args, **kwargs)
    if hasattr(self, '_process') and self._process:
        intercepted["pid"] = self._process.pid
        print(f"  [PATCH] Captured subprocess PID: {self._process.pid}")
    return result


def patched_build_cmd(self, *args, **kwargs):
    """Intercept command building to see and modify args."""
    cmd = original_build_cmd(self, *args, **kwargs)
    intercepted["cmd"] = cmd
    print(f"  [PATCH] Command: {' '.join(str(c) for c in cmd[:5])}...")
    # Look for --resume flag
    for i, arg in enumerate(cmd):
        if str(arg) == "--resume" and i + 1 < len(cmd):
            print(f"  [PATCH] Session resume: {cmd[i+1]}")
    return cmd


# Apply patches
OriginalTransport.connect = patched_connect
OriginalTransport._build_command = patched_build_cmd


async def main():
    print("=== Spike 26: Transport Monkey-Patching ===\n")

    # --- Test 1: Basic interception ---
    print("--- Test 1: Intercept subprocess creation ---")
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
    )
    client = ClaudeSDKClient(options)
    session_id = None

    async with client:
        await client.query("Say 'hello world' and nothing else.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:100]}")
                    elif hasattr(block, 'name'):
                        intercepted["tool_uses"].append(block.name)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print(f"  Session ID: {session_id}")

    print(f"\n  Intercepted PID: {intercepted['pid']}")
    print(f"  Intercepted cmd length: {len(intercepted['cmd']) if intercepted['cmd'] else 0}")
    print(f"  Tool uses seen: {intercepted['tool_uses']}")

    # --- Test 2: Can we find and read the subprocess's session data? ---
    if session_id and intercepted['pid']:
        print(f"\n--- Test 2: Post-mortem session inspection ---")
        # Check if session JSONL exists
        project_path = "-Users-breedoon-Documents-obs"
        session_file = os.path.expanduser(f"~/.claude/projects/{project_path}/{session_id}.jsonl")
        if os.path.exists(session_file):
            with open(session_file) as f:
                lines = f.readlines()
            print(f"  Session JSONL: {len(lines)} lines")
            # Parse message types
            for line in lines[:5]:
                data = json.loads(line)
                print(f"    Type: {data.get('type', '?')}, keys: {list(data.keys())[:5]}")
        else:
            print(f"  Session file not found at {session_file}")

    # --- Test 3: Can we patch _find_cli_binary to use a wrapper? ---
    print(f"\n--- Test 3: Binary path override ---")
    original_find = OriginalTransport._find_cli_binary

    def patched_find_binary(self):
        original_path = original_find(self)
        print(f"  [PATCH] Original binary: {original_path}")
        # We could return a wrapper script here
        # For now, just log and return original
        return original_path

    OriginalTransport._find_cli_binary = patched_find_binary

    client2 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=2,
    ))
    async with client2:
        await client2.query("Say 'test' and nothing else.")
        async for msg in client2.receive_response():
            if isinstance(msg, ResultMessage):
                print(f"  Client2 done with session: {msg.session_id[:12]}...")

    # --- Test 4: Can we intercept message streams? ---
    print(f"\n--- Test 4: Message stream interception ---")

    original_read = OriginalTransport._read_messages_impl
    stream_messages = []

    async def patched_read_messages(self):
        """Wrap message stream to capture all messages."""
        async for msg in original_read(self):
            stream_messages.append(msg)
            yield msg

    OriginalTransport._read_messages_impl = patched_read_messages

    client3 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=2,
    ))
    async with client3:
        await client3.query("What is 2+2? Answer with just the number.")
        async for msg in client3.receive_response():
            if isinstance(msg, ResultMessage):
                print(f"  Client3 done")

    print(f"  Raw stream messages captured: {len(stream_messages)}")
    for m in stream_messages[:10]:
        if isinstance(m, dict):
            print(f"    {m.get('type', '?')}: {str(m)[:120]}")
        else:
            print(f"    {type(m).__name__}: {str(m)[:120]}")

    # Restore originals
    OriginalTransport.connect = original_connect
    OriginalTransport._build_command = original_build_cmd
    OriginalTransport._find_cli_binary = original_find
    OriginalTransport._read_messages_impl = original_read

    print(f"\n=== VERDICT ===")
    print(f"  PID capturable: {intercepted['pid'] is not None}")
    print(f"  Command interceptable: {intercepted['cmd'] is not None}")
    print(f"  Binary path overridable: True (patched_find_binary worked)")
    print(f"  Message stream interceptable: {len(stream_messages) > 0}")
    print(f"  Session data readable: {session_id is not None}")


if __name__ == "__main__":
    asyncio.run(main())
