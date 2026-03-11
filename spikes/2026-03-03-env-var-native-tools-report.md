# Native Team Tools via Env Vars: Spike Report

**Date**: 2026-03-03
**Builds on**: `2026-03-03-recursive-subagent-feasibility-report.md`

## Executive Summary

**Native task tools in SDK-spawned agents: PROVEN WORKING.** Two env vars unlock
TaskCreate/TaskList/TaskGet/TaskUpdate in any `ClaudeSDKClient` process, with a
third env var (`CLAUDE_CODE_TASK_LIST_ID`) pointing them at the correct task directory.
Workers can see each other's task updates in real time via shared files.

**SendMessage partially works.** The tool appears and executes, but delivers messages
to the "default" team inbox because the CLI's team context resolver (`J5()`) doesn't
read env vars — it checks AsyncLocalStorage and a process-global variable that only
native team spawning sets. A trivial MCP inbox tool can bridge this gap.

---

## 1. The Four Magic Env Vars

| Env Var | What It Unlocks | Found By |
|---------|----------------|----------|
| `CLAUDE_CODE_ENABLE_TASKS=1` | TaskCreate, TaskList, TaskGet, TaskUpdate | Reverse-engineering `h$()` gate in cli.js |
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` | TeamCreate, TeamDelete, SendMessage | Reverse-engineering `X7()` gate in cli.js |
| `CLAUDE_CODE_TASK_LIST_ID=<team-name>` | Points task tools at correct directory | Reverse-engineering `U0()` in cli.js |
| `CLAUDE_CODE_TEAM_NAME=<team-name>` | Referenced in error messages but NOT read by `J5()` | Partial — doesn't actually work for SendMessage |

### What Spike 17 Got Wrong

Spike 17 tested `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (teams gate) but **never tested
`CLAUDE_CODE_ENABLE_TASKS=1`** (task gate). These are different env vars gating different
tool sets via different `isEnabled()` functions.

---

## 2. Gating Functions (Decompiled from cli.js)

### `h$()` — Task Tools Gate

```javascript
function h$() {
  if (isFalsy(process.env.CLAUDE_CODE_ENABLE_TASKS))  return false;
  if (isTruthy(process.env.CLAUDE_CODE_ENABLE_TASKS)) return true;  // ← THIS!
  if (!isInteractive) return false;  // SDK mode = non-interactive
  return true;
}
```

Key: `CLAUDE_CODE_ENABLE_TASKS=1` **overrides** the non-interactive check. Without it,
SDK-spawned agents (non-interactive) get task tools disabled by default.

### `X7()` — Team Tools Gate

```javascript
function X7() {
  if (!isTruthy(process.env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS)
      && !process.argv.includes('--agent-teams'))
    return false;
  if (!featureFlag("tengu_amber_flint", true))  // defaults true
    return false;
  return true;
}
```

### `U0()` — Task Directory Resolver

```javascript
function U0() {
  if (process.env.CLAUDE_CODE_TASK_LIST_ID)
    return process.env.CLAUDE_CODE_TASK_LIST_ID;  // ← env var wins!
  let A = getAsyncLocalStorage();
  if (A) return A.teamName;
  return getTeamName() || globalDefault || sessionId();
}
```

### `J5()` — Team Name Resolver (SendMessage uses this)

```javascript
function J5(passedContext) {
  let store = getAsyncLocalStorage();
  if (store) return store.teamName;       // NOT set for SDK processes
  if (global?.teamName) return global.teamName;  // NOT set for SDK processes
  return passedContext?.teamName;          // NOT set — appState.teamContext empty
}
```

**This is why SendMessage delivers to "default"** — none of J5's lookup paths succeed
for SDK-spawned workers, and the inbox path builder falls back to `"default"`.

---

## 3. Spike Results

### Spike 19b: Env Var Tool Injection (CONFIRMED)

Both env vars set → **25 tools** (up from 19 baseline for subagents):

```
INIT tools (25): ['AskUserQuestion', 'Bash', 'Edit', 'EnterPlanMode', 'EnterWorktree',
  'ExitPlanMode', 'Glob', 'Grep', 'NotebookEdit', 'Read', 'SendMessage', 'Skill',
  'Task', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskOutput', 'TaskStop', 'TaskUpdate',
  'TeamCreate', 'TeamDelete', 'ToolSearch', 'WebFetch', 'WebSearch', 'Write']

  Task tools found: {'TaskList', 'TaskCreate', 'TaskGet', 'TaskUpdate'}
  Team tools found: {'SendMessage', 'TeamCreate', 'TeamDelete'}
```

### Spike 20b: Full Team Assembly (CONFIRMED)

1. Main agent: TeamCreate + 2x TaskCreate
2. calc-worker (SDK-spawned via MCP): TaskList → found tasks → TaskUpdate(in_progress) → TaskUpdate(completed)
3. str-worker (SDK-spawned via MCP): TaskList → saw task #1 done by calc-worker → claimed task #2 → completed
4. **Ground truth verification**: Task files on disk confirmed workers wrote directly

```
=== Worker Spawn Results ===
  calc-worker: 25 tools, TaskList=true, TaskUpdate=true, SendMessage=true
  str-worker:  25 tools, TaskList=true, TaskUpdate=true, SendMessage=true

=== Task Files (ground truth) ===
  1.json: status=completed, owner=calc-worker, subject=Add numbers
  2.json: status=completed, owner=str-worker, subject=Reverse string
```

### Spike 21: Native SendMessage (PARTIAL)

SendMessage executes successfully but writes to wrong directory:
- Worker called `SendMessage(type="message", recipient="team-lead", content="ALPHA-7")`
- Message delivered to `~/.claude/teams/default/inboxes/team-lead.json` instead of
  `~/.claude/teams/spike-21-msg/inboxes/team-lead.json`
- Root cause: `J5()` returns undefined for SDK processes → inbox path falls back to "default"

---

## 4. What We Need to Build (Revised)

### Now: Almost Nothing for Tasks

The native task tools handle everything: file creation, IDs, locking, dependencies,
status tracking. We just need to set 3 env vars.

### Trivial MCP Tool: Inbox Read/Write

For messaging, a ~20-line MCP tool that:
1. Reads/writes `~/.claude/teams/<team>/inboxes/<agent>.json` directly
2. Follows the same JSON format as native inboxes
3. Interoperable with native SendMessage (same file format)

```python
@tool("send_to_inbox", "Send a message to a teammate's inbox", {...})
async def send_to_inbox(args):
    team = args["team_name"]
    recipient = args["recipient"]
    message = args["message"]
    inbox_path = Path.home() / ".claude" / "teams" / team / "inboxes" / f"{recipient}.json"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(inbox_path.read_text()) if inbox_path.exists() else []
    data.append({"from": args.get("sender", "worker"), "text": message, ...})
    inbox_path.write_text(json.dumps(data))
    return {"content": [{"type": "text", "text": f"Message sent to {recipient}"}]}
```

### What We NO LONGER Need to Build

| Component | Before | Now |
|-----------|--------|-----|
| TaskCreate MCP tool | Custom implementation | **Native** (env var) |
| TaskList MCP tool | Custom implementation | **Native** (env var) |
| TaskUpdate MCP tool | Custom implementation | **Native** (env var) |
| TaskGet MCP tool | Custom implementation | **Native** (env var) |
| File locking for tasks | Custom implementation | **Native** (handled by CLI) |
| Task ID generation | Custom implementation | **Native** (handled by CLI) |
| Task dependency tracking | Custom implementation | **Native** (handled by CLI) |
| TeamCreate MCP tool | Custom implementation | **Native** (env var) |
| SendMessage MCP tool | Custom implementation | **Need ~20-line MCP tool** |
| Inbox read MCP tool | Custom implementation | **Need ~15-line MCP tool** |

**Reduction: ~90% of the originally planned MCP tools are now unnecessary.**

---

## 5. Recommended Architecture

```
Main Agent (ClaudeSDKClient)
├── Env: ENABLE_TASKS=1, AGENT_TEAMS=1, TASK_LIST_ID=<team>
├── Has: TeamCreate, TaskCreate, TaskList, TaskUpdate, TaskGet (native)
├── Has: spawn_worker MCP tool
├── Has: send_to_inbox / read_inbox MCP tools (trivial)
│
├── spawn_worker("calc-worker", prompt="...")
│   └── Worker (ClaudeSDKClient)
│       ├── Env: same + AGENT_NAME=calc-worker
│       ├── Has: TaskList, TaskUpdate (native!) ← reads same files
│       └── Has: send_to_inbox MCP tool
│
└── spawn_worker("research-worker", prompt="...")
    └── Worker (ClaudeSDKClient)
        ├── Env: same + AGENT_NAME=research-worker
        ├── Has: TaskList, TaskUpdate (native!) ← reads same files
        └── Has: send_to_inbox MCP tool
```

### Env Var Recipe (set once, inherited by all spawned processes)

```python
os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = team_name
os.environ["CLAUDE_CODE_TEAM_NAME"] = team_name
```

---

## 6. Cross-SDK Support: Proxy vs Direct File I/O

For non-Claude SDKs (Codex, Gemini), the env var hack won't work — those CLIs don't
read `CLAUDE_CODE_ENABLE_TASKS`. Two approaches considered:

### Option A: Fake API Proxy

Run a local HTTP server mimicking the Anthropic messages API. Set
`ANTHROPIC_BASE_URL=http://localhost:9999`. The Claude CLI connects, gets all 25
native tools, and our proxy returns deterministic `tool_use` responses — using the
CLI as a headless tool execution engine with zero model cost.

**Pros**: New features come free (CLI handles them natively). Concurrency, locking,
dependency resolution all handled by Anthropic's tested code. Narrow maintenance
surface (just the API response format).

**Cons**: Fragile at the API protocol level. Must match exact streaming response
format. If Anthropic changes the API contract, the proxy breaks.

### Option B: Direct File I/O (Python TaskManager)

Write a ~50-line Python class that reads/writes the same JSON files the CLI uses.
We know the exact format from decompiling the CLI source.

**Pros**: No dependency on CLI binary. Simple, fast, no network layer.

**Cons**: Must reimplement and maintain all task logic. New features (priorities,
subtasks, etc.) require reverse-engineering and adding. File locking edge cases
in high-concurrency scenarios.

### Verdict (Deferred)

For Claude workers: env vars (free, maintained by Anthropic).
For non-Claude workers: TBD. The proxy approach is preferred because it delegates
complexity to the CLI's battle-tested implementation, but both are fragile in
different ways. Decision deferred until Codex/Gemini integration is prioritized.

---

## 7. Risks and Caveats

1. **Undocumented env vars**: These are internal to the CLI and could change between
   versions. Pin the CLI version and test after updates.

2. **TASK_LIST_ID must match**: All workers must have the same `CLAUDE_CODE_TASK_LIST_ID`
   or they'll read/write to different task directories.

3. **SendMessage team routing is broken for SDK processes**: Until Anthropic adds env
   var support to `J5()`, we need our own inbox MCP tool. This is low-risk — the inbox
   format is a simple JSON array.

4. **Feature flags**: `X7()` checks `r8("tengu_amber_flint", true)` — a growth book
   feature flag that defaults to `true`. If Anthropic disables this flag, team tools
   disappear. Low risk for now.

5. **isInteractive bypass**: `CLAUDE_CODE_ENABLE_TASKS=1` explicitly overrides the
   non-interactive check. This is a designed escape hatch, not a hack.

---

## 8. All Spike Files

```
spikes/
├── spike_19_env_task_tools.py        # Full env var test (3 configs)
├── spike_19b_quick_env_test.py       # Quick both-vars test → 25 tools ✓
├── spike_20_native_team_assembly.py  # First attempt (workers couldn't find tasks)
├── spike_20b_fixed_team.py           # Fixed with TASK_LIST_ID → FULL SUCCESS ✓
├── spike_21_native_messaging.py      # SendMessage test (delivers to wrong team)
└── 2026-03-03-env-var-native-tools-report.md  # This report
```
