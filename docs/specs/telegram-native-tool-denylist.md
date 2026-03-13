# Telegram Native Tool Denylist

**Status**: Implemented  
**Date**: 2026-03-12  
**Owner**: `src/obs_agent/hooks.py`

## Context

Telegram orchestration uses OBS-owned tools for delegation and inbox messaging:

- `AgentTask` / `AgentTaskOutput` / `AgentTaskStop`
- `SendInboxMessage` / `ReadInbox`

Native tools in the same runtime can overlap and produce split behavior. This
spec fixes ownership boundaries for Telegram.

## Current Tool Surface (2026-03-12)

Important distinction: these are mostly **native Claude tools**, not OBS MCP
tools.

Live SDK probe on 2026-03-12:

- baseline session: 22 tools
- with `CLAUDE_CODE_ENABLE_TASKS=1`: 25 tools
- added by `ENABLE_TASKS=1`: `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`
- baseline already includes: `Task`, `TaskOutput`, `TaskStop`, `TeamCreate`,
  `TeamDelete`, `SendMessage`

The old "20+ team MCP tools" phrasing is inaccurate. The observed "20+" count
is total native tool count in session init; only a subset is team/task-related.

## Env-Var Hack Usage in Telegram

Telegram still applies env overrides for team workers:

- `CLAUDE_CODE_ENABLE_TASKS=1`
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
- `CLAUDE_CODE_TASK_LIST_ID=<team_name>`
- `CLAUDE_CODE_TEAM_NAME=<team_name>`
- `CLAUDE_CODE_AGENT_NAME=<agent_name>` when present

This is applied when creating/resuming child worker sessions for AgentTask/ForkTask.

## Decision

Keep hard deny in `PreToolUse` for native tools superseded by OBS behavior.

Blocked native task tools:

- `Task`
- `TaskOutput`
- `TaskStop`

Blocked native mode-control tools:

- `EnterPlanMode`

Blocked native inbox/messaging tools:

- `SendMessage`
- `ReadMessage`
- `ReadMessages`
- `ListMessages`
- `GetMessages`
- `ReceiveMessages`

Allowed native team/task tools (not currently blocked):

- `TaskCreate`
- `TaskGet`
- `TaskList`
- `TaskUpdate`
- `TeamCreate`
- `TeamDelete`

The deny guard normalizes MCP-style names (for example `mcp__native__Task`)
before checking the denylist.

## Behavior

When a blocked tool is requested, `PreToolUse` returns a deterministic hard deny:

- task tools: "use `AgentTask*`"
- inbox tools: "use `SendInboxMessage` / `ReadInbox`"
- mode tool: "plan mode control is not available in Telegram runtime"

Hook response semantics use both:

- `hookSpecificOutput.permissionDecision = "deny"` (tool-level deny)
- top-level decision/system fields (`decision: "block"`, `reason`, `systemMessage`)

This gives stronger, explicit denial feedback to the model instead of a soft
"wrong API shape" feel.

This is hard-coded at platform level for Telegram so agents cannot bypass OBS
orchestration semantics.

Important: denylist policy blocks execution, but does not guarantee the native
tool is hidden from tool discovery/listing. In live runs, agents may still
report that `Task` is "available" while launch is denied by policy.

## Verification

Unit tests in `tests/test_hooks.py` cover:

- direct native `Task*` denial
- MCP-prefixed `Task*` denial
- native inbox denial (`SendMessage`, `ReadMessages`)
- native mode-control denial (`EnterPlanMode`)
- explicit allow for OBS inbox tools (`SendInboxMessage`, `ReadInbox`)

Telegram tests verify team worker env propagation in child sessions:

- `tests/test_telegram.py` (`CLAUDE_CODE_*` assertions for launched/resumed workers)
