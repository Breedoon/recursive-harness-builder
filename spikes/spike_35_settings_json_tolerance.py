"""
Spike 35: Settings.json Tolerance Investigation

Tests whether Claude Code CLI / SDK tolerates:
1. Valid settings.json with project-level setting_sources
2. Extra/unknown keys in settings.json
3. Fake hook event types in hooks
4. Custom metadata stashed in settings.json
5. Settings passed as JSON string via SDK settings option
6. Whether settings are accessible from the SDK session object
7. Malformed hook values, invalid types, etc.

Goal: Determine if we can put custom hook definitions (e.g. PreCompact callback config)
in .claude/settings.json at the project level and have them survive SDK loading.
"""

import asyncio
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

# Unset CLAUDECODE to avoid SDK issues when running from Claude Code
os.environ.pop("CLAUDECODE", None)

from claude_agent_sdk import ClaudeSDKClient, TextBlock
from claude_agent_sdk.types import ClaudeAgentOptions, ResultMessage


async def run_query(label: str, cwd: str, settings: str | None = None,
                    setting_sources: list[str] | None = None) -> dict:
    """Run a minimal SDK query and return result info."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    opts = ClaudeAgentOptions(
        system_prompt="Respond with exactly one word: SETTINGS_OK. Nothing else.",
        max_turns=1,
        permission_mode="bypassPermissions",
        cwd=cwd,
    )
    if settings is not None:
        opts.settings = settings
    if setting_sources is not None:
        opts.setting_sources = setting_sources

    result_info = {"label": label, "success": False, "error": None}

    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Say SETTINGS_OK")
            text = ""
            async for msg in client.receive_response():
                if hasattr(msg, "content"):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                if isinstance(msg, ResultMessage):
                    result_info["session_id"] = msg.session_id
            result_info["success"] = True
            result_info["response_preview"] = text[:100]
            print(f"  SUCCESS: {text[:80]}")
    except Exception as e:
        result_info["error"] = str(e)
        print(f"  FAILED: {e}")

    return result_info


def make_project(name: str, settings: dict) -> str:
    """Create a temp project dir with .claude/settings.json."""
    base = tempfile.mkdtemp(prefix=f"spike35_{name}_")
    claude_dir = Path(base) / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"  Project dir: {base}")
    print(f"  settings.json: {json.dumps(settings, indent=2)[:200]}")
    return base


async def main():
    results = []
    temp_dirs = []

    # ============================================================
    # TEST 1: Baseline - valid minimal settings.json, project sources
    # ============================================================
    proj1 = make_project("valid_minimal", {
        "permissions": {
            "allow": [],
            "deny": []
        }
    })
    temp_dirs.append(proj1)
    r1 = await run_query(
        "1. Valid minimal settings.json + setting_sources=['project']",
        cwd=proj1,
        setting_sources=["project"]
    )
    results.append(r1)

    # ============================================================
    # TEST 2: Extra unknown top-level keys
    # ============================================================
    proj2 = make_project("extra_toplevel", {
        "permissions": {"allow": [], "deny": []},
        "myCustomField": "hello",
        "obs_hooks": {
            "PreCompact": {"script": "/path/to/handler.py", "timeout": 30}
        },
        "randomJunk": [1, 2, 3],
        "deeply": {"nested": {"custom": {"data": True}}}
    })
    temp_dirs.append(proj2)
    r2 = await run_query(
        "2. Extra unknown top-level keys in settings.json",
        cwd=proj2,
        setting_sources=["project"]
    )
    results.append(r2)

    # ============================================================
    # TEST 3: Fake hook event types in hooks section
    # ============================================================
    proj3 = make_project("fake_hooks", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo 'pre-compact fired'"
                        }
                    ]
                }
            ],
            "CacheExpiration": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo 'cache expired'"
                        }
                    ]
                }
            ],
            "CustomHookThatDoesNotExist": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo 'custom hook'"
                        }
                    ]
                }
            ]
        }
    })
    temp_dirs.append(proj3)
    r3 = await run_query(
        "3. Fake hook event types (CacheExpiration, CustomHookThatDoesNotExist)",
        cwd=proj3,
        setting_sources=["project"]
    )
    results.append(r3)

    # ============================================================
    # TEST 4: Malformed hooks (wrong structure)
    # ============================================================
    proj4 = make_project("malformed_hooks", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": "not_an_array",
            "PostToolUse": {"wrong": "structure"}
        }
    })
    temp_dirs.append(proj4)
    r4 = await run_query(
        "4. Malformed hook values (string instead of array)",
        cwd=proj4,
        setting_sources=["project"]
    )
    results.append(r4)

    # ============================================================
    # TEST 5: Totally invalid JSON type for known keys
    # ============================================================
    proj5 = make_project("invalid_types", {
        "permissions": "not_an_object",
        "hooks": 42
    })
    temp_dirs.append(proj5)
    r5 = await run_query(
        "5. Invalid types for known keys (permissions=string, hooks=number)",
        cwd=proj5,
        setting_sources=["project"]
    )
    results.append(r5)

    # ============================================================
    # TEST 6: Settings passed as JSON string via SDK (not file)
    # ============================================================
    proj6 = tempfile.mkdtemp(prefix="spike35_json_string_")
    temp_dirs.append(proj6)
    settings_json = json.dumps({
        "permissions": {"allow": [], "deny": []},
        "myCustomData": {"hook_config": {"PreCompact": "handler.py"}},
        "hooks": {
            "FakeEvent": [{"matcher": "", "hooks": [{"type": "command", "command": "echo fake"}]}]
        }
    })
    r6 = await run_query(
        "6. Settings as JSON string with custom data + fake hooks",
        cwd=proj6,
        settings=settings_json,
        setting_sources=["project"]
    )
    results.append(r6)

    # ============================================================
    # TEST 7: Empty settings.json
    # ============================================================
    proj7 = make_project("empty_settings", {})
    temp_dirs.append(proj7)
    r7 = await run_query(
        "7. Empty settings.json ({})",
        cwd=proj7,
        setting_sources=["project"]
    )
    results.append(r7)

    # ============================================================
    # TEST 8: settings.json with only custom keys (no standard keys)
    # ============================================================
    proj8 = make_project("custom_only", {
        "obs_agent": {
            "hooks": {
                "PreCompact": {
                    "handler": "obs_agent.hooks:on_pre_compact",
                    "timeout_ms": 5000
                },
                "CacheExpiration": {
                    "handler": "obs_agent.hooks:on_cache_expire",
                    "ttl_seconds": 3600
                }
            },
            "version": "1.0.0"
        }
    })
    temp_dirs.append(proj8)
    r8 = await run_query(
        "8. Only custom keys, no standard Claude settings",
        cwd=proj8,
        setting_sources=["project"]
    )
    results.append(r8)

    # ============================================================
    # TEST 9: Valid hooks + custom hooks side by side
    # ============================================================
    proj9 = make_project("valid_plus_custom", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "echo 'real hook'"}
                    ]
                }
            ],
            "CacheExpiration": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "echo 'fake'"}
                    ]
                }
            ]
        }
    })
    temp_dirs.append(proj9)
    r9 = await run_query(
        "9. Valid hooks (PreToolUse) + fake hooks (CacheExpiration) together",
        cwd=proj9,
        setting_sources=["project"]
    )
    results.append(r9)

    # ============================================================
    # TEST 10: Hook with invalid type field
    # ============================================================
    proj10 = make_project("invalid_hook_type", {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "nonexistent_type", "command": "echo bad"}
                    ]
                }
            ]
        }
    })
    temp_dirs.append(proj10)
    r10 = await run_query(
        "10. Hook with invalid type field (nonexistent_type instead of command)",
        cwd=proj10,
        setting_sources=["project"]
    )
    results.append(r10)

    # ============================================================
    # TEST 11: Large custom payload
    # ============================================================
    big_custom = {f"key_{i}": f"value_{i}" * 100 for i in range(50)}
    proj11 = make_project("huge_custom", {
        "permissions": {"allow": [], "deny": []},
        "obs_agent_config": big_custom
    })
    temp_dirs.append(proj11)
    r11 = await run_query(
        "11. Large custom payload (~50KB) in settings.json",
        cwd=proj11,
        setting_sources=["project"]
    )
    results.append(r11)

    # ============================================================
    # TEST 12: settings.json passed as file path via --settings
    # ============================================================
    proj12 = make_project("file_path_settings", {
        "permissions": {"allow": [], "deny": []},
        "obs_custom": {"key": "from_file_path"}
    })
    temp_dirs.append(proj12)
    settings_file = str(Path(proj12) / ".claude" / "settings.json")
    r12 = await run_query(
        "12. Settings as file path (--settings /path/to/settings.json)",
        cwd=proj12,
        settings=settings_file,
        setting_sources=["project"]
    )
    results.append(r12)

    # ============================================================
    # TEST 13: Inspect SDK objects for settings access
    # ============================================================
    print(f"\n{'='*60}")
    print("TEST 13: Inspect SDK objects for settings access")
    print(f"{'='*60}")

    proj13 = make_project("inspect_sdk", {
        "permissions": {"allow": [], "deny": []},
        "obs_custom": {"key": "value123"}
    })
    temp_dirs.append(proj13)

    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        opts = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj13,
            setting_sources=["project"],
        )

        transport = SubprocessCLITransport(prompt="test", options=opts)
        cmd = transport._build_command()
        print(f"  CLI command: {' '.join(cmd)}")

        # Check if settings file path option works
        settings_path = str(Path(proj13) / ".claude" / "settings.json")
        opts2 = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj13,
            settings=settings_path,
            setting_sources=["project"],
        )
        transport2 = SubprocessCLITransport(prompt="test", options=opts2)
        cmd2 = transport2._build_command()
        si = cmd2.index("--settings") if "--settings" in cmd2 else -1
        if si >= 0:
            print(f"  --settings value: {cmd2[si+1]}")

        # Check if settings as JSON string works
        opts3 = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj13,
            settings=json.dumps({"obs_custom": {"key": "value123"}}),
            setting_sources=["project"],
        )
        transport3 = SubprocessCLITransport(prompt="test", options=opts3)
        cmd3 = transport3._build_command()
        si3 = cmd3.index("--settings") if "--settings" in cmd3 else -1
        if si3 >= 0:
            print(f"  --settings JSON: {cmd3[si3+1][:100]}")

        print(f"  SUCCESS: SDK transport builds commands fine")
        results.append({"label": "13. SDK object inspection", "success": True, "error": None})
    except Exception as e:
        print(f"  FAILED: {e}")
        results.append({"label": "13. SDK object inspection", "success": False, "error": str(e)})

    # ============================================================
    # TEST 14: Check if server_info / initialization result exposes settings
    # ============================================================
    print(f"\n{'='*60}")
    print("TEST 14: Check initialization result for settings data")
    print(f"{'='*60}")

    proj14 = make_project("init_result", {
        "permissions": {"allow": [], "deny": []},
        "obs_custom": {"key": "value_in_init"}
    })
    temp_dirs.append(proj14)

    try:
        opts14 = ClaudeAgentOptions(
            system_prompt="Say OK",
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=proj14,
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=opts14) as client:
            info = await client.get_server_info()
            print(f"  Server info keys: {list(info.keys()) if info else 'None'}")
            if info:
                # Pretty print first 500 chars
                info_str = json.dumps(info, indent=2, default=str)
                print(f"  Server info (first 500 chars): {info_str[:500]}")

            # Send a query to make sure the session is valid
            await client.query("Say OK")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    print(f"  Session ID: {msg.session_id}")

            results.append({"label": "14. Init result inspection", "success": True, "error": None})
    except Exception as e:
        print(f"  FAILED: {e}")
        results.append({"label": "14. Init result inspection", "success": False, "error": str(e)})

    # ============================================================
    # SUMMARY
    # ============================================================
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        status = "PASS" if r["success"] else "FAIL"
        err = f" — {r['error'][:80]}" if r.get("error") else ""
        print(f"  [{status}] {r['label']}{err}")

    passed = sum(1 for r in results if r["success"])
    total = len(results)
    print(f"\n  {passed}/{total} tests passed")

    # Cleanup
    for d in temp_dirs:
        shutil.rmtree(d, ignore_errors=True)

    return results


if __name__ == "__main__":
    results = asyncio.run(main())
