# Claude Agent SDK for Python - Research

## Source
- Package: `claude-agent-sdk` v0.1.33
- Installed at: `/Users/breedoon/Documents/JetBrainsProjects/PyCharm/P/orca/.venv/lib/python3.12/site-packages/claude_agent_sdk/`
- Sub-agent session: a08e615 (from [[Agent/system/sessions/2026-02-11-initial-design|initial design session]])
- Docs: https://platform.claude.com/docs/en/agent-sdk/overview

## Two Entry Points

### query() - Simple, Unidirectional
```python
async for message in query(prompt="...", options=ClaudeAgentOptions(...)):
    print(message)
```

### ClaudeSDKClient - Interactive, Bidirectional
```python
async with ClaudeSDKClient(options) as client:
    await client.query("message")
    async for msg in client.receive_response():
        print(msg)
    # Can send more messages, interrupt, change settings mid-session
```

## ClaudeAgentOptions (Key Parameters)

| Parameter           | Type      | Purpose                                               |
| ------------------- | --------- | ----------------------------------------------------- |
| system_prompt       | str       | Custom instructions                                   |
| cwd                 | Path      | Working directory                                     |
| allowed_tools       | list[str] | Tools agent can use                                   |
| permission_mode     | str       | "default", "acceptEdits", "plan", "bypassPermissions" |
| max_turns           | int       | Safety limit                                          |
| resume              | str       | Session ID to resume                                  |
| fork_session        | bool      | Branch from existing session                          |
| hooks               | dict      | Lifecycle hook callbacks                              |
| mcp_servers         | dict      | External/in-process tools                             |
| agents              | dict      | Subagent definitions                                  |
| setting_sources     | list      | ["user", "project"] for skill loading                 |
| max_budget_usd      | float     | Cost limit                                            |
| max_thinking_tokens | int       | Extended thinking budget                              |

Full list: 30+ options available.

## Hooks

| Hook | When | Use Case |
|------|------|----------|
| PreToolUse | Before tool runs | Block/modify tool calls |
| PostToolUse | After tool runs | Log, audit, trigger side effects |
| UserPromptSubmit | User sends prompt | Inject context |
| Stop | Agent finishes | Cleanup, save state |
| SubagentStop | Subagent done | Track parallel tasks |
| PreCompact | Before compaction | Persist context |

Hook signature:
```python
async def hook(input_data: dict, tool_use_id: str | None, context: dict) -> dict
```

Return `{}` to allow, return `{"hookSpecificOutput": {"permissionDecision": "deny"}}` to block.

## Session Management

- **Resume**: `query(prompt, options=ClaudeAgentOptions(resume=session_id))`
- **Fork**: `ClaudeAgentOptions(resume=session_id, fork_session=True)` - creates new branch, preserves original
- **Prompt caching**: Transparent, handled by Claude Code CLI. ~1 hour window.
- **Cache tracking**: Not in SDK API. Must read JSONL transcript files for cache hit/miss counters.
- **Session persistence**: Stored by CLI, cleaned after 30 days by default.

## MCP Tools (In-Process)

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("tool_name", "description", {"param": type})
async def my_tool(args):
    return {"content": [{"type": "text", "text": "result"}]}

server = create_sdk_mcp_server(name="my-tools", version="1.0", tools=[my_tool])
```

No subprocess overhead. Direct access to application state.

## Subagents

```python
from claude_agent_sdk import AgentDefinition

agents = {
    "reviewer": AgentDefinition(
        description="Code reviewer",
        prompt="You are a senior code reviewer...",
        tools=["Read", "Glob", "Grep"],
        model="sonnet"
    )
}
```

**Critical limitation**: Subagents cannot spawn their own subagents.

## Message Types

| Type | Content |
|------|---------|
| SystemMessage | subtype="init" has session_id |
| AssistantMessage | TextBlock, ToolUseBlock, ThinkingBlock |
| UserMessage | User or tool result content |
| ResultMessage | total_cost_usd, duration_ms, usage stats |

## Skills (Filesystem-Based)

Located at `.claude/skills/SKILLNAME/SKILL.md`. Require `setting_sources=["user", "project"]` or they silently fail.

## Key Limitations

- Subagents cannot spawn subagents
- Skills are filesystem-only (no programmatic registration)
- No hook for "session about to expire" (need timer ourselves)
- No direct cache control or metrics from SDK
- Prompt caching is transparent (can only check via JSONL)
- ClaudeSDKClient can't cross async contexts (anyio limitation)
- No built-in testing utilities (MockTransport is only extension point)
