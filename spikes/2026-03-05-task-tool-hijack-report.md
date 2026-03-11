# Task Tool Session Hijack — Feasibility Report

**Date:** 2026-03-05
**Spikes:** 22b, 23b, 24b, 25, 26, 27, 28, 29, 30, 31, 32, 33
**Goal:** Gain SDK-level control over agents spawned by Claude Code's native Task (Agent) tool — stream outputs, fork sessions, enable recursive spawning.

## Executive Summary

**VERDICT: FULLY FEASIBLE.** Three complementary patterns were proven:

| Pattern | Control Level | Streaming | Context | Recursion | Complexity |
|---------|--------------|-----------|---------|-----------|------------|
| **Hook-Intercept-Fork** | Full — replace native Task | ✅ Real-time | ✅ Session fork | ✅ Via fork | Medium |
| **Hybrid Task Monitor** | Full — SDK workers | ✅ Async | ⚠️ Fresh sessions | ✅ TaskCreate | Low |
| **PostToolUse Observer** | Read-only — monitor | ❌ Post-hoc | N/A | N/A | Trivial |

**Recommended: Hook-Intercept-Fork** — intercept native Task tool calls via PreToolUse hooks, block them, run replacement agents as session forks under full SDK control.

---

## Key Discovery: "Task" IS the Agent Tool

SDK agents have a tool called **`Task`** (not "Agent"). This IS the native Agent tool:
- Available in all SDK agents by default (22 tools baseline)
- Spawns general-purpose, Explore, Plan, and custom agents
- Returns: agent ID, content blocks, duration, token counts
- **Spike 28** confirmed it works and successfully spawned a subagent

## Proven Techniques

### 1. PreToolUse Hook Intercepts Task Tool ✅ (Spike 31, 32)

**Prior belief (spike 11, 2026-03-03): hooks don't fire for Agent tool.**
**CORRECTED: PreToolUse and PostToolUse hooks FIRE for the Task tool.**

```python
async def intercept_task(hook_input, tool_use_id, context):
    if hook_input.get("tool_name") == "Task":
        # hook_input contains:
        # - session_id: parent session ID
        # - transcript_path: JSONL file path
        # - cwd: working directory
        # - tool_input: {prompt, model, subagent_type, description}
        # - tool_use_id: for correlation
        return {"continue_": False, "decision": "block", "reason": "..."}
    return {"continue_": True}
```

The hook receives the FULL task specification before execution. We can block it and run our own agent.

### 2. Block + Replace Works ✅ (Spike 32, Test 2)

Successfully blocked the native Task tool and ran a replacement SDK agent:

```
[BLOCK] Blocking Task tool!
[BLOCK] Input: {"prompt": "What is 7*8?", "model": "haiku", ...}
[BLOCK] Running our own agent: prompt='What is 7*8?', model=haiku
[BLOCK] Our agent result: 56
[BLOCK] Our agent SID: 57e41fd5-...
TEXT: The Task tool executed successfully. The result is 56.
```

The main agent received the replacement result via the hook's `reason` field and correctly reported "56".

### 3. Session Forking Works ✅ (Spike 32, Test 3)

```python
fork_client = ClaudeSDKClient(ClaudeAgentOptions(
    resume=parent_session_id,
    fork_session=True,
    model="haiku",
    cwd="/Users/breedoon/Documents/obs",
))
```

The forked agent retains parent context:
```
Fork: You said "Say 'hello world'" and I responded with "hello world".
Fork SID: d1189577-...  (different from parent)
```

### 4. Hybrid Task Runner ✅ (Spike 30)

Native task creation + SDK worker spawning works end-to-end:

1. Main agent creates tasks via `TaskCreate` (env vars: `CLAUDE_CODE_ENABLE_TASKS=1`)
2. Python monitors `~/.claude/tasks/{team}/` directory
3. Spawns `ClaudeSDKClient` workers for each pending task
4. Workers complete independently, update task files to `completed`

```
Main: Tasks created! (fibonacci + haiku)
[MONITOR] Found new task: 1 — Calculate fibonacci
[MONITOR] Found new task: 2 — Write a haiku
[WORKER 1] The 10th fibonacci number is 55 ✓
[WORKER 2] Debugging at dawn / Stack traces unwind below / Logic fails again
Final: 1=completed, 2=completed
```

### 5. Recursive Sub-Task Creation ✅ (Spike 33)

Workers with `CLAUDE_CODE_ENABLE_TASKS=1` can create sub-tasks:

```
Initial: 1 task → Worker creates 2 sub-tasks → Monitor spawns 2 sub-workers
Final: 3 tasks total, all completed
  1: [completed] Plan a dinner party (worker-1)
  2: [completed] Choose menu (worker-2)
  3: [completed] Make guest list (worker-3)
```

### 6. PostToolUse Captures Full Response ✅ (Spike 32, Test 1)

PostToolUse hook receives the complete subagent response:

```json
{
  "tool_response": {
    "status": "completed",
    "agentId": "a2648fb56851f0733",
    "content": [{"type": "text", "text": "SUBAGENT_RESPONSE_XYZ"}],
    "totalDurationMs": 570,
    "totalTokens": 19743,
    "usage": { ... full token breakdown ... }
  }
}
```

Note: `agentId` is returned but NOT the subagent's session ID.

### 7. Session JSONL Readable ✅ (Spike 29)

Session data at `~/.claude/projects/{path}/{sid}.jsonl` is readable and contains conversation content (code words verified present).

---

## Blocked / Not Working

### A. can_use_tool Callback ❌ (Spike 22b, 28)

The `can_use_tool` callback **NEVER fires** in any permission mode. This appears to be a CLI behavior — `bypassPermissions` skips permission checks entirely, and other modes may handle Task tool internally.

**Not needed:** hooks provide the same interception capability.

### B. Agent Tool via Env Vars ❌ (Spike 24b)

No env var enables the "Agent" tool name. But this is irrelevant because:
- SDK agents already have the **Task** tool (= Agent tool) by default
- No special env vars needed for basic Task tool access

### C. Session Resume Reliability ⚠️ (Spike 29 vs 32)

Session forking failed in spike 29 but worked in spike 32. The difference may be:
- Race condition with concurrent processes
- stderr callback presence
- Timing between session close and fork

**Mitigation:** Add retry logic and ensure clean session disconnect before forking.

---

## The Recommended Pattern: Hook-Intercept-Fork-Stream

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage,
)

class TaskInterceptor:
    """Intercepts Task tool calls, runs replacement agents under SDK control."""

    def __init__(self, stream_callback=None):
        self.stream_callback = stream_callback  # e.g., send to Telegram

    async def pre_tool_hook(self, hook_input, tool_use_id, context):
        if hook_input.get("tool_name") != "Task":
            return {"continue_": True}

        task_input = hook_input.get("tool_input", {})
        prompt = task_input.get("prompt", "")
        model = task_input.get("model", "haiku")
        main_sid = hook_input.get("session_id")

        # Fork from main session for full context
        fork_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=model,
            max_turns=15,
            resume=main_sid,
            fork_session=True,
            cwd=hook_input.get("cwd", "."),
        )

        result_text = ""
        fork_client = ClaudeSDKClient(fork_opts)

        async with fork_client:
            await fork_client.query(prompt)
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text
                            # Stream to Telegram/etc in real time
                            if self.stream_callback:
                                await self.stream_callback(block.text)

        # Block native Task and inject our result
        return {
            "continue_": False,
            "decision": "block",
            "reason": f"Result: {result_text}",
        }

# Usage:
interceptor = TaskInterceptor(stream_callback=send_to_telegram)

client = ClaudeSDKClient(ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    hooks={
        "PreToolUse": [
            HookMatcher(hooks=[interceptor.pre_tool_hook]),
        ],
    },
))
```

### What This Gives You

| Capability | Status |
|-----------|--------|
| Intercept Task tool before execution | ✅ |
| Read full task specification (prompt, model, type) | ✅ |
| Spawn replacement agent under SDK control | ✅ |
| Fork from main session (preserves context) | ✅ |
| Stream replacement agent output in real-time | ✅ |
| Return result to main agent | ✅ |
| Replacement agent can itself be intercepted (recursive) | ✅ |
| Custom model/tools for replacement agent | ✅ |
| Access to replacement agent's session ID | ✅ |

### Recursive Forking

Since the replacement agent is an SDK client, it can also have hooks:

```python
# Replacement agent with its own interceptor → enables recursive forking
fork_client = ClaudeSDKClient(ClaudeAgentOptions(
    resume=main_sid, fork_session=True,
    hooks={
        "PreToolUse": [HookMatcher(hooks=[interceptor.pre_tool_hook])],
    },
))
```

Each level of recursion creates a new fork from the parent session.

---

## Alternative Pattern: Hybrid Task Monitor

For simpler use cases where real-time streaming isn't critical:

```python
# 1. Set env vars for native task tools
os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = team_name

# 2. Main agent creates tasks via TaskCreate
#    (agent uses it naturally, no hooks needed)

# 3. Python monitors task directory
async def monitor_tasks():
    task_dir = Path.home() / ".claude" / "tasks" / team_name
    while True:
        for f in task_dir.glob("*.json"):
            data = json.loads(f.read_text())
            if data["status"] == "pending" and not data["owner"]:
                # Spawn SDK worker
                asyncio.create_task(run_worker(f.stem, data))
        await asyncio.sleep(1)

# 4. Workers can create sub-tasks (recursive)
#    env vars propagate to workers
```

---

## Environment Variables Reference

```bash
# Enable native task tools in SDK agents
CLAUDE_CODE_ENABLE_TASKS=1                    # TaskCreate/TaskList/TaskGet/TaskUpdate
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1        # TeamCreate/TeamDelete/SendMessage
CLAUDE_CODE_TASK_LIST_ID=<team-name>          # Points task tools to correct directory
CLAUDE_CODE_TEAM_NAME=<team-name>             # Team context for SendMessage
CLAUDE_CODE_AGENT_NAME=<worker-name>          # Worker identity
```

Tools available with these env vars: **25 total** (baseline 22 + TaskCreate, TaskList, TaskGet, TaskUpdate).

---

## Tool Counts by Context

| Context | Tools | Notable Inclusions | Notable Exclusions |
|---------|-------|-------------------|-------------------|
| SDK client (baseline) | 22 | Task, TaskOutput, TaskStop, TeamCreate, TeamDelete, SendMessage | TaskCreate/List/Get/Update |
| SDK client (+ env vars) | 25 | All of above + TaskCreate, TaskList, TaskGet, TaskUpdate | None significant |
| Native subagent | 19 | Most tools | Task, EnterPlanMode, ExitPlanMode |

---

## Spike Results Summary

| Spike | Test | Result |
|-------|------|--------|
| 22b | can_use_tool fires for Task? | ❌ Never fires |
| 24b | Agent tool via env vars? | ❌ No env var, but Task tool = Agent |
| 28 | Task tool works in SDK agents? | ✅ Spawns subagents successfully |
| 29 | Session fork with cwd? | ⚠️ Fork failed (race condition?) |
| 30 | Hybrid task monitor? | ✅ Full end-to-end success |
| 31 | Hooks fire for Task tool? | ✅ PreToolUse + PostToolUse both fire |
| 32.1 | PostToolUse full output? | ✅ Complete response + metrics |
| 32.2 | Block + Replace? | ✅ Replacement agent result injected |
| 32.3 | Session fork with stderr? | ✅ Fork works, has parent context |
| 33 | Recursive sub-task creation? | ✅ Workers create sub-tasks |

---

## CORRECTED Prior Findings

| Prior Spike | Claim | Correction |
|-------------|-------|------------|
| Spike 11 (2026-03-03) | "PreToolUse hook doesn't fire for Task" | **WRONG.** Spike 31 proves hooks fire for Task tool. Spike 11 may have tested `Agent` tool name, or used a different hook configuration. |
| Spike 9 (2026-03-03) | "Subagents only have 19 tools" | **Partially wrong for SDK context.** SDK clients (not subagents) have 22+ tools including Task. The 19-tool limitation only applies to agents spawned BY the native Task tool. |

---

## Open Questions / Future Work

1. **Session fork reliability** — Sometimes fails with exit code 1. Needs retry logic and investigation of race conditions.
2. **Large result delivery** — Does the hook `reason` field handle results >10KB? May need chunking or file-based delivery.
3. **Concurrent hook execution** — If main agent spawns multiple Tasks concurrently, do hooks execute concurrently? Need to verify thread safety of fork spawning.
4. **MCP server inheritance** — Do Task-spawned subagents inherit parent MCP servers? If yes, we could provide a control channel without hooks.
5. **SendMessage routing fix** — Spike 21 showed SendMessage routes to "default" team. Need MCP inbox tool or env var fix.
6. **Performance** — Each fork spawns a new claude subprocess (~67MB RSS). For many concurrent forks, memory may be a concern.

---

## Next Steps

1. **Build `TaskInterceptor` class** — Production-ready version of the hook-intercept-fork pattern
2. **Integrate with ConversationRunner** — Stream forked agent output through existing Telegram/CLI infrastructure
3. **Add retry logic for session forks** — Handle intermittent exit code 1 failures
4. **Test with real OBS agent** — Verify hooks work when agent has MCP servers and system prompts
5. **Benchmark concurrent forks** — Measure memory/latency with 3-5 simultaneous forks
