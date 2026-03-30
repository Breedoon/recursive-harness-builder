"""
Spike: Debug WHY setting_sources=["project"] doesn't pick up autoCompactEnabled=false.

Hypothesis: the SDK resolves "project" settings from CWD/.claude/settings.json.
Maybe the CWD is wrong, or the SDK looks in a different location.

Tests:
1. Set CWD to vault path, setting_sources=["project"]
2. Create a .claude/settings.json in a temp dir, set CWD there
3. Use setting_sources=["user","project"] to confirm user source works
4. Put autoCompactEnabled=false in global config permanently, use just ["user"]
"""
import asyncio
import json
import sys
import os
import tempfile
from pathlib import Path

os.environ.pop("CLAUDECODE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    SystemMessage,
    ResultMessage,
)

VAULT_PATH = Path(os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
))

padding = "ABCDEFGHIJ" * 3000


async def test(label: str, extra_opts: dict, max_turns: int = 18):
    compact_fired = False

    async def on_pre_compact(hook_input, tool_use_id, context):
        nonlocal compact_fired
        compact_fired = True
        print(f"    *** PreCompact FIRED! ***", flush=True)
        return {"continue_": True}

    opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply ONLY 'OK'.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={"PreCompact": [HookMatcher(matcher=None, hooks=[on_pre_compact])]},
        thinking={"type": "disabled"},
        **extra_opts,
    )

    print(f"\n{'='*60}", flush=True)
    print(f"TEST: {label}", flush=True)
    print(f"{'='*60}", flush=True)

    last_total = 0
    try:
        async with ClaudeSDKClient(opts) as client:
            for i in range(max_turns):
                await client.query(f"[{i+1}] OK. {padding}")
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
                        print(f"  COMPACTED at turn {i+1}", flush=True)
                        compact_fired = True
                    elif isinstance(msg, ResultMessage) and msg.usage:
                        total = sum(msg.usage.get(k, 0) for k in ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
                        last_total = total
                        if i % 3 == 0:
                            print(f"  Turn {i+1}: {total:,} tokens", flush=True)
                        if msg.is_error:
                            print(f"  ERROR at {i+1}: {str(msg.result)[:100]}", flush=True)
                            return compact_fired, last_total
                if compact_fired:
                    break
                if last_total > 190000:
                    print(f"  {last_total:,} tokens — no compaction!", flush=True)
                    break
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    status = "COMPACTED" if compact_fired else "NO COMPACTION"
    print(f"  → {status} at {last_total:,} tokens", flush=True)
    return compact_fired, last_total


async def main():
    print("=== Debug: Why project settings don't disable compaction ===\n", flush=True)

    # Test 1: Create a clean temp dir with .claude/settings.json
    tmpdir = Path(tempfile.mkdtemp(prefix="compaction_test_"))
    claude_dir = tmpdir / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"autoCompactEnabled": False}))
    print(f"Created {settings} with autoCompactEnabled=false", flush=True)

    c1, _ = await test(
        f"setting_sources=['project'], cwd={tmpdir}",
        {"setting_sources": ["project"], "cwd": str(tmpdir)},
    )

    # Test 2: Same but with additional CLAUDE.md
    claude_md = tmpdir / "CLAUDE.md"
    claude_md.write_text("# Test project\n")
    c2, _ = await test(
        f"project with CLAUDE.md, cwd={tmpdir}",
        {"setting_sources": ["project"], "cwd": str(tmpdir)},
    )

    # Test 3: Vault path but add "user" to sources + set global config
    claude_json = Path.home() / ".claude.json"
    existing = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    existing["autoCompactEnabled"] = False
    claude_json.write_text(json.dumps(existing, indent=2))

    c3, _ = await test(
        "vault cwd, setting_sources=['user','project']",
        {"setting_sources": ["user", "project"], "cwd": str(VAULT_PATH)},
    )

    # Clean up global config
    existing.pop("autoCompactEnabled", None)
    claude_json.write_text(json.dumps(existing, indent=2))

    # Test 4: Check if there's a difference between "settings" file and "setting_sources"
    # Use a standalone settings file with ONLY autoCompactEnabled
    standalone = Path(tempfile.mktemp(suffix=".json"))
    standalone.write_text(json.dumps({"autoCompactEnabled": False}))

    c4, _ = await test(
        f"settings={standalone} (file path, no sources)",
        {"settings": str(standalone)},
    )
    standalone.unlink(missing_ok=True)

    # Summary
    print(f"\n{'='*60}", flush=True)
    print("SUMMARY", flush=True)
    print(f"  1 (clean tmpdir + project): {'COMPACTED' if c1 else 'NO COMPACT'}", flush=True)
    print(f"  2 (tmpdir + CLAUDE.md + project): {'COMPACTED' if c2 else 'NO COMPACT'}", flush=True)
    print(f"  3 (vault + user+project): {'COMPACTED' if c3 else 'NO COMPACT'}", flush=True)
    print(f"  4 (standalone file): {'COMPACTED' if c4 else 'NO COMPACT'}", flush=True)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
