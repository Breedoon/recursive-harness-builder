"""
Spike 27: Claude binary wrapper interposition.

Create a wrapper script that sits between the SDK and the real claude binary.
The wrapper:
1. Logs all invocations (args, env vars)
2. Captures stdin/stdout via tee
3. Reports session IDs and PIDs to a control file
4. Forwards everything to the real claude binary

This lets us intercept ALL subprocess creation, including Agent tool subagents.

Key question: If we make the agent use the wrapper instead of the real binary,
does everything still work? And can we capture useful data?
"""
import asyncio
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)
import claude_agent_sdk._internal.transport.subprocess_cli as cli_transport

os.environ.pop("CLAUDECODE", None)

# Find real binary
OriginalTransport = cli_transport.SubprocessCLITransport
original_find = OriginalTransport._find_cli_binary


def find_real_binary():
    """Find the real claude binary path."""
    t = OriginalTransport.__new__(OriginalTransport)
    return original_find(t)


REAL_BINARY = find_real_binary()
CONTROL_DIR = Path(tempfile.mkdtemp(prefix="claude-wrapper-"))
WRAPPER_SCRIPT = CONTROL_DIR / "claude-wrapper"
LOG_FILE = CONTROL_DIR / "invocations.log"
OUTPUT_DIR = CONTROL_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def create_wrapper():
    """Create wrapper shell script that intercepts claude invocations."""
    script = f"""#!/bin/bash
# Claude binary wrapper — intercepts and logs all invocations
REAL_BINARY="{REAL_BINARY}"
LOG_FILE="{LOG_FILE}"
OUTPUT_DIR="{OUTPUT_DIR}"

# Generate unique ID for this invocation
INVOCATION_ID="inv_$(date +%s)_$$"

# Log the invocation
echo "[$INVOCATION_ID] $(date -Iseconds) PID=$$ PPID=$PPID" >> "$LOG_FILE"
echo "[$INVOCATION_ID] ARGS: $@" >> "$LOG_FILE"
echo "[$INVOCATION_ID] CWD: $(pwd)" >> "$LOG_FILE"

# Check for --resume flag to capture session ID
for arg in "$@"; do
    if [ "$prev_arg" = "--resume" ]; then
        echo "[$INVOCATION_ID] SESSION_RESUME: $arg" >> "$LOG_FILE"
    fi
    prev_arg="$arg"
done

# Create output capture files
STDOUT_LOG="$OUTPUT_DIR/$INVOCATION_ID.stdout"
STDERR_LOG="$OUTPUT_DIR/$INVOCATION_ID.stderr"

# Execute real binary, tee stdout to log
exec "$REAL_BINARY" "$@" > >(tee "$STDOUT_LOG") 2> >(tee "$STDERR_LOG" >&2)
"""
    WRAPPER_SCRIPT.write_text(script)
    WRAPPER_SCRIPT.chmod(WRAPPER_SCRIPT.stat().st_mode | stat.S_IEXEC)
    print(f"  Wrapper created at: {WRAPPER_SCRIPT}")
    print(f"  Log file: {LOG_FILE}")
    print(f"  Real binary: {REAL_BINARY}")


async def main():
    print("=== Spike 27: Binary Wrapper Interposition ===\n")

    # Create the wrapper
    create_wrapper()

    # Patch _find_cli_binary to return our wrapper
    def patched_find(self):
        return str(WRAPPER_SCRIPT)

    OriginalTransport._find_cli_binary = patched_find

    # --- Test 1: Basic agent through wrapper ---
    print("\n--- Test 1: Basic agent through wrapper ---")
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
    )
    client = ClaudeSDKClient(options)

    try:
        async with client:
            await client.query("Say 'wrapper test successful' and nothing else.")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Response: {block.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  Session: {msg.session_id[:12]}...")
                    print(f"  Cost: ${msg.total_cost_usd:.4f}")

        print(f"  Wrapper test: PASSED")
    except Exception as e:
        print(f"  Wrapper test: FAILED — {e}")

    # Check log file
    if LOG_FILE.exists():
        log = LOG_FILE.read_text()
        print(f"\n  === Wrapper Log ===")
        for line in log.strip().split('\n'):
            print(f"    {line}")

    # Check output captures
    output_files = list(OUTPUT_DIR.iterdir())
    print(f"\n  Output files captured: {len(output_files)}")
    for f in output_files:
        size = f.stat().st_size
        print(f"    {f.name}: {size} bytes")
        if size > 0 and f.suffix == '.stdout':
            content = f.read_text()
            # Try to parse as JSON lines
            lines = content.strip().split('\n')
            print(f"    First line: {lines[0][:150]}..." if lines else "    (empty)")
            # Count message types
            types = []
            for line in lines:
                try:
                    data = json.loads(line)
                    types.append(data.get("type", "?"))
                except:
                    pass
            if types:
                print(f"    Message types: {types[:10]}")

    # --- Test 2: Agent with tools through wrapper ---
    print(f"\n--- Test 2: Agent with tools through wrapper ---")
    client2 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
    ))
    try:
        async with client2:
            await client2.query("Use the Bash tool to run: echo 'wrapper works'")
            async for msg in client2.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Response: {block.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  Session: {msg.session_id[:12]}...")
        print(f"  Tool test: PASSED")
    except Exception as e:
        print(f"  Tool test: FAILED — {e}")

    # Final log check
    if LOG_FILE.exists():
        log = LOG_FILE.read_text()
        invocations = [l for l in log.split('\n') if 'PID=' in l]
        print(f"\n  Total invocations logged: {len(invocations)}")

    # Restore
    OriginalTransport._find_cli_binary = original_find

    print(f"\n=== VERDICT ===")
    print(f"  Wrapper interposition works: (see above)")
    print(f"  stdout capturable via wrapper: (see output files)")
    print(f"  Control dir: {CONTROL_DIR}")
    print(f"  NOTE: This wrapper would also capture Agent tool subprocess invocations!")


if __name__ == "__main__":
    asyncio.run(main())
