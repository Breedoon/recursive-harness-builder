"""
Spike 25: Process-level session hijacking

Creative approaches to gain control of a running agent subprocess:

1. Find PID from ps, read /dev/fd/ (macOS doesn't have /proc)
2. Use lsof to find the subprocess's stdin/stdout file descriptors
3. Try to attach to a running session via resume while subprocess is alive
4. Read session JSONL in real-time (tail -f style)
5. Use dtrace or similar for I/O interception

Also tests the "binary wrapper" approach: can we intercept subprocess creation?
"""
import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)


# Global to capture session ID from inside running agent
captured_session_id = None
agent_pid = None


@tool("report_session", "Report your session info", {
    "type": "object",
    "properties": {
        "msg": {"type": "string"},
    },
    "required": ["msg"],
})
async def report_session(args):
    """MCP tool to get info from inside the agent."""
    global agent_pid
    # Try to find the agent's PID
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    )
    for line in result.stdout.split('\n'):
        if 'claude' in line and 'stream-json' in line and 'grep' not in line:
            parts = line.split()
            if len(parts) > 1:
                agent_pid = int(parts[1])

    return {"content": [{"type": "text", "text": f"Session info captured. PID candidates found."}]}


server = create_sdk_mcp_server("hijack_tools", tools=[report_session])


async def main():
    print("=== Spike 25: Process-Level Session Hijacking ===\n")

    # --- Test 1: Spawn agent, capture session, try parallel resume ---
    print("--- Test 1: Spawn long-running agent + parallel hijack ---")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=5,
        mcp_servers={"hijack_tools": server},
    )

    session_id_holder = {"id": None}

    async def run_main_agent():
        """Run main agent with a slow task."""
        client = ClaudeSDKClient(options)
        async with client:
            await client.query(
                "Step 1: Call report_session tool with msg='starting'. "
                "Step 2: Remember the code word DIAMOND-FORTRESS-99. "
                "Step 3: Write a detailed 500-word essay about why the sky is blue. "
                "Step 4: When done, say 'ESSAY COMPLETE'."
            )
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    session_id_holder["id"] = msg.session_id
                    print(f"  Main agent done. Session: {msg.session_id[:12]}...")
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            if len(block.text) < 100:
                                print(f"  Main: {block.text}")

    async def try_hijack():
        """Wait for agent to start, then try to hijack its session."""
        # Wait for PID discovery
        for _ in range(30):
            await asyncio.sleep(1)
            if agent_pid:
                break

        if not agent_pid:
            print("  Hijack: Could not find agent PID")
            return

        print(f"  Hijack: Found agent PID {agent_pid}")

        # Try to read its file descriptors (macOS)
        try:
            result = subprocess.run(
                ["lsof", "-p", str(agent_pid)],
                capture_output=True, text=True, timeout=5
            )
            pipe_fds = [l for l in result.stdout.split('\n') if 'PIPE' in l or 'FIFO' in l]
            print(f"  Hijack: Found {len(pipe_fds)} pipe FDs")
            for fd in pipe_fds[:5]:
                print(f"    {fd.strip()}")
        except Exception as e:
            print(f"  Hijack: lsof error: {e}")

        # Try to find session JSONL
        project_dir = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
        recent_files = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
        print(f"  Hijack: {len(recent_files)} recent JSONL files")
        for f in recent_files:
            mtime = time.strftime('%H:%M:%S', time.localtime(f.stat().st_mtime))
            size = f.stat().st_size
            print(f"    {f.stem[:12]}... modified={mtime} size={size}B")

    # Run both concurrently
    await asyncio.gather(
        run_main_agent(),
        try_hijack(),
    )

    session_id = session_id_holder["id"]
    if not session_id:
        print("  FAILED: No session ID captured")
        return

    # --- Test 2: Fork from completed session ---
    print(f"\n--- Test 2: Fork from completed session {session_id[:12]}... ---")
    fork_options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
        resume=session_id,
        fork_session=True,
    )
    fork_client = ClaudeSDKClient(fork_options)
    try:
        async with fork_client:
            await fork_client.query("What is the code word I told you earlier? Reply with ONLY the code word.")
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Fork result: {block.text[:200]}")
                            if "DIAMOND" in block.text.upper():
                                print(f"  *** FORK HAS PARENT CONTEXT! ***")
                elif isinstance(msg, ResultMessage):
                    print(f"  Fork session: {msg.session_id[:12]}...")
    except Exception as e:
        print(f"  Fork ERROR: {e}")

    # --- Test 3: Read JSONL tail in real-time ---
    print(f"\n--- Test 3: Session JSONL inspection ---")
    project_dir = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
    session_file = project_dir / f"{session_id}.jsonl"
    if session_file.exists():
        lines = session_file.read_text().strip().split('\n')
        print(f"  Session file: {len(lines)} lines, {session_file.stat().st_size} bytes")
        # Show message types
        types = []
        for line in lines:
            try:
                data = json.loads(line)
                types.append(data.get("type", "?"))
            except:
                types.append("?")
        print(f"  Message types: {types}")
        # Check if code word is in the file
        content = session_file.read_text()
        if "DIAMOND" in content:
            print(f"  *** Code word found in JSONL — session data is readable! ***")
    else:
        print(f"  Session file not found at {session_file}")

    print(f"\n=== VERDICT ===")
    print(f"  Session ID capturable: {session_id is not None}")
    print(f"  Agent PID discoverable: {agent_pid is not None}")
    print(f"  Session forking works: (see above)")
    print(f"  JSONL readable: {session_file.exists() if session_id else False}")


if __name__ == "__main__":
    asyncio.run(main())
