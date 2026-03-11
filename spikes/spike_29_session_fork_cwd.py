"""
Spike 29: Session forking with correct cwd.

Spike 23b failed on fork/resume. This tests with explicit cwd to fix project context.
Also tests: can we read a session's full conversation from JSONL?

Writes to /tmp/spike_29.log
"""
import asyncio
import json
import os
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_29.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

CWD = "/Users/breedoon/Documents/obs"


async def main():
    log("=== Spike 29: Session Fork with CWD ===")

    # Step 1: Create session
    log("\n--- Step 1: Create session ---")
    client1 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        cwd=CWD,
    ))
    sid = None

    async with client1:
        await client1.query("Remember: SECRET_CODE=DIAMOND-77. Say 'stored' only.")
        async for msg in client1.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  {b.text[:100]}")
            elif isinstance(msg, ResultMessage):
                sid = msg.session_id
                log(f"  SID: {sid}")
                log(f"  Cost: ${msg.total_cost_usd:.4f}")

    if not sid:
        log("FAILED: no session ID")
        LOG.close()
        return

    # Check JSONL content
    log(f"\n--- JSONL Content ---")
    proj = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
    sf = proj / f"{sid}.jsonl"
    if sf.exists():
        content = sf.read_text()
        lines = content.strip().split('\n')
        log(f"  {len(lines)} lines, {sf.stat().st_size} bytes")
        for i, line in enumerate(lines):
            d = json.loads(line)
            log(f"  [{i}] type={d.get('type')}, keys={list(d.keys())[:6]}")
        if "DIAMOND" in content:
            log(f"  SECRET_CODE found in JSONL!")
    else:
        log(f"  No JSONL at {sf}")
        # Try other project dirs
        for d in Path.home().joinpath(".claude", "projects").iterdir():
            candidate = d / f"{sid}.jsonl"
            if candidate.exists():
                log(f"  Found at: {candidate}")
                break

    # Step 2: Fork with same cwd
    log(f"\n--- Step 2: Fork with cwd ---")
    fork_client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        resume=sid, fork_session=True,
        cwd=CWD,
    ))
    try:
        async with fork_client:
            await fork_client.query("What is SECRET_CODE? Reply ONLY the code value.")
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  Fork: {b.text[:200]}")
                            if "DIAMOND" in b.text.upper():
                                log(f"  *** FORK HAS PARENT CONTEXT! ***")
                elif isinstance(msg, ResultMessage):
                    log(f"  Fork SID: {msg.session_id}")
                    log(f"  Different from parent: {msg.session_id != sid}")
    except Exception as e:
        log(f"  Fork ERROR: {e}")
        import traceback
        LOG.write(traceback.format_exc() + "\n")
        LOG.flush()

    # Step 3: Resume with same cwd
    log(f"\n--- Step 3: Resume ---")
    resume_client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        resume=sid,
        cwd=CWD,
    ))
    try:
        async with resume_client:
            await resume_client.query("What is SECRET_CODE? Reply ONLY the code value.")
            async for msg in resume_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  Resume: {b.text[:200]}")
                            if "DIAMOND" in b.text.upper():
                                log(f"  *** RESUME HAS SESSION CONTEXT! ***")
                elif isinstance(msg, ResultMessage):
                    log(f"  Resume SID: {msg.session_id}")
                    log(f"  Same as parent: {msg.session_id == sid}")
    except Exception as e:
        log(f"  Resume ERROR: {e}")
        import traceback
        LOG.write(traceback.format_exc() + "\n")
        LOG.flush()

    # Step 4: Fork from a DIFFERENT cwd (to test isolation)
    log(f"\n--- Step 4: Fork from different cwd ---")
    fork2 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        resume=sid, fork_session=True,
        cwd="/tmp",
    ))
    try:
        async with fork2:
            await fork2.query("What is SECRET_CODE? Reply ONLY the code value.")
            async for msg in fork2.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  DiffCwd Fork: {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  DiffCwd SID: {msg.session_id}")
    except Exception as e:
        log(f"  DiffCwd Fork ERROR: {e}")

    LOG.close()


asyncio.run(main())
