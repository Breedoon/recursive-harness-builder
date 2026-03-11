# Claude Agent SDK: Task, Agent Teams, and Subagent Architecture Report

**Date**: 2026-03-03
**SDK Version**: claude-agent-sdk 0.1.35
**CLI**: Native ARM64 binary at `/opt/homebrew/bin/claude`

## Executive Summary

Subagents are separate Claude Code CLI processes orchestrated via a JSON control protocol over stdio. **Subagents cannot spawn sub-subagents** — this is a hard architectural constraint enforced by stripping the `Task` tool from subagent contexts. Teams are file-based coordination over `~/.claude/teams/` inboxes. Recursive delegation is only possible through workarounds (see Section 7).

---

## 1. Control Protocol (Spike 13, 14)

The SDK communicates with the Claude CLI via a bidirectional JSON protocol over stdin/stdout:

### Message Flow: Agent Spawning
```
SDK                           CLI (claude binary)
 |                              |
 |--- control_request --------->|  {subtype: "initialize", agents: {...}}
 |<-- control_response ---------|  {commands, models, account, pid}
 |                              |
 |--- user -------------------->|  {message: "Use the mini agent..."}
 |<-- system (init) -----------|  {tools: [...22 tools], session_id, model}
 |<-- assistant (thinking) -----|  ThinkingBlock
 |<-- assistant (tool_use) -----|  {tool: "Task", input: {subagent_type, prompt}}
 |<-- system (task_started) ----|  {task_id, tool_use_id, task_type: "local_agent"}
 |<-- user ---------------------|  Subagent prompt with parent_tool_use_id
 |<-- rate_limit_event ---------|  {status, resetsAt, rateLimitType}
 |<-- user (tool_result) -------|  Subagent response + usage + agentId
 |<-- assistant (thinking) -----|  Main agent processes result
 |<-- assistant (text) ---------|  Final response
 |<-- result (success) ---------|  {cost, turns, modelUsage, session_id}
```

### Key Protocol Messages

| Message | Direction | Purpose |
|---------|-----------|---------|
| `control_request (initialize)` | SDK→CLI | Register hooks, agents, start session |
| `control_response` | CLI→SDK | Returns commands, models, account info |
| `user` | SDK→CLI | User message |
| `system (init)` | CLI→SDK | Session config: tools list, MCP servers, model |
| `system (task_started)` | CLI→SDK | Subagent spawned: task_id, tool_use_id |
| `assistant (tool_use)` | CLI→SDK | Task tool call with subagent_type, prompt |
| `user (tool_result)` | CLI→SDK | Subagent result with content, usage, agentId |
| `rate_limit_event` | CLI→SDK | Rate limit status |
| `result (success)` | CLI→SDK | Turn complete: cost, turns, modelUsage |

### Agent Registration at Initialize

Agents are sent as a dict in the `initialize` control request:
```json
{
  "type": "control_request",
  "request": {
    "subtype": "initialize",
    "agents": {
      "mini": {
        "description": "Answers in one word",
        "prompt": "Answer with exactly one word.",
        "model": "haiku"
      }
    }
  }
}
```

Agents are **immutable once registered** — you cannot add/remove agents mid-session.

---

## 2. Subagent Tool Access (Spike 9)

### Main Agent Tools (22 total)
```
Task, TaskOutput, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write,
NotebookEdit, WebFetch, TodoWrite, WebSearch, TaskStop, AskUserQuestion,
Skill, EnterPlanMode, EnterWorktree, TeamCreate, TeamDelete, SendMessage,
ToolSearch
```

### Subagent Tools (19 total — missing 3)
Missing vs main agent:
- **Task** — STRIPPED (prevents recursive spawning)
- **EnterPlanMode** — removed
- **ExitPlanMode** — removed

Subagents retain:
- **TeamCreate, TeamDelete** — can create teams (but useless without Task)
- **SendMessage** — can send messages to teammates
- **TodoWrite** — local task lists
- All file ops, web ops, Bash

### Tool Restriction via AgentDefinition.tools

The `tools` field in `AgentDefinition` is an **allowlist filter**, not a grant list. It can only **restrict** — you cannot grant tools the runtime doesn't provide (Spike 10: setting `tools=["Task", "Agent"]` still results in no Task tool at runtime).

---

## 3. SubagentStart/SubagentStop Hooks — DON'T FIRE (Spike 2, 11)

Despite being documented in the SDK types:
- `SubagentStartHookInput` (types.py:256-261)
- `SubagentStopHookInput` (types.py:229-236)

**These hooks never fired in any spike.** PreToolUse/PostToolUse hooks also don't fire for Task tool calls. The subagent lifecycle appears to be handled entirely within the CLI binary, not dispatched back to the SDK's hook callback system.

**Implication**: We cannot intercept or modify subagent spawning via SDK hooks. The only observation point is the `system (task_started)` message in the protocol stream.

---

## 4. Recursive Spawning — BLOCKED (Spikes 3, 7, 10)

### What Happens When a Subagent Tries to Delegate

**Spike 3** (coordinator → worker): The coordinator subagent, instructed to delegate, tried `Skill({"skill": "agent"})` — a workaround attempt since it doesn't have the Task tool. The result came from the coordinator itself answering.

**Spike 7** (Agent → Agent → Agent): The process **hung indefinitely** — no output at all. Had to be killed after 3+ minutes. This matches documented behavior of JS heap OOM crashes from recursive spawning attempts.

**Spike 10** (explicit Task tool in AgentDefinition.tools): The subagent still didn't have the Task tool at runtime. It answered directly instead of delegating.

### Enforcement Mechanism
The CLI strips the Task/Agent tool from the subagent's tool set. There is no SDK-level way to override this — it's enforced in the CLI binary, not in the Python SDK.

---

## 5. Teams (Spikes 5, 8)

### File System Structure
```
~/.claude/teams/{team-name}/
├── config.json          # Team definition, members, lead session
└── inboxes/
    └── {agent-name}.json  # Per-agent message inbox

~/.claude/tasks/{team-name}/
├── .lock                # File lock for concurrent access
├── .highwatermark       # Task ID counter
├── 1.json               # Task files (numeric IDs)
└── ...
```

### Team Config (from real data)
```json
{
  "name": "research-panel",
  "createdAt": 1771632820051,
  "leadAgentId": "team-lead@research-panel",
  "leadSessionId": "bdb3a036-...",
  "members": [
    {
      "agentId": "team-lead@research-panel",
      "name": "team-lead",
      "agentType": "team-lead",
      "model": "claude-opus-4-6",
      "joinedAt": 1771632820051,
      "tmuxPaneId": "",
      "cwd": "/path/to/project"
    },
    {
      "agentId": "worker@research-panel",
      "name": "worker",
      "agentType": "general-purpose",
      "prompt": "...",
      "color": "blue",
      "backendType": "in-process",
      "joinedAt": 1771632844667
    }
  ]
}
```

### Creating Teams from SDK (Spike 5)

**TeamCreate works** — creates config.json and task directory. Returns:
```json
{
  "team_name": "spike-test-team",
  "team_file_path": "~/.claude/teams/spike-test-team/config.json",
  "lead_agent_id": "team-lead@spike-test-team"
}
```

### Spawning Teammates (Spike 8)

Teammates are spawned via the Task tool with `team_name` and `name` parameters:
```python
Task({
    "subagent_type": "general-purpose",
    "team_name": "spike-team-08",
    "name": "worker",
    "prompt": "...",
})
```

The teammate becomes a **separate Claude Code process** (not nested in the parent's context). Communication is async via message inboxes.

### Task Files
```json
{
  "id": "1",
  "subject": "Explore project context",
  "description": "Check existing skills...",
  "activeForm": "Exploring project context",
  "status": "completed",
  "blocks": [],
  "blockedBy": []
}
```

### Inbox Messages
```json
{
  "from": "worker-name",
  "text": "Message content or JSON with type field",
  "timestamp": "2026-02-21T00:14:32.433Z",
  "read": true,
  "color": "blue"
}
```

### Key Team Behaviors

1. **Async communication**: Team lead doesn't block waiting for teammate responses
2. **File-based**: All state in JSON files — no WebSocket/IPC
3. **Polling**: Agents check inboxes periodically
4. **Teammate independence**: Each teammate is a full CLI process with its own context window
5. **TaskCreate/TaskList/TaskUpdate**: These are NOT standard tools — they're team-context tools injected when an agent is part of a team (not available to standalone SDK agents)

---

## 6. MCP Tools and Subagents (Spike 6)

MCP tools registered via `create_sdk_mcp_server` are **visible to subagents** — they appear as `mcp__server_name__tool_name` in the subagent's tool set. However:

- Tool invocation requires correct function signatures (the SDK passes kwargs dict)
- The `@tool()` with no-arg functions needs to accept `**kwargs` or the invocation fails
- Subagents can call MCP tools; the MCP request goes back through the control protocol to the SDK's in-process handler

---

## 7. Paths to Recursive Subagents

Given the constraints, here are potential approaches ranked by feasibility:

### A. Custom MCP Tool That Spawns SDK Clients (Most Promising)

**Idea**: Create an MCP tool called `spawn_subagent` that internally creates a new `ClaudeSDKClient` and runs a query.

```python
@tool("spawn_subagent", "Spawn a sub-subagent", {...})
async def spawn_subagent(prompt: str, model: str = "haiku"):
    # This runs in the SDK host process, not in the CLI
    client = ClaudeSDKClient(ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=5,
    ))
    async with client:
        await client.query(prompt)
        result = []
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result.append(block.text)
            elif isinstance(msg, ResultMessage):
                break
    return "\n".join(result)
```

**Why it works**: MCP tool callbacks run in the SDK host process, which can freely spawn new CLI subprocesses. The subagent doesn't need the Task tool — our MCP tool does the spawning.

**Concern**: Each sub-subagent is a new CLI process with full startup cost. No depth limit enforcement (could stack-overflow with many levels). Need careful timeout handling.

### B. Codex/Gemini CLI as MCP Tool

**Idea**: MCP tool that shells out to `codex` or `gemini` CLI:

```python
@tool("codex_query", "Ask Codex to do something", {...})
async def codex_query(prompt: str):
    proc = await asyncio.create_subprocess_exec(
        "codex", "--prompt", prompt, "--model", "o3-mini",
        stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode()
```

**Why it works**: MCP tools can run arbitrary processes. The other SDK doesn't need to integrate with Claude's control protocol.

### C. Pre-registered Agent Pool

**Idea**: Register many agents at `initialize` time. Each "level" of recursion is a pre-registered agent with a different name. The "recursive" agent's prompt tells it to use agent N+1.

**Limitation**: Must pre-define the recursion depth. Maximum practical depth ~5-10 agents.

### D. Team-Based Recursion

**Idea**: A subagent creates a team (it has TeamCreate) and then... can't spawn teammates (no Task tool). Dead end unless combined with approach A.

### E. External Process Coordinator

**Idea**: MCP tool that writes to a file/queue. External process reads queue, spawns SDK clients, writes results back. MCP tool polls for results.

**Why**: Decouples spawning from the CLI's tool restrictions. More complex but most flexible.

---

## 8. Protocol-Level Observations

### Subagent Result Format
The tool_result from a subagent includes:
```json
{
  "tool_use_result": {
    "status": "completed",
    "prompt": "original prompt",
    "agentId": "a38b43f4621e8e12e",
    "content": [{"type": "text", "text": "response"}],
    "totalDurationMs": 842,
    "totalTokens": 18905,
    "totalToolUseCount": 0,
    "usage": {/* detailed token breakdown */}
  }
}
```

### Rate Limiting
Every subagent turn triggers a `rate_limit_event` with:
- `rateLimitType`: "five_hour" (subscription-based)
- `overageStatus`: "rejected" / "accepted"
- `resetsAt`: Unix timestamp

### Model Usage Tracking
The `result` message includes `modelUsage` broken down by model:
```json
{
  "claude-haiku-4-5-20251001": {
    "inputTokens": 21,
    "outputTokens": 224,
    "cacheReadInputTokens": 58570,
    "cacheCreationInputTokens": 8784,
    "costUSD": 0.017978,
    "contextWindow": 200000,
    "maxOutputTokens": 32000
  }
}
```

---

## 9. Key Constraints Summary

| Constraint | Enforcement | Bypassable? |
|-----------|-------------|-------------|
| No recursive subagents | Task tool stripped from subagent context | Yes, via MCP tools (Approach A) |
| No nested teams | Teammates lack Task tool | Yes, via MCP tools |
| Agents immutable after init | Control protocol only accepts agents at initialize | No (must reconnect) |
| Team communication is async | File-based inboxes, polling | Inherent design |
| SubagentStart/Stop hooks don't fire | CLI handles internally | No SDK workaround found |
| PreToolUse/PostToolUse skip Task tool | CLI internal dispatch | No SDK workaround found |
| Subagent tool allowlist is restrict-only | CLI enforces available tools | No (can't grant missing tools) |
| Max ~7 concurrent subagents | Practical/resource limit | Unclear if enforced |

---

## 10. Spike Results Summary

| Spike | What | Result |
|-------|------|--------|
| 01 | Basic AgentDefinition | **WORKS** — agent called via Task tool, haiku model |
| 02 | SubagentStart/Stop hooks | **HOOKS DON'T FIRE** — 0 events captured |
| 03 | Coordinator → Worker delegation | **BLOCKED** — coordinator can't use Task tool, used Skill workaround |
| 04 | Tool restriction via tools=[] | **WORKS** — tools list restricts available tools |
| 05 | TeamCreate from SDK | **WORKS** — creates config.json and task dir |
| 06 | Subagent with MCP tools | **PARTIALLY WORKS** — tools visible but invocation signature issue |
| 07 | Agent → Agent → Agent | **HANGS** — process hangs indefinitely, killed after 3min |
| 08 | Full team lifecycle | **WORKS** — create team, spawn teammate, send message |
| 09 | Tool introspection | **GOLD** — main=22 tools, subagent=19 (missing Task, PlanMode) |
| 10 | Explicit Task in tools list | **STILL BLOCKED** — tools list can't grant Task |
| 11 | PreToolUse hook for Task | **HOOKS DON'T FIRE** — 0 events captured |
| 12 | Raw message stream | **WORKS** — SystemMessage types: init, task_started |
| 13-14 | Custom transport / protocol dump | **WORKS** — full bidirectional protocol captured |
