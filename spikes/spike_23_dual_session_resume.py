"""
Spike 23: Can two SDK clients connect to the same session simultaneously?

If yes, we can "hijack" a running agent's session by resuming it from another client.
If no, can we at least fork from a running session?

Also tests: Can we read a session's JSONL file while it's active?
"""
import asyncio
import json
import os
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)


async def main():
    print("=== Spike 23: Dual Session Connection ===\n")

    # Step 1: Create a session and get its ID
    print("--- Step 1: Create initial session ---")
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
    )
    client1 = ClaudeSDKClient(options)
    session_id = None

    async with client1:
        await client1.query("Remember this code word: PINEAPPLE-SUNSET-42. Say 'Code word stored.' and nothing else.")
        async for msg in client1.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Client1: {block.text[:200]}")
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print(f"  Session ID: {session_id}")

    if not session_id:
        print("  FAILED: No session ID obtained")
        return

    # Step 2: Try to fork from this session
    print(f"\n--- Step 2: Fork from session {session_id[:12]}... ---")
    fork_options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
        resume=session_id,
        fork_session=True,
    )
    fork_client = ClaudeSDKClient(fork_options)
    fork_session_id = None

    try:
        async with fork_client:
            await fork_client.query("What is the code word I told you? Reply with ONLY the code word.")
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Fork: {block.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    fork_session_id = msg.session_id
                    print(f"  Fork session ID: {fork_session_id}")
                    print(f"  Same as original? {fork_session_id == session_id}")
    except Exception as e:
        print(f"  Fork ERROR: {e}")

    # Step 3: Try to resume the ORIGINAL session (not fork)
    print(f"\n--- Step 3: Resume original session ---")
    resume_options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
        resume=session_id,
    )
    resume_client = ClaudeSDKClient(resume_options)

    try:
        async with resume_client:
            await resume_client.query("What is the code word? Reply with ONLY the code word.")
            async for msg in resume_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Resume: {block.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    resume_sid = msg.session_id
                    print(f"  Resume session ID: {resume_sid}")
                    print(f"  Same as original? {resume_sid == session_id}")
    except Exception as e:
        print(f"  Resume ERROR: {e}")

    # Step 4: Try SIMULTANEOUS connection
    print(f"\n--- Step 4: Simultaneous dual connection ---")
    options_a = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
        resume=session_id,
    )
    options_b = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
        resume=session_id,
        fork_session=True,
    )

    async def run_client(name, opts, prompt):
        client = ClaudeSDKClient(opts)
        texts = []
        try:
            async with client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                texts.append(block.text)
                    elif isinstance(msg, ResultMessage):
                        return {"name": name, "texts": texts, "session_id": msg.session_id, "error": None}
        except Exception as e:
            return {"name": name, "texts": texts, "session_id": None, "error": str(e)}

    results = await asyncio.gather(
        run_client("resume-A", options_a, "What code word did I tell you? Reply ONLY with the code word."),
        run_client("fork-B", options_b, "What code word did I tell you? Reply ONLY with the code word."),
    )

    for r in results:
        print(f"  {r['name']}: texts={r['texts'][:2]}, sid={r['session_id'][:12] if r['session_id'] else 'None'}..., error={r['error']}")

    # Step 5: Check if session JSONL files are readable
    print(f"\n--- Step 5: Session file inspection ---")
    project_dir = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
    session_file = project_dir / f"{session_id}.jsonl"
    if session_file.exists():
        lines = session_file.read_text().strip().split('\n')
        print(f"  Session file exists: {session_file}")
        print(f"  Lines: {len(lines)}")
        # Show first and last line structure
        if lines:
            first = json.loads(lines[0])
            print(f"  First line type: {first.get('type', '?')}")
            if len(lines) > 1:
                last = json.loads(lines[-1])
                print(f"  Last line type: {last.get('type', '?')}")
    else:
        print(f"  Session file NOT found at expected path")
        # Search for it
        for p in project_dir.iterdir():
            if p.stem == session_id:
                print(f"  Found at: {p}")

    print(f"\n=== VERDICT ===")
    if fork_session_id and fork_session_id != session_id:
        print("  Session forking WORKS — creates new session with parent context")
    if all(r.get("error") is None for r in results):
        print("  Simultaneous connections WORK — both clients connected OK")
    else:
        for r in results:
            if r.get("error"):
                print(f"  {r['name']} FAILED: {r['error']}")


if __name__ == "__main__":
    asyncio.run(main())
