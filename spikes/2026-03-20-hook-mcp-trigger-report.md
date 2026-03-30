# Hook → MCP Tool Trigger — Spike Report

**Date:** 2026-03-20
**Spike:** 38
**Goal:** Can a hook (especially a repo-level settings.json hook) force an agent to execute an MCP tool defined in the SDK?

## Executive Summary

**VERDICT: Not natively possible for repo-level hooks. Proven for SDK Python hooks.**

The hook output schema has no `executeTool`, `replaceTool`, or `forceToolCall` mechanism. However, SDK Python hooks can call MCP tool handler functions directly and inject results back to the agent. Settings.json (file-based) hooks don't fire at all when running via the Agent SDK.

## Three-Tier Findings

| Approach | Works? | Mechanism | Limitation |
|----------|--------|-----------|------------|
| **SDK Python hook** | **YES** | Call `tool.handler(args)` directly in hook callback, inject result via `additionalContext`/`reason` | SDK-level only, not repo-level config |
| **Settings.json hook (advisory)** | **NO** | Deny tool + `additionalContext` asking agent to call MCP tool | Hooks don't fire via SDK; even if they did, agent compliance isn't guaranteed |
| **Settings.json hook (HTTP bridge)** | **NO** | Hook curls HTTP endpoint that wraps MCP logic | Same — hooks don't fire via SDK |

## What Was Tested

### Test 3: Direct Python Hook (PASS)

SDK Python `PreToolUse` hook intercepts Bash, calls MCP tool handler directly, blocks Bash, returns result:

```python
# SdkMcpTool has a .handler field — the async callable
result = await spike_echo_fn.handler({"message": "direct_python_hook_call"})
result_text = result["content"][0]["text"]

return {
    "continue_": False,
    "decision": "block",
    "reason": f"Bash blocked. MCP tool result: {result_text}",
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "additionalContext": f"MCP tool result: {result_text}",
    },
}
```

Agent output confirmed: *"The tool call was BLOCKED by a PreToolUse hook"* and *"The MCP tool was executed on my behalf"* with the marker `SPIKE38_MCP_CALLED` present in the response.

**Key detail:** The hook calls the Python function, not the MCP protocol. The agent sees the result as context text, NOT as a tool_use/tool_result pair in the conversation. The agent knows Bash was denied and sees the injected result.

### Tests 1, 2, 4: Settings.json Hooks (ALL FAIL)

**Root cause: settings.json hooks don't fire when running via the Claude Agent SDK.**

Tested across:
- `bypassPermissions` mode (Tests 1, 2)
- `acceptEdits` mode (Test 4)
- Minimal configuration with no SDK hooks (isolated test)
- With and without git init in the project directory

Debug logging in the hook scripts confirmed: **the scripts were never executed.** The Claude Code CLI subprocess, when launched by the SDK, does not load or execute hooks defined in `.claude/settings.json` at the working directory.

This is likely by design — the SDK controls the hook pipeline via its own JSONRPC `initialize` message. File-based hooks are a CLI feature for interactive/terminal use, not SDK use.

## Hook Output Schema (Definitive)

`PreToolUseHookSpecificOutput` has exactly 4 fields:
- `permissionDecision`: `"allow"` / `"deny"` / `"ask"` — whether tool executes
- `permissionDecisionReason`: string — feedback on deny, shown to agent
- `updatedInput`: dict — modifies the SAME tool's input (cannot change which tool is called)
- `additionalContext`: string — text injected into conversation

Top-level `SyncHookJSONOutput` adds:
- `continue_`: bool — whether agent continues
- `decision`: `"block"` — blocking indicator
- `reason`: string — feedback for Claude
- `systemMessage`: string — warning for user
- `stopReason`: string — shown when `continue_` is false

**There is no mechanism to:**
- Inject a new tool call into the agent's execution
- Change which tool is called (only modify the current tool's input)
- Force the agent to call a specific tool deterministically

## What This Means for the OBS Platform

### What Already Works (SDK hooks)

The platform's `hooks.py` already uses the pattern that works:
- `_BLOCKED_NATIVE_TASK_TOOLS` blocks native Task/SendMessage → tells agent to use AgentTask/SendInboxMessage
- Queue drain hooks inject messages via `additionalContext`
- Immutable guard blocks writes to Sources/

These are SDK Python hooks, so they fire reliably. The advisory pattern (deny + tell agent what to do) works because the agent cooperates — but it's not forced execution.

### The Gap: Repo-Level Config → MCP Tool Execution

The user wants: a hook defined in repo-level config that forces MCP tool execution. This requires bridging two disconnected systems:
1. **Repo-level config** (`.claude/settings.json` or similar) — defines WHAT should happen
2. **SDK Python hooks** — have the power to EXECUTE MCP tools

## Proposed SDK Modifications

### Option A: Config-Driven SDK Hooks (Recommended)

Add a hook config file (e.g., `.claude/hook-rules.json`) that SDK Python hooks read at runtime:

```json
{
  "rules": [
    {
      "event": "PreToolUse",
      "match": {"tool_name": "Bash"},
      "action": "deny_and_execute",
      "execute": {
        "tool": "mcp__obs-agent__session_lineage",
        "input": {"include_xml": "false"}
      },
      "inject_result_as": "additionalContext"
    }
  ]
}
```

The SDK's PreToolUse hook pipeline reads this file and:
1. Matches the rule
2. Calls the MCP tool's `.handler` directly
3. Injects the result via `additionalContext`

**Pros:** Repo-level config, deterministic execution, no CLI changes needed.
**Cons:** Custom format, need to build rule engine.

### Option B: HTTP Bridge for MCP Tools

Expose in-process MCP tools via a local HTTP endpoint:

```python
# In session.py startup
from aiohttp import web

async def handle_tool_call(request):
    data = await request.json()
    tool_name = data["tool"]
    tool_input = data.get("input", {})
    handler = mcp_tool_registry[tool_name].handler
    result = await handler(tool_input)
    return web.json_response(result)

app = web.Application()
app.router.add_post("/mcp/call", handle_tool_call)
# Start on ephemeral port, write port to /tmp/obs-mcp-bridge.port
```

Then settings.json hooks (if they ever work via SDK) or any external process could curl it:
```bash
curl -s http://localhost:PORT/mcp/call -d '{"tool": "session_lineage", "input": {}}'
```

**Pros:** Standard HTTP interface, any process can call MCP tools.
**Cons:** Security (localhost exposure), needs port management, only works if settings.json hooks fire.

### Option C: New Hook Output Field (Requires CLI Changes)

Add `executeTools` to `PreToolUseHookSpecificOutput`:

```python
class PreToolUseHookSpecificOutput(TypedDict):
    # ... existing fields ...
    executeTools: NotRequired[list[dict[str, Any]]]  # NEW
    # Each entry: {"tool_name": "...", "tool_input": {...}}
```

The CLI processes this by injecting tool_use/tool_result message pairs into the conversation, as if the agent called the tool.

**Pros:** True forced execution, clean API.
**Cons:** Requires Anthropic CLI changes (not our code), complex to implement correctly (needs tool_use_id generation, conversation state management).

### Recommendation

**Option A** is the pragmatic choice. It:
- Works with what we control (SDK Python hooks)
- Provides repo-level configurability
- Doesn't require CLI or external changes
- Can be implemented incrementally

The existing `HookPipeline` in `hooks.py` is already structured to support this — just add a rule-file reader as another pipeline stage.

## Prior Art

| Spike | Finding | Status |
|-------|---------|--------|
| 31 | PreToolUse/PostToolUse fire for Task tool | Confirmed (SDK hooks only) |
| 32 | Block + Replace with SDK agent in hook | Confirmed |
| 36 | Settings.json hooks fire alongside SDK hooks | **NOT confirmed for SDK usage** — may only apply to interactive CLI |
| 37 | Hook scripts receive stdin JSON | Not reachable (hooks don't fire) |
| **38** | SDK hook calls MCP tool handler directly | **NEW — confirmed** |
| **38** | Settings.json hooks don't fire via SDK | **NEW — confirmed** |

## Spike 39: Config-Driven Hook Engine (PROVEN)

**The missing piece was `setting_sources=["project"]`** — without it, the SDK passes `--setting-sources ""` to the CLI, which disables all file-based settings. With it, settings.json hooks fire correctly even in `bypassPermissions` mode.

### Final Config Format

```json
{
  "permissions": {...},
  "hooks": {...},
  "obs": {
    "tool_intercepts": [
      {
        "on": "PreToolUse",
        "match": "Bash",
        "mode": "advisory",
        "tool": "mcp__obs-agent__AgentTask",
        "input": {"prompt": "...", "display_name": "..."},
        "context": "SYSTEM: {tool} must be called with input {input}. Do it now."
      },
      {
        "on": "PreToolUse",
        "match": "Grep",
        "mode": "direct",
        "tool": "mcp__obs-agent__session_lineage",
        "input": {"include_xml": "false"},
        "context": "Lineage checked automatically: {result}"
      }
    ]
  }
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `on` | Yes | Hook event: `"PreToolUse"` |
| `match` | Yes | Tool name to intercept, or `"*"` for all |
| `mode` | Yes | `"advisory"` (agent calls tool, proper JSONL) or `"direct"` (SDK calls handler, invisible) |
| `tool` | Yes | Full MCP tool name to execute |
| `input` | Yes | Tool input matching the MCP schema |
| `context` | No | Template for `additionalContext`. Placeholders: `{tool}`, `{input}`, `{result}` (direct only) |

### How It Works

The `ConfigDrivenHookEngine` class:
1. Reads `obs.tool_intercepts` from `settings.json` at init
2. Registers as an SDK `PreToolUse` hook
3. On each tool call, checks for matching intercept rule
4. **Advisory mode**: Denies tool via `permissionDecision: "deny"`, sets `permissionDecisionReason` + `additionalContext` instructing agent to call target tool. Agent makes the call → proper tool_use/tool_result in JSONL.
5. **Direct mode**: Denies tool, calls target tool's `.handler()` directly, injects result via `additionalContext`. Agent sees result as text, not as a tool call.

### Test Results (Spike 39)

| Test | Config | Result |
|------|--------|--------|
| Advisory (Bash→spike_echo) | Single rule, advisory mode | PASS — agent called spike_echo, proper JSONL |
| Direct (Bash→spike_action) | Single rule, direct mode | PASS — handler called, result injected |
| Mixed (Bash→advisory, Grep→direct) | Two rules, both modes | PASS — both fired in same turn |

### Implementation Path

1. Add `ConfigDrivenHookEngine` to `src/obs_agent/hooks.py`
2. Register it in the existing `HookPipeline`
3. Build tool registry from MCP tool server at init
4. Settings.json `obs.tool_intercepts` becomes the config surface
5. Agent repos define their own intercept rules

## Open Questions

1. **Dynamic input**: Should `input` support templates like `{"prompt": "$tool_input.command"}`? Would allow forwarding the original tool's input to the replacement tool.
2. **Thread safety of `.handler` calls from hooks.** If multiple hooks fire concurrently and all call `.handler`, is the MCP tool implementation thread-safe?
3. **Retry on advisory failure**: If the agent doesn't comply with advisory mode (non-deterministic), should the engine retry or fall back to direct?
4. **Match patterns**: Should `match` support glob/regex for tool families (e.g., `mcp__obs-agent__*`)?
