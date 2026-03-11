"""
Spike 23b: Session forking and dual connection test.
Writes all output to /tmp/spike_23b.log
"""
import asyncio
import json
import os
import sys

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_23b.log", "w")

def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)


async def main():
    log("=== Spike 23b: Session Forking & Dual Connection ===")

    # Step 1: Create session with code word
    log("\n--- Step 1: Create initial session ---")
    client1 = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
    ))
    session_id = None

    async with client1:
        await client1.query("Remember this code word: PINEAPPLE-SUNSET-42. Say 'Code word stored.' only.")
        async for msg in client1.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  Client1: {b.text[:200]}")
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                log(f"  Session ID: {session_id}")

    if not session_id:
        log("FAILED: no session ID")
        return

    # Step 2: Fork from this session
    log(f"\n--- Step 2: Fork from {session_id[:12]}... ---")
    fork_client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        resume=session_id, fork_session=True,
    ))
    fork_sid = None

    try:
        async with fork_client:
            await fork_client.query("What is the code word I told you? Reply with ONLY the code word.")
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  Fork: {b.text[:200]}")
                            if "PINEAPPLE" in b.text.upper():
                                log("  *** FORK HAS PARENT CONTEXT! ***")
                elif isinstance(msg, ResultMessage):
                    fork_sid = msg.session_id
                    log(f"  Fork SID: {fork_sid}")
                    log(f"  Same as orig? {fork_sid == session_id}")
    except Exception as e:
        log(f"  Fork ERROR: {e}")

    # Step 3: Resume original session (not fork)
    log(f"\n--- Step 3: Resume original ---")
    resume_client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions", model="haiku", max_turns=3,
        resume=session_id,
    ))
    try:
        async with resume_client:
            await resume_client.query("What is the code word? Reply ONLY code word.")
            async for msg in resume_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  Resume: {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  Resume SID: {msg.session_id}")
                    log(f"  Same? {msg.session_id == session_id}")
    except Exception as e:
        log(f"  Resume ERROR: {e}")

    # Step 4: Simultaneous fork + resume
    log(f"\n--- Step 4: Simultaneous connections ---")

    async def run_client(name, opts, prompt):
        client = ClaudeSDKClient(opts)
        texts = []
        try:
            async with client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock):
                                texts.append(b.text)
                    elif isinstance(msg, ResultMessage):
                        return name, texts, msg.session_id, None
        except Exception as e:
            return name, texts, None, str(e)

    r = await asyncio.gather(
        run_client("resume-A", ClaudeAgentOptions(
            permission_mode="bypassPermissions", model="haiku", max_turns=3,
            resume=session_id,
        ), "What code word? ONLY the word."),
        run_client("fork-B", ClaudeAgentOptions(
            permission_mode="bypassPermissions", model="haiku", max_turns=3,
            resume=session_id, fork_session=True,
        ), "What code word? ONLY the word."),
    )
    for name, texts, sid, err in r:
        log(f"  {name}: texts={texts[:1]}, sid={sid[:12] if sid else None}..., err={err}")

    # Step 5: Check JSONL
    log(f"\n--- Step 5: JSONL inspection ---")
    from pathlib import Path
    proj = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
    sf = proj / f"{session_id}.jsonl"
    if sf.exists():
        lines = sf.read_text().strip().split('\n')
        log(f"  File: {len(lines)} lines, {sf.stat().st_size} bytes")
        for line in lines[:3]:
            d = json.loads(line)
            log(f"    type={d.get('type','?')}, keys={list(d.keys())[:5]}")
        if "PINEAPPLE" in sf.read_text():
            log(f"  *** Code word in JSONL = readable ***")
    else:
        log(f"  No JSONL at {sf}")

    log(f"\n=== VERDICT ===")
    log(f"  Session forking: {'WORKS' if fork_sid and fork_sid != session_id else 'FAILED'}")
    log(f"  Fork has context: {'YES' if fork_sid else 'UNKNOWN'}")

    LOG.close()


asyncio.run(main())
