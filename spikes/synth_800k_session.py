"""
Spike: Synthesize a ~800K token session JSONL, then resume it with sonnet[1m]
to test compaction behavior near the 1M boundary.

Instead of building up context turn by turn (expensive, rate-limited),
we craft a session file directly and resume from it.
"""
import asyncio
import json
import sys
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

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

# Session storage
PROJ_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"

_PARAGRAPH = (
    "The history of computing is a fascinating journey that spans several decades. "
    "From the earliest mechanical calculators to modern quantum processors, each era "
    "brought innovations that transformed how humans interact with information. Charles "
    "Babbage conceived the Analytical Engine in the 1830s, establishing principles that "
    "would guide computer design for over a century. Ada Lovelace, working alongside "
    "Babbage, wrote what many consider the first computer program. The twentieth century "
    "saw the development of electronic computers, starting with machines like ENIAC and "
    "UNIVAC. These room-sized devices used vacuum tubes and consumed enormous amounts of "
    "power. The invention of the transistor at Bell Labs revolutionized the field, making "
    "computers smaller, faster, and more reliable. The integrated circuit further "
    "accelerated this trend, packing thousands and eventually billions of transistors onto "
    "tiny silicon chips. Personal computers emerged in the 1970s and 1980s, bringing "
    "computational power to homes and offices worldwide. "
)


def estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token per 4 chars."""
    return len(text) // 4


def create_session_jsonl(session_id: str, target_tokens: int = 800_000) -> Path:
    """Create a synthetic session JSONL with ~target_tokens of context."""
    filepath = PROJ_DIR / f"{session_id}.jsonl"
    lines = []
    now = datetime.now(timezone.utc).isoformat()

    total_tokens = 0
    turn_num = 0
    prev_uuid = None

    # Each turn: user message (~25K tokens) + assistant response (~5 tokens)
    # ~25K tokens = ~100K chars of text
    padding = _PARAGRAPH * 100  # ~80K chars = ~20K tokens per message
    tokens_per_turn = estimate_tokens(padding) + 50  # padding + overhead

    num_turns = target_tokens // tokens_per_turn

    print(f"Creating session {session_id}")
    print(f"  Target: {target_tokens:,} tokens")
    print(f"  Padding per message: {len(padding):,} chars (~{estimate_tokens(padding):,} tokens)")
    print(f"  Turns needed: {num_turns}")

    for i in range(num_turns):
        msg_uuid = str(uuid.uuid4())

        # User message
        user_content = (
            f"[{i+1}] This is message {i+1} in a context window capacity test. "
            f"Reply with 'OK {i+1}'.\n\n"
            f"Reference material (section {i+1}):\n{padding}"
        )

        user_line = {
            "parentUuid": prev_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/Users/breedoon/Documents/obs",
            "sessionId": session_id,
            "version": "2.1.59",
            "gitBranch": "main",
            "type": "user",
            "message": {
                "role": "user",
                "content": user_content,
            },
            "uuid": msg_uuid,
            "timestamp": now,
            "permissionMode": "bypassPermissions",
        }
        lines.append(json.dumps(user_line))

        # Assistant response
        asst_uuid = str(uuid.uuid4())
        asst_line = {
            "parentUuid": msg_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/Users/breedoon/Documents/obs",
            "sessionId": session_id,
            "version": "2.1.59",
            "gitBranch": "main",
            "message": {
                "model": "claude-sonnet-4-6-20250514",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": f"OK {i+1}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": estimate_tokens(user_content),
                    "cache_read_input_tokens": total_tokens,
                    "output_tokens": 5,
                },
            },
            "uuid": asst_uuid,
            "timestamp": now,
        }
        lines.append(json.dumps(asst_line))

        # Result message
        result_line = {
            "parentUuid": asst_uuid,
            "isSidechain": False,
            "type": "result",
            "subtype": "success",
            "duration_ms": 1500,
            "duration_api_ms": 1200,
            "is_error": False,
            "num_turns": 1,
            "result": f"OK {i+1}",
            "session_id": session_id,
            "cost_usd": 0.01,
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": estimate_tokens(user_content),
                "cache_read_input_tokens": total_tokens,
                "output_tokens": 5,
            },
            "uuid": str(uuid.uuid4()),
            "timestamp": now,
        }
        lines.append(json.dumps(result_line))

        total_tokens += estimate_tokens(user_content) + 10
        prev_uuid = asst_uuid
        turn_num = i + 1

        if (i + 1) % 5 == 0:
            print(f"  Turn {i+1}: ~{total_tokens:,} tokens cumulative")

    filepath.write_text("\n".join(lines) + "\n")
    file_size = filepath.stat().st_size
    print(f"\n  Created: {filepath}")
    print(f"  File size: {file_size:,} bytes ({file_size/1024/1024:.1f} MB)")
    print(f"  Total turns: {turn_num}")
    print(f"  Estimated tokens: {total_tokens:,}")
    return filepath


def get_total_context(usage: dict) -> int:
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


async def test_resume(session_id: str, model: str, label: str, extra_opts: dict = None):
    """Resume a session and send a few more messages to test compaction."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"  Resuming session {session_id} with model={model}")
    print(f"{'='*70}", flush=True)

    compact_events = []
    post_compact_summaries = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"  *** PreCompact! trigger={hook_input.get('trigger')} ***", flush=True)
        return {"continue_": True}

    async def on_post_compact(hook_input, tool_use_id, context):
        data = dict(hook_input)
        post_compact_summaries.append(data)
        summary = data.get("compact_summary", "")
        print(f"  *** PostCompact! summary={len(summary)} chars ***", flush=True)
        return {"continue_": True}

    opts_dict = dict(
        model=model,
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
    if extra_opts:
        opts_dict.update(extra_opts)

    opts = ClaudeAgentOptions(**opts_dict)

    error_msg = None
    last_total = 0

    try:
        async with ClaudeSDKClient(opts) as client:
            # Send 5 more messages to see behavior
            padding = _PARAGRAPH * 100
            for i in range(5):
                prompt = (
                    f"[RESUME-{i+1}] Reply 'RESUME OK {i+1}'.\n\n"
                    f"Additional reference:\n{padding}"
                )
                try:
                    await client.query(prompt)
                    text = ""
                    usage_data = None
                    compacted = False

                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    text += block.text
                        elif isinstance(msg, SystemMessage):
                            if msg.subtype == "compact_boundary":
                                meta = msg.data.get("compact_metadata", {})
                                compacted = True
                                print(f"  Resume {i+1}: *** COMPACTED *** pre_tokens={meta.get('pre_tokens'):,}", flush=True)
                            elif msg.subtype not in ("init",):
                                print(f"  Resume {i+1}: SystemMsg: {msg.subtype}", flush=True)
                        elif isinstance(msg, ResultMessage):
                            usage_data = msg.usage
                            if msg.is_error:
                                err = str(msg.result)[:300]
                                if "rate limit" in err.lower():
                                    print(f"  Resume {i+1}: Rate limited, waiting 60s...", flush=True)
                                    await asyncio.sleep(60)
                                    continue
                                error_msg = err
                                print(f"  Resume {i+1}: ERROR: {err}", flush=True)

                    if usage_data:
                        total = get_total_context(usage_data)
                        last_total = total
                        print(f"  Resume {i+1}: {total:,} tokens | '{text.strip()[:30]}'", flush=True)

                    if error_msg or compacted:
                        break

                except Exception as e:
                    if "rate limit" in str(e).lower():
                        print(f"  Resume {i+1}: Rate limited, waiting 60s...", flush=True)
                        await asyncio.sleep(60)
                        continue
                    error_msg = str(e)[:200]
                    print(f"  Resume {i+1}: {type(e).__name__}: {error_msg}", flush=True)
                    break

    except Exception as e:
        error_msg = str(e)[:200]
        print(f"  CLIENT ERROR: {e}", flush=True)

    compacted = bool(compact_events)
    status = "COMPACTED" if compacted else ("ERROR" if error_msg else "NO COMPACTION")
    print(f"\n  RESULT: {status} | {last_total:,} tokens | compact_events={len(compact_events)} | post_compact={len(post_compact_summaries)}", flush=True)

    return {
        "label": label,
        "session_id": session_id,
        "model": model,
        "last_total_tokens": last_total,
        "compacted": compacted,
        "compact_events": len(compact_events),
        "post_compact_summaries": len(post_compact_summaries),
        "error": error_msg,
    }


async def main():
    print(f"=== Synthetic 800K Session Spike ===")
    print(f"Started: {datetime.now().isoformat()}\n")

    # Step 1: Create a synthetic ~800K token session
    session_id = str(uuid.uuid4())
    create_session_jsonl(session_id, target_tokens=800_000)

    # Step 2: Resume with sonnet[1m] — does it compact?
    r1 = await test_resume(
        session_id, "sonnet[1m]",
        "Resume 800K session with sonnet[1m] (no extra flags)",
    )

    # Step 3: If it compacted, try forking with different settings
    if r1["compacted"]:
        # Fork and try with higher window
        r2 = await test_resume(
            session_id, "sonnet[1m]",
            "Resume 800K + WINDOW=999999 + PCT=99",
            extra_opts={
                "fork_session": True,
                "env": {
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "999999",
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "99",
                },
            },
        )

    # Final report
    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}")
    print(f"  Session: {session_id}")
    print(f"  {r1['label']}: {'COMPACTED' if r1['compacted'] else 'NO COMPACTION'} at {r1['last_total_tokens']:,} tokens")
    if r1.get("error"):
        print(f"    Error: {r1['error']}")

    # Cleanup: remove synthetic session
    session_file = PROJ_DIR / f"{session_id}.jsonl"
    # Don't delete yet — might want to reuse
    print(f"\n  Session file kept at: {session_file}")
    print(f"  To delete: rm '{session_file}'")


if __name__ == "__main__":
    asyncio.run(main())
