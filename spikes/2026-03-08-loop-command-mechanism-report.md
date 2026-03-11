# /loop Command & Cron Scheduling — Mechanism Report

**Date**: 2026-03-08
**Goal**: Understand how Claude Code's `/loop` command works, whether it's accessible from the Claude Agent SDK (Python), and how it could be integrated into a custom SDK-based agent.

## Executive Summary

**`/loop` is a bundled skill in Claude Code that provides syntactic sugar over three internal tools: CronCreate, CronDelete, CronList.** These tools implement session-scoped cron scheduling entirely within the Claude Code CLI harness. They are **NOT available via the Python Agent SDK** — not through `allowed_tools`, not through env vars, and not through any SDK API. The cron scheduler is a CLI-only feature gated by the `tengu_kairos_cron` feature flag and requires an interactive REPL idle loop to fire scheduled prompts.

To replicate `/loop` behavior in an SDK-based agent, you must build your own scheduler in Python that feeds prompts to the SDK client on a timer.

---

## 1. What /loop Does (User Perspective)

`/loop` is a bundled skill (added in Claude Code 2.1.71, released 2026-03-07) that schedules a prompt to run repeatedly on an interval.

### Usage

```
/loop 5m check if the deployment finished
/loop check the build                       # defaults to every 10 minutes
/loop check the build every 2 hours         # trailing interval
/loop 20m /review-pr 1234                   # loop another skill
```

### Interval Parsing

The skill applies three priority rules:

1. **Leading token**: If input begins with `5m`, `2h`, `30s`, etc., that becomes the interval
2. **Trailing "every" clause**: Recognizes `every 20m` at the end (but not contextual phrases like "check every PR")
3. **Default fallback**: 10-minute interval if no interval specified

Supported units: `s` (seconds, rounded up to nearest minute), `m` (minutes), `h` (hours), `d` (days).

### Interval-to-Cron Conversion

| Input | Cron Expression |
|-------|----------------|
| `Nm` (minutes <=59) | `*/N * * * *` |
| `Nh` (hours <=23) | `0 */N * * *` |
| `Nd` (days) | `0 0 */N * *` |

### Session-Scoped Behavior

- Tasks only fire while Claude Code is running **and idle** (between turns, not mid-response)
- Closing the terminal or exiting Claude Code cancels everything
- No persistence across restarts
- Recurring tasks auto-expire after **3 days**
- Maximum **50 scheduled tasks** per session
- No catch-up for missed fires — fires once when idle

---

## 2. Under the Hood: Three Internal Tools

`/loop` is a prompt-based skill that instructs Claude to call the underlying tools. The tools themselves are built into the Claude Code CLI:

| Tool | Purpose | Input |
|------|---------|-------|
| **CronCreate** | Schedule a new task | `cron` (5-field expression), `prompt` (text), `recurring` (bool, default true) |
| **CronList** | List all scheduled tasks | No input |
| **CronDelete** | Cancel a task by ID | `id` (8-character job ID) |

### CronCreate Details

- Accepts standard 5-field cron: `minute hour day-of-month month day-of-week`
- Supports: wildcards (`*`), single values, steps (`*/15`), ranges (`1-5`), comma-separated lists
- Does NOT support: `L`, `W`, `?`, name aliases (`MON`, `JAN`)
- Validates cron matches a future date within one year
- Enforces 50-job maximum per session
- Returns an 8-character job ID

### Scheduler Runtime (`createCronScheduler`)

- **Tick interval**: Checks every 1 second for due jobs
- **Idle requirement**: Only fires when REPL is not actively processing a turn
- **Enqueues at low priority**: Scheduled prompts enter the prompt queue, don't interrupt current work
- **Jitter**: Adds deterministic offset (up to 10% of period, capped at 15 minutes) derived from task ID to prevent thundering herd
- **One-shot jitter**: Tasks on `:00` or `:30` fire up to 90 seconds early
- **3-day auto-expiry**: Recurring tasks fire one final time, then self-delete

### Feature Gate

The cron tools are gated by the `tengu_kairos_cron` feature flag, polled every 5 minutes. This means availability depends on server-side feature rollout, not just CLI version.

### Disable Switch

Set `CLAUDE_CODE_DISABLE_CRON=1` to disable the scheduler entirely. The cron tools and `/loop` become unavailable.

---

## 3. Is /loop Accessible from the Python Agent SDK?

**NO.** Confirmed by two spike tests:

### Spike Result 1: Baseline SDK Agent Tools (22 tools)

```
Task, TaskOutput, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write,
NotebookEdit, WebFetch, WebSearch, Skill, AskUserQuestion, EnterPlanMode,
EnterWorktree, TeamCreate, TeamDelete, SendMessage
```

**CronCreate, CronDelete, CronList are absent.**

### Spike Result 2: Explicit `allowed_tools` Attempt

```python
ClaudeAgentOptions(
    allowed_tools=["CronCreate", "CronDelete", "CronList"],
    ...
)
```

Result: Same 20 tools listed. **Cron tools still absent.** The `allowed_tools` parameter cannot conjure tools that the CLI doesn't make available in SDK mode.

### Why They're Absent

1. **REPL-only feature**: The cron scheduler requires the interactive REPL idle loop to fire tasks. SDK mode (`--input-format stream-json --output-format stream-json`) doesn't run the REPL — it uses a streaming control protocol instead.

2. **Feature gate**: The `tengu_kairos_cron` flag controls tool registration. Even if the gate is open, the tools are only registered in the interactive REPL code path, not in the SDK subprocess code path.

3. **No SDK API**: The Python SDK has no `schedule()` method, no cron-related types in `types.py`, and no references to cron/loop/schedule anywhere in the SDK source code (`claude_agent_sdk/`).

4. **Not in JSONL format**: Cron tasks are stored in-memory (session-scoped arrays) or in `.claude/scheduled_tasks.json` for durable tasks. They are not part of the JSONL conversation transcript format.

---

## 4. Implementation Mechanism: CLI Harness, Not Conversation Format

The `/loop` and cron system is implemented entirely in the **CLI harness layer**:

| Component | Where | What |
|-----------|-------|------|
| `/loop` skill | Bundled skill in CLI binary | Prompt template that instructs Claude to call CronCreate |
| CronCreate/Delete/List | Internal tools registered in CLI | Tool implementations for job CRUD |
| `createCronScheduler` | CLI harness (Node.js) | 1-second tick loop checking for due jobs |
| Job storage | In-memory arrays | Session-scoped, cleared on exit |
| Durable storage | `.claude/scheduled_tasks.json` | For Desktop scheduled tasks (different feature) |
| Prompt injection | REPL idle handler | Enqueues scheduled prompts into the input queue |

**The conversation format (JSONL) does not contain cron-specific entries.** When a scheduled task fires, its prompt is simply injected into the conversation as a regular user message. There is no special JSONL entry type for cron/loop/schedule.

---

## 5. How to Replicate /loop in a Custom SDK Agent

Since the cron tools are unavailable in the SDK, you must build your own scheduler. Here are three approaches:

### Approach A: Python asyncio Scheduler (Recommended)

Build a scheduler that feeds prompts to a `ClaudeSDKClient` on a timer:

```python
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

@dataclass
class CronJob:
    id: str
    prompt: str
    interval_seconds: int
    next_fire: datetime
    recurring: bool = True
    expires_at: datetime | None = None

class AgentScheduler:
    """Session-scoped cron scheduler for SDK agents."""

    def __init__(self, client: ClaudeSDKClient):
        self.client = client
        self.jobs: dict[str, CronJob] = {}
        self._busy = False

    def add(self, job_id: str, prompt: str, interval_seconds: int,
            recurring: bool = True) -> CronJob:
        job = CronJob(
            id=job_id,
            prompt=prompt,
            interval_seconds=interval_seconds,
            next_fire=datetime.now() + timedelta(seconds=interval_seconds),
            recurring=recurring,
            expires_at=datetime.now() + timedelta(days=3) if recurring else None,
        )
        self.jobs[job_id] = job
        return job

    def remove(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    async def run(self):
        """Tick loop — check every second for due jobs."""
        while True:
            await asyncio.sleep(1)
            if self._busy:
                continue

            now = datetime.now()
            for job in list(self.jobs.values()):
                # Check expiry
                if job.expires_at and now >= job.expires_at:
                    self.jobs.pop(job.id, None)
                    continue

                if now >= job.next_fire:
                    self._busy = True
                    try:
                        await self.client.query(job.prompt)
                        async for msg in self.client.receive_response():
                            pass  # Process response
                    finally:
                        self._busy = False

                    if job.recurring:
                        job.next_fire = now + timedelta(
                            seconds=job.interval_seconds
                        )
                    else:
                        self.jobs.pop(job.id, None)
```

**Pros**: Full control, works with any SDK client, can stream responses to Telegram/CLI.
**Cons**: Must build cron expression parsing, no natural language interval support.

### Approach B: MCP Tool Providing Cron Tools

Expose CronCreate/CronDelete/CronList as MCP tools that the SDK agent can call:

```python
from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.tools import tool

scheduler = AgentScheduler(client)  # From Approach A

@tool("CronCreate", "Schedule a recurring prompt", {
    "type": "object",
    "properties": {
        "cron": {"type": "string", "description": "5-field cron expression"},
        "prompt": {"type": "string"},
        "recurring": {"type": "boolean", "default": True},
    },
    "required": ["cron", "prompt"],
})
async def cron_create(cron: str, prompt: str, recurring: bool = True):
    interval = parse_cron_to_seconds(cron)  # You'd implement this
    job = scheduler.add(uuid4().hex[:8], prompt, interval, recurring)
    return f"Scheduled job {job.id}: '{prompt}' every {interval}s"

server = create_sdk_mcp_server("cron", tools=[cron_create, ...])
```

**Pros**: Agent can schedule tasks via natural tool use; matches CLI behavior.
**Cons**: More complex; requires cron expression parser; agent must "know" about these tools.

### Approach C: Hook-Based Timer Injection

Use a PostToolUse or Stop hook to inject scheduled prompts as `additionalContext`:

```python
async def check_schedule(hook_input, tool_use_id, context):
    due_prompts = scheduler.get_due()
    if due_prompts:
        return {
            "continue_": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"SCHEDULED TASK DUE: {due_prompts[0].prompt}",
            },
        }
    return {"continue_": True}
```

**Pros**: Lightweight; prompts injected between turns like the real scheduler.
**Cons**: Only fires at hook boundaries; no guaranteed timing; can't inject if agent is idle (no hooks fire).

### Recommendation

**Approach A** for basic scheduling in a daemon/bot.
**Approach B** if you want the agent itself to decide when to schedule (closest to `/loop` behavior).
**Approach A + B combined** for the full experience: MCP tools let the agent create/delete jobs, Python scheduler handles the timer and prompt injection.

---

## 6. Key Differences: CLI /loop vs SDK Equivalent

| Aspect | CLI /loop | SDK Equivalent |
|--------|-----------|---------------|
| Tool availability | Built-in (CronCreate/Delete/List) | Must provide via MCP |
| Scheduler | Node.js 1-second tick loop in REPL | Python asyncio task |
| Prompt injection | Low-priority queue in REPL | `client.query()` call |
| Feature gate | `tengu_kairos_cron` server-side flag | None needed |
| Persistence | Session-scoped (in-memory) | Your choice |
| Idle detection | REPL knows when it's between turns | Must track `_busy` state |
| Jitter | Deterministic offset from task ID | Optional, your implementation |
| Auto-expiry | 3 days | Your implementation |
| Max tasks | 50 | Your implementation |

---

## 7. JSONL and Conversation Format

**No special JSONL entries for cron.** When a scheduled task fires:

1. The scheduler waits for the REPL to be idle
2. The prompt is injected as a regular user message into the conversation
3. The conversation proceeds normally — Claude sees it as a regular prompt
4. The JSONL transcript records it as a standard `UserMessage` / `AssistantMessage` exchange

There is no `CronEntry`, `ScheduledMessage`, or similar type in the JSONL format. The scheduling is entirely orthogonal to the conversation format.

---

## 8. Desktop Scheduled Tasks (Related but Different)

Claude Code Desktop has a separate **durable scheduling** system:

- Persists across restarts
- Stores tasks in `.claude/scheduled_tasks.json`
- Runs without an active terminal session
- Not session-scoped
- Available via Desktop UI, not via `/loop`

This is a separate feature from `/loop` and is also not accessible via the SDK.

---

## Files

- `/tmp/spike_cron_check.py` — Verified CronCreate absent from SDK baseline tools (19 tools listed)
- `/tmp/spike_cron_allowed.py` — Verified CronCreate absent even with explicit `allowed_tools` (20 tools listed)
