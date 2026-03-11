"""
Spike 36: Settings.json Hooks at Runtime — Do They Actually Fire?

Tests:
1. Does a real PreCompact hook from settings.json fire during compaction?
2. Does a real PreToolUse hook from settings.json fire during tool use?
3. Does a SessionStart hook from settings.json fire?
4. Can we stash custom data in settings.json and read it back from our hook scripts?
5. Do SDK-level hooks and settings.json hooks coexist?
6. Can we read settings.json ourselves from a hook script (since cwd is available)?

Key question: if we add a hook in settings.json (file-based), does it execute
alongside SDK hooks registered via ClaudeAgentOptions.hooks?
"""

import asyncio
import json
import os
import sys
import tempfile
import shutil
import stat
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

from claude_agent_sdk import ClaudeSDKClient, TextBlock
from claude_agent_sdk.types import (
    ClaudeAgentOptions, ResultMessage, HookMatcher,
    AssistantMessage
)


def make_project(name: str, settings: dict, scripts: dict[str, str] | None = None) -> str:
    """Create a temp project dir with .claude/settings.json and optional scripts."""
    base = tempfile.mkdtemp(prefix=f"spike36_{name}_")
    claude_dir = Path(base) / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))

    if scripts:
        for fname, content in scripts.items():
            script_path = Path(base) / fname
            script_path.write_text(content)
            script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    return base


async def test_session_start_hook():
    """Test 1: SessionStart hook from settings.json fires on session start."""
    print(f"\n{'='*60}")
    print("TEST 1: SessionStart hook from settings.json")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_session_start_")

    proj = make_project("session_start", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'SESSION_START_FIRED' > {marker_file}"
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

        fired = Path(marker_file).exists()
        if fired:
            content = Path(marker_file).read_text().strip()
            print(f"  PASS: SessionStart hook fired. Marker content: '{content}'")
        else:
            print(f"  FAIL: SessionStart hook did NOT fire (no marker file)")
        return fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_pretooluse_hook_from_settings():
    """Test 2: PreToolUse hook from settings.json fires on tool use."""
    print(f"\n{'='*60}")
    print("TEST 2: PreToolUse hook from settings.json fires on tool use")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_pretooluse_")

    proj = make_project("pretooluse", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo \"TOOL=$TOOL_NAME\" >> {marker_file}"
                        }
                    ]
                }
            ]
        }
    })

    try:
        opts = ClaudeAgentOptions(
            system_prompt="You must use the Bash tool to run 'echo hello'. Do that now.",
            max_turns=3,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Run 'echo hello' using the Bash tool")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        fired = Path(marker_file).exists()
        if fired:
            content = Path(marker_file).read_text().strip()
            print(f"  PASS: PreToolUse hook fired. Marker content: '{content}'")
        else:
            print(f"  FAIL: PreToolUse hook did NOT fire")
        return fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_sdk_hooks_coexist_with_settings_hooks():
    """Test 3: SDK hooks (Python callbacks) coexist with settings.json hooks."""
    print(f"\n{'='*60}")
    print("TEST 3: SDK hooks + settings.json hooks coexist")
    print(f"{'='*60}")

    settings_marker = tempfile.mktemp(prefix="spike36_settings_hook_")
    sdk_hook_fired = {"value": False, "tool_name": None}

    proj = make_project("coexist", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'SETTINGS_HOOK' >> {settings_marker}"
                        }
                    ]
                }
            ]
        }
    })

    async def sdk_hook_callback(input_data, tool_use_id, context):
        sdk_hook_fired["value"] = True
        sdk_hook_fired["tool_name"] = input_data.get("tool_name", "unknown")
        return {}  # no-op, just observe

    try:
        opts = ClaudeAgentOptions(
            system_prompt="Use the Bash tool to run 'echo test'. Do it now.",
            max_turns=3,
            permission_mode="bypassPermissions",
            cwd=proj,
            setting_sources=["project"],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[sdk_hook_callback])
                ]
            }
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Run echo test with Bash")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        settings_fired = Path(settings_marker).exists()
        if settings_fired:
            content = Path(settings_marker).read_text().strip()
            print(f"  Settings hook: FIRED (content: '{content}')")
        else:
            print(f"  Settings hook: NOT FIRED")

        print(f"  SDK hook: {'FIRED' if sdk_hook_fired['value'] else 'NOT FIRED'} "
              f"(tool: {sdk_hook_fired['tool_name']})")

        both = settings_fired and sdk_hook_fired["value"]
        print(f"  {'PASS' if both else 'PARTIAL'}: Both hooks {'fired' if both else 'did not both fire'}")
        return both
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(settings_marker).unlink(missing_ok=True)


async def test_hook_reads_settings_json():
    """Test 4: Hook script can read settings.json from cwd."""
    print(f"\n{'='*60}")
    print("TEST 4: Hook script reads custom data from settings.json")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_read_settings_")

    # Create a hook script that reads settings.json and extracts custom data
    hook_script = f"""#!/bin/bash
# Read custom data from settings.json in the project .claude dir
CUSTOM_VALUE=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d.get('obs_agent',{{}}).get('cache_ttl','NOT_FOUND'))")
echo "CUSTOM_VALUE=$CUSTOM_VALUE" > {marker_file}
"""

    proj = make_project("read_settings", {
        "permissions": {"allow": [], "deny": []},
        "obs_agent": {
            "cache_ttl": 3600,
            "hooks": {
                "on_compact": "do_something"
            }
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_script.strip().replace('\n', ' && ')
                        }
                    ]
                }
            ]
        }
    }, scripts={"read_settings.sh": hook_script})

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
            print(f"  PASS: Hook read settings.json. Content: '{content}'")
            return "3600" in content
        else:
            print(f"  FAIL: Hook did not fire or did not write marker")
            return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_stop_hook_from_settings():
    """Test 5: Stop hook from settings.json fires."""
    print(f"\n{'='*60}")
    print("TEST 5: Stop hook from settings.json fires on completion")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_stop_")

    proj = make_project("stop_hook", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'STOP_FIRED' > {marker_file}"
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

        # Give a moment for the Stop hook to fire
        await asyncio.sleep(1)

        fired = Path(marker_file).exists()
        if fired:
            content = Path(marker_file).read_text().strip()
            print(f"  PASS: Stop hook fired. Content: '{content}'")
        else:
            print(f"  FAIL: Stop hook did NOT fire")
        return fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_notification_hook():
    """Test 6: Notification hook from settings.json."""
    print(f"\n{'='*60}")
    print("TEST 6: Notification hook from settings.json")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_notification_")

    proj = make_project("notification", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "Notification": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'NOTIFICATION_FIRED' > {marker_file}"
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

        await asyncio.sleep(1)

        fired = Path(marker_file).exists()
        if fired:
            content = Path(marker_file).read_text().strip()
            print(f"  PASS: Notification hook fired. Content: '{content}'")
        else:
            print(f"  INFO: Notification hook did NOT fire (may not trigger for simple queries)")
        return fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_fake_hook_event_ignored():
    """Test 7: Fake hook events are silently ignored (don't crash, don't fire)."""
    print(f"\n{'='*60}")
    print("TEST 7: Fake hook events are silently ignored")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_fake_")

    proj = make_project("fake_ignored", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "CacheExpiration": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'FAKE_FIRED' > {marker_file}"
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

        await asyncio.sleep(0.5)

        fired = Path(marker_file).exists()
        if not fired:
            print(f"  PASS: Fake hook event 'CacheExpiration' was silently ignored (no crash, no fire)")
        else:
            print(f"  UNEXPECTED: Fake hook actually fired?!")
        return not fired
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_precompact_hook():
    """Test 8: PreCompact hook — hard to trigger naturally, but test it's accepted."""
    print(f"\n{'='*60}")
    print("TEST 8: PreCompact hook accepted (may not fire in short session)")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_precompact_")

    proj = make_project("precompact", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"echo 'PRECOMPACT_FIRED' > {marker_file}"
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
        # Session should start fine even with PreCompact hook
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Say OK")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

        print(f"  PASS: Session ran successfully with PreCompact hook in settings.json")
        print(f"  (PreCompact fires on context compaction, not in short sessions)")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def test_hook_receives_env_vars():
    """Test 9: Hook script receives hook-specific env vars (HOOK_EVENT, TOOL_NAME, etc.)."""
    print(f"\n{'='*60}")
    print("TEST 9: Hook script receives env vars")
    print(f"{'='*60}")

    marker_file = tempfile.mktemp(prefix="spike36_env_")

    proj = make_project("env_vars", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"env | grep -i 'HOOK\\|SESSION\\|CLAUDE\\|CWD\\|TRANSCRIPT' > {marker_file} 2>&1 || echo 'NO_HOOK_VARS' > {marker_file}"
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
            content = Path(marker_file).read_text().strip()
            print(f"  Hook env vars (first 500 chars):")
            for line in content.split('\n')[:15]:
                print(f"    {line}")
            return True
        else:
            print(f"  FAIL: Hook did not fire")
            return False
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        Path(marker_file).unlink(missing_ok=True)


async def main():
    results = {}

    results["1_session_start"] = await test_session_start_hook()
    results["2_pretooluse"] = await test_pretooluse_hook_from_settings()
    results["3_coexist"] = await test_sdk_hooks_coexist_with_settings_hooks()
    results["4_read_settings"] = await test_hook_reads_settings_json()
    results["5_stop"] = await test_stop_hook_from_settings()
    results["6_notification"] = await test_notification_hook()
    results["7_fake_ignored"] = await test_fake_hook_event_ignored()
    results["8_precompact"] = await test_precompact_hook()
    results["9_env_vars"] = await test_hook_receives_env_vars()

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
