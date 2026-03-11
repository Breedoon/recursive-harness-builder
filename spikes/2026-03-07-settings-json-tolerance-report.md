# Settings.json Tolerance & Custom Hooks — Spike Report

**Date**: 2026-03-07
**Spikes**: 35, 36, 37
**Goal**: Determine whether custom hooks and arbitrary data can live in `.claude/settings.json` at the project level, survive SDK loading, and be accessible at runtime.

## Executive Summary

**The Claude Code CLI is extremely tolerant of unknown content in settings.json.** You can:

1. Add arbitrary top-level keys — silently ignored
2. Add fake hook event names (e.g. `CacheExpiration`) — silently ignored, no crash
3. Mix real hooks with fake hooks — real ones fire, fake ones are ignored
4. Put invalid types for known keys (e.g. `"permissions": "string"`) — still works
5. Stash large custom payloads (~50KB) — no problem
6. Use `settings.local.json` alongside `settings.json` — both tolerated

**Real hooks defined in settings.json DO fire** alongside SDK-level Python hooks. The two systems coexist without conflict.

## Key Findings

### Finding 1: CLI Ignores Unknown Schema Elements

All 14 tolerance tests passed (spike 35). The CLI does NOT validate settings.json against a strict schema. Specifically:

| Scenario | Result |
|----------|--------|
| Empty `{}` | Works |
| Only custom keys, no standard keys | Works |
| Extra unknown top-level keys | Works |
| Fake hook event names (`CacheExpiration`, etc.) | Works (ignored) |
| Malformed hook values (string instead of array) | Works |
| Invalid types for known keys (`permissions: "string"`) | Works |
| Invalid hook type field (`nonexistent_type`) | Works |
| Large custom payload (~50KB) | Works |
| Settings as JSON string via `--settings` | Works |
| Settings as file path via `--settings` | Works |
| `settings.local.json` alongside `settings.json` | Works |

**Conclusion**: You can put anything in `settings.json` and the CLI won't break. It cherry-picks what it understands and ignores the rest.

### Finding 2: File-Based Hooks Fire at Runtime

Hooks defined in `settings.json` fire exactly as documented (spike 36):

| Hook Event | Fires? | Notes |
|------------|--------|-------|
| `SessionStart` | **YES** | Fires on session initialization |
| `PreToolUse` | **YES** | Fires before each tool use, receives tool context |
| `PostToolUse` | **YES** | (inferred from PreToolUse behavior) |
| `Stop` | **YES** | Fires on session completion |
| `PreCompact` | Accepted | Doesn't fire in short sessions (no compaction needed) |
| `Notification` | Accepted | Doesn't fire for simple queries |
| `CacheExpiration` (fake) | **Silently ignored** | No crash, no fire |

### Finding 3: SDK Hooks and File Hooks Coexist

**Both SDK hooks (Python callbacks via `ClaudeAgentOptions.hooks`) and file hooks (settings.json commands) fire independently.** Tested with PreToolUse — both the Python callback AND the settings.json command executed for the same tool use event.

This means you can:
- Use settings.json hooks for shell-based actions (writing files, sending signals)
- Use SDK hooks for Python-level logic (blocking tools, injecting context)
- Both fire for the same events without interference

### Finding 4: Hook Scripts Receive Rich Context

**On stdin**, hooks receive JSON with full context:

**SessionStart stdin:**
```json
{
  "session_id": "852a4aa4-...",
  "transcript_path": "/Users/.../.claude/projects/.../852a4aa4.jsonl",
  "cwd": "/path/to/project",
  "hook_event_name": "SessionStart",
  "source": "startup"
}
```

**PreToolUse stdin:**
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "bypassPermissions",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {"command": "echo hello", "description": "..."},
  "tool_use_id": "toolu_..."
}
```

**Environment variables available to hooks:**
- `CLAUDE_PROJECT_DIR` — path to the project directory (the `cwd`)
- `CLAUDE_CODE_ENTRYPOINT` — how the CLI was started (e.g. `sdk-py`)
- `CLAUDE_AGENT_SDK_VERSION` — SDK version
- `CLAUDE_ENV_FILE` — path to session env file

### Finding 5: Hook Scripts Can Read Custom Settings

A hook script can read `.claude/settings.json` using `CLAUDE_PROJECT_DIR`:

```bash
#!/bin/bash
SETTINGS="$CLAUDE_PROJECT_DIR/.claude/settings.json"
VALUE=$(python3 -c "import json; print(json.load(open('$SETTINGS'))['obs_agent']['cache_ttl'])")
```

This was tested and confirmed — the hook successfully read `cache_ttl: 7200` from a custom `obs_agent` namespace in settings.json.

### Finding 6: Hooks Can Inject Context Back to the Agent

Hook scripts that output JSON with `additionalContext` successfully inject that context into the agent's conversation:

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "INJECTED_CONTEXT_FROM_SETTINGS_HOOK"}}
```

The agent received and echoed the injected context verbatim.

### Finding 7: Server Info Does NOT Expose Settings

The `get_server_info()` response contains:
- `commands` — available slash commands
- `output_style` / `available_output_styles`
- `models` / `account` / `pid`

It does NOT expose the loaded settings.json content. Custom data from settings.json is not surfaced through the SDK's initialization response.

### Finding 8: No API to Read Settings From SDK Object

The SDK provides no method to read back the settings that were loaded. The `ClaudeAgentOptions.settings` field is write-only (passes to `--settings` CLI flag). To access custom settings data at runtime, you must:
1. Read the file yourself (`json.load(open('.claude/settings.json'))`)
2. Or have a hook script read it and pass data via `additionalContext`

## Recommended Architecture for Custom Hooks

Based on these findings, here's the recommended pattern for custom OBS Agent hooks:

### Pattern: Custom Namespace in settings.json

```json
{
  "permissions": {"allow": [], "deny": []},
  "obs_agent": {
    "hooks": {
      "PreCompact": {
        "handler": "obs_agent.hooks:on_compact",
        "timeout_ms": 5000
      },
      "CacheExpiration": {
        "ttl_seconds": 3600,
        "handler": "obs_agent.hooks:on_cache_expire"
      }
    },
    "version": "1.0.0"
  },
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 -c \"import json,sys; from obs_agent.hooks import on_compact; on_compact(json.load(sys.stdin))\""
          }
        ]
      }
    ]
  }
}
```

This separates:
- **`obs_agent`** — custom namespace for our config (ignored by CLI, read by our code)
- **`hooks`** — real Claude hooks that fire at runtime (PreCompact, etc.)

### Pattern: SDK + File Hook Cooperation

For hooks that need both Python logic AND shell actions:

1. **File hook** (settings.json): Fires a shell command that writes to a signal file or sends a notification
2. **SDK hook** (Python callback): Handles the same event with rich Python logic (blocking, context injection, forking)
3. Both fire independently for the same event — no conflict

### Pattern: Reading Config at Startup

```python
import json
from pathlib import Path

def load_obs_config(project_dir: str) -> dict:
    settings_path = Path(project_dir) / ".claude" / "settings.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
        return settings.get("obs_agent", {})
    return {}
```

## What Does NOT Work

1. **Fake hook events don't fire** — `CacheExpiration` in the hooks section is accepted but never triggered. You can't invent new hook events that the CLI will call.
2. **No way to read settings via SDK API** — the loaded settings aren't exposed through `get_server_info()` or any other SDK method.
3. **Hook commands run with the project as cwd** — but `CLAUDE_PROJECT_DIR` may resolve through symlinks differently than the original `cwd` you passed.

## Implications for OBS Agent

1. **PreCompact hook is the right place** for cache expiration handling — it fires when the context window fills up, which is exactly when we'd want to extract/persist memory.

2. **Custom hook config can live in settings.json** under an `obs_agent` namespace — the CLI won't touch it, and our Python code can read it directly.

3. **No need to parse settings.json just for hooks** — the SDK's Python `hooks` parameter handles our programmatic hooks. File-based hooks in settings.json are only useful for shell commands or scripts that need to run independently.

4. **For a "CacheExpiration" concept** — use `PreCompact` (fires on context compaction) combined with our own timer-based logic in the daemon. The PreCompact hook receives `trigger` ("manual" or "auto") and `custom_instructions` on stdin.

## Files

- `spikes/spike_35_settings_json_tolerance.py` — 14 tolerance tests (all pass)
- `spikes/spike_36_settings_hooks_runtime.py` — 9 runtime hook tests (7/9 pass; 2 are informational)
- `spikes/spike_37_settings_hook_data_access.py` — 5 data access tests (all pass)
