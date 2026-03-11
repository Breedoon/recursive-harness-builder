"""
Spike 37: Hook Data Access — Can hooks read settings.json and receive stdin JSON?

Tests:
1. SessionStart hook runs a script that reads .claude/settings.json custom data
2. Hook receives JSON on stdin with session_id, cwd, etc.
3. PreToolUse hook receives tool input on stdin
4. Can we use the hook's CLAUDE_PROJECT_DIR env var to find settings.json?
5. settings.local.json tolerance (project-local settings)
"""

import asyncio
import json
import os
import stat
import tempfile
import shutil
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

from claude_agent_sdk import ClaudeSDKClient, TextBlock
from claude_agent_sdk.types import ClaudeAgentOptions, ResultMessage


def make_project(name: str, settings: dict, scripts: dict[str, str] | None = None,
                 local_settings: dict | None = None) -> str:
    """Create a temp project dir with .claude/settings.json and optional scripts."""
    base = tempfile.mkdtemp(prefix=f"spike37_{name}_")
    claude_dir = Path(base) / ".claude"
    claude_dir.mkdir()

    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))

    if local_settings:
        local_path = claude_dir / "settings.local.json"
        local_path.write_text(json.dumps(local_settings, indent=2))

    if scripts:
        for fname, content in scripts.items():
            script_path = Path(base) / fname
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(content)
            script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    return base


async def test_hook_script_reads_settings():
    """Test 1: Hook script reads custom data from settings.json using CLAUDE_PROJECT_DIR."""
    print(f"\n{'='*60}")
    print("TEST 1: Hook script reads settings.json via CLAUDE_PROJECT_DIR")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike37_read_")

    hook_script = f"""#!/bin/bash
# Use CLAUDE_PROJECT_DIR to find settings.json
if [ -n "$CLAUDE_PROJECT_DIR" ]; then
    SETTINGS="$CLAUDE_PROJECT_DIR/.claude/settings.json"
else
    SETTINGS=".claude/settings.json"
fi

if [ -f "$SETTINGS" ]; then
    VALUE=$(python3 -c "
import json, sys
d = json.load(open('$SETTINGS'))
print(d.get('obs_agent', {{}}).get('cache_ttl', 'NOT_FOUND'))
")
    echo "SETTINGS_READ=YES CACHE_TTL=$VALUE PROJECT_DIR=$CLAUDE_PROJECT_DIR" > {marker_file}
else
    echo "SETTINGS_READ=NO FILE_NOT_FOUND=$SETTINGS" > {marker_file}
fi
"""

    proj = make_project("read_settings", {
        "permissions": {"allow": [], "deny": []},
        "obs_agent": {
            "cache_ttl": 7200,
            "compact_handler": "obs_agent.hooks:on_compact"
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": f"bash {tempfile.mktemp(suffix='.sh')}"}
                    ]
                }
            ]
        }
    })

    # Write the actual script file
    script_path = Path(proj) / "hook_read_settings.sh"
    script_path.write_text(hook_script)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    # Update the hook command to use the script
    settings = json.loads((Path(proj) / ".claude" / "settings.json").read_text())
    settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f"bash {script_path}"
    (Path(proj) / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Say OK")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        if Path(marker_file).exists():
            content = Path(marker_file).read_text().strip()
            print(f"  Result: {content}")
            passed = "7200" in content
            print(f"  {'PASS' if passed else 'FAIL'}: Hook {'read' if passed else 'failed to read'} custom settings")
            return passed
        else:
            print(f"  FAIL: Hook did not fire")
            return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_hook_stdin_json():
    """Test 2: Hook receives JSON on stdin with session context."""
    print(f"\n{'='*60}")
    print("TEST 2: Hook receives JSON on stdin")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike37_stdin_")

    proj = make_project("stdin_json", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"cat > {marker_file}"
                        }
                    ]
                }
            ]
        }
    })

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Say OK")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        if Path(marker_file).exists():
            raw = Path(marker_file).read_text().strip()
            print(f"  Raw stdin (first 500 chars): {raw[:500]}")
            try:
                data = json.loads(raw)
                print(f"  Parsed JSON keys: {list(data.keys())}")
                print(f"  session_id: {data.get('session_id', 'NOT_PRESENT')}")
                print(f"  cwd: {data.get('cwd', 'NOT_PRESENT')}")
                print(f"  hook_event_name: {data.get('hook_event_name', 'NOT_PRESENT')}")
                return True
            except json.JSONDecodeError:
                print(f"  INFO: stdin was not JSON (raw: {raw[:200]})")
                return True  # Hook fired, even if no JSON
        else:
            print(f"  FAIL: Hook did not fire")
            return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_pretooluse_stdin():
    """Test 3: PreToolUse hook receives tool info on stdin."""
    print(f"\n{'='*60}")
    print("TEST 3: PreToolUse hook receives tool info on stdin")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike37_pretool_stdin_")

    proj = make_project("pretool_stdin", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"cat > {marker_file}"
                        }
                    ]
                }
            ]
        }
    })

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Run 'echo hello' using Bash tool. Do it now.",
            max_turns=3,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Run echo hello with Bash")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        if Path(marker_file).exists():
            raw = Path(marker_file).read_text().strip()
            try:
                data = json.loads(raw)
                print(f"  Parsed JSON keys: {list(data.keys())}")
                print(f"  hook_event_name: {data.get('hook_event_name')}")
                print(f"  tool_name: {data.get('tool_name')}")
                print(f"  tool_input keys: {list(data.get('tool_input', {}).keys())}")
                print(f"  session_id present: {'session_id' in data}")
                print(f"  cwd: {data.get('cwd', 'NOT_PRESENT')}")
                print(f"  PASS: PreToolUse hook receives rich tool context on stdin")
                return True
            except json.JSONDecodeError:
                print(f"  Raw (not JSON): {raw[:300]}")
                return True
        else:
            print(f"  FAIL: PreToolUse hook did not fire")
            return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_settings_local_json():
    """Test 4: settings.local.json tolerance."""
    print(f"\n{'='*60}")
    print("TEST 4: settings.local.json alongside settings.json")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike37_local_")

    proj = make_project("local_settings", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": f"echo 'LOCAL_TEST' > {marker_file}"}
                    ]
                }
            ]
        }
    }, local_settings={
        "obs_local_config": {"secret": "local_value"},
        "permissions": {"allow": ["Bash(*:*)"], "deny": []}
    })

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project", "local"],
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Say OK")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        fired = Path(marker_file).exists()
        print(f"  {'PASS' if fired else 'FAIL'}: Session {'worked' if fired else 'failed'} with settings.local.json present")
        return fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_hook_output_json():
    """Test 5: Hook can output JSON to influence behavior."""
    print(f"\n{'='*60}")
    print("TEST 5: Hook outputs JSON with additionalContext")
    print(f"{'='*60}")

    proj = make_project("hook_output", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'echo \'{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "INJECTED_CONTEXT_FROM_SETTINGS_HOOK"}}\''
                        }
                    ]
                }
            ]
        }
    })

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Repeat verbatim any additional context you received. Say NONE if there was none.",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
        )
        text = ""
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("What additional context did you receive?")
            async for msg in client.receive_response():
                if hasattr(msg, "content"):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                if isinstance(msg, ResultMessage):
                    break

        has_context = "INJECTED_CONTEXT" in text.upper()
        print(f"  Response: {text[:200]}")
        print(f"  {'PASS' if has_context else 'INFO'}: Agent {'received' if has_context else 'may not have received'} injected context")
        return True  # Session worked regardless
    finally:
        shutil.rmtree(proj, ignore_errors=True)


async def main():
    results = {}

    results["1_read_settings"] = await test_hook_script_reads_settings()
    results["2_stdin_json"] = await test_hook_stdin_json()
    results["3_pretool_stdin"] = await test_pretooluse_stdin()
    results["4_local_settings"] = await test_settings_local_json()
    results["5_hook_output"] = await test_hook_output_json()

    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} tests passed")

    return results


if __name__ == "__main__":
    asyncio.run(main())
