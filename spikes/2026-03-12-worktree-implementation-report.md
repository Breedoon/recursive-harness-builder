# Claude Code Worktree Implementation — Spike Report

**Date:** 2026-03-12
**SDK Version Analyzed:** `@anthropic-ai/claude-agent-sdk` v0.2.49 (bundled CLI v2.1.49)
**Methodology:** Reverse-engineering minified `cli.js` bundle, SDK type definitions (`sdk.d.ts`, `sdk-tools.d.ts`), online docs, changelogs, GitHub issues

---

## Executive Summary

Worktrees are a **Claude Code CLI feature**, not an Agent SDK feature. The SDK exposes worktree functionality through tools (EnterWorktree) and tool parameters (isolation: "worktree" on Task), but has **no programmatic API** for creating or managing worktrees. This means our obs platform cannot programmatically start sessions in worktrees via the SDK — worktrees are triggered either by users, by the agent itself calling a tool, or declaratively via agent frontmatter. However, there are useful patterns we could adopt for subagent isolation.

---

## 1. Feature Timeline

- **v2.1.49 (2026-02-19):** Initial release. `--worktree` CLI flag, `EnterWorktree` tool, `isolation: "worktree"` on Task tool, `WorktreeCreate`/`WorktreeRemove` hook events in settings.json
- **v2.1.50 (2026-02-20):** `isolation: worktree` in `.claude/agents/` frontmatter, `claude agents` command
- **v2.1.63:** Project configs & auto memory shared across worktrees of same repo
- **v2.1.69:** `worktree` field added to status line hook (name, path, branch, original dir)
- **v2.1.70:** Faster `--worktree` startup
- **v2.1.72:** `ExitWorktree` tool added. Bug fixes for resume cwd, background task notifications

**Note:** Our bundled SDK is v0.2.49 (matches CLI v2.1.49). It does NOT have `ExitWorktree` — that was added in v2.1.72.

---

## 2. Three Entry Points for Worktrees

### 2.1 CLI Flag: `--worktree [name]`

```bash
claude --worktree my-feature
claude -w  # auto-generated name
claude --worktree --tmux  # also creates tmux session
```

- **Scope:** Per-session. The entire session runs in the worktree.
- **What happens:** Creates `<repo>/.claude/worktrees/<name>/` with branch `worktree-<name>`. Copies `settings.local.json`, configures hooks path, symlinks directories from `worktree.symlinkDirectories` setting.
- **Cleanup on exit:** If no changes → auto-removed. If changes → user prompted to keep or remove.
- **SDK access:** None. The SDK's `Options` type has no `worktree` parameter. Cannot be triggered via `query()`.

### 2.2 Tool: `EnterWorktree` (agent-initiated, mid-session)

```typescript
// Tool input
interface EnterWorktreeInput {
  name?: string;  // optional, auto-generated if omitted
}

// Tool output
interface EnterWorktreeOutput {
  worktreePath: string;
  worktreeBranch: string;
  message: string;
}
```

- **Scope:** Per-session. Once entered, the session's cwd moves to the worktree.
- **Who triggers it:** The agent, in response to user saying "work in a worktree" or "start a worktree."
- **Guard rails:**
  - Must be in a git repo
  - Cannot already be in a worktree (throws: "Already in a worktree session")
  - If cwd is inside a worktree, it resolves to the main repo root first via `resolveMainRepoRoot()`
- **Implementation (from cli.js):** Calls `createWorktreeForSession()` → `git worktree add -b worktree-<name> .claude/worktrees/<name> HEAD` → copies local settings → symlinks configured dirs → sets global `yg` (worktree session state) → changes `process.cwd()`.
- **ExitWorktree (v2.1.72+):** Allows leaving a worktree mid-session. Not in our SDK version.
- **Limitation:** Cannot enter an *existing* worktree. Always creates new. [GitHub #31969](https://github.com/anthropics/claude-code/issues/31969)

### 2.3 Task Tool Parameter: `isolation: "worktree"` (subagent isolation)

```typescript
// Task tool input (partial)
{
  prompt: string;
  description: string;
  subagent_type: string;
  isolation?: "worktree";  // <-- this
  // ...
}
```

- **Scope:** Per-subagent. The spawned subagent runs in its own worktree.
- **Who triggers it:** The parent agent, when spawning a child via the Task tool.
- **What happens (from cli.js reverse-engineering):**
  1. `createAgentWorktree()` is called with a random name
  2. Creates worktree at `<repo>/.claude/worktrees/<name>/`
  3. The subagent runs with cwd = worktree path
  4. On completion: checks `hasWorktreeChanges()` (uncommitted files OR commits ahead of original HEAD)
  5. If no changes → `removeAgentWorktree()` (git worktree remove --force + branch delete)
  6. If changes → worktree preserved, `worktreePath` and `worktreeBranch` returned in tool result
- **Also available via agent frontmatter:** `.claude/agents/my-agent.md` with `isolation: worktree` in YAML frontmatter
- **Known bugs:**
  - Branch names hardcoded as `worktree-agent-{hash}`, not customizable [#27749](https://github.com/anthropics/claude-code/issues/27749)
  - Background team members with `isolation: "worktree"` may have cwd still set to main repo [#33045](https://github.com/anthropics/claude-code/issues/27749)

---

## 3. SDK Surface Area

### What the SDK exposes

| Surface | Available? | Details |
|---------|-----------|---------|
| `options.worktree` on `query()` | ❌ | Not in `Options` type |
| `AgentDefinition.isolation` | ❌ | Not in SDK's programmatic type (only in frontmatter) |
| `EnterWorktree` as built-in tool | ✅ | Listed in `ToolInputSchemas` union, typed I/O |
| `isolation` on Task tool input | ✅ | Agent can pass it when calling Task tool |
| `WorktreeCreate`/`WorktreeRemove` hooks | ⚠️ | In settings.json hooks only, NOT in SDK `HookEvent` type (SDK hooks: PreToolUse, PostToolUse, etc.) |
| `listSessions({ includeWorktrees })` | ✅ | Discovery of worktree sessions |
| `allowedTools`/`disallowedTools` for EnterWorktree | ✅ | Can control whether agent can create worktrees |
| `worktree.symlinkDirectories` setting | ✅ | Via settings.json, not SDK API |

### Key type definitions

```typescript
// sdk-tools.d.ts
interface EnterWorktreeInput {
  name?: string;
}

interface EnterWorktreeOutput {
  worktreePath: string;
  worktreeBranch: string;
  message: string;
}

// sdk.d.ts — AgentDefinition (SDK v0.2.49)
type AgentDefinition = {
  description: string;
  tools?: string[];
  disallowedTools?: string[];
  prompt: string;
  model?: 'sonnet' | 'opus' | 'haiku' | 'inherit';
  mcpServers?: AgentMcpServerSpec[];
  criticalSystemReminder_EXPERIMENTAL?: string;
  skills?: string[];
  // NOTE: no `isolation` field!
};
```

### What's NOT in the SDK

1. **No programmatic worktree creation.** Can't pass `worktree: true` to `query()`.
2. **No `isolation` on `AgentDefinition`.** The frontmatter-based `.claude/agents/` definitions support `isolation: worktree`, but the SDK's programmatic `AgentDefinition` type does not include this field. This appears to be a gap.
3. **No `WorktreeCreate`/`WorktreeRemove` in SDK hooks.** These hook events exist in settings.json hooks but are NOT in the SDK's `HookEvent` type (which only has: PreToolUse, PostToolUse, PostToolUseFailure, Notification, UserPromptSubmit, SessionStart/End, Stop, SubagentStart/Stop, PreCompact, PermissionRequest, Setup, TeammateIdle, TaskCompleted, ConfigChange).
4. **No `ExitWorktree` in SDK v0.2.49.** Added in CLI v2.1.72.

---

## 4. Internal Architecture (from cli.js)

### Worktree Session State

The runtime maintains a global `yg` variable (named `currentWorktreeSession` in source):

```javascript
// Demangled from cli.js
let currentWorktreeSession = {
  originalCwd: string,       // where we were before
  worktreePath: string,      // .claude/worktrees/<name>/
  worktreeName: string,      // the name
  worktreeBranch: string,    // worktree-<name>
  originalBranch: string,    // branch we were on
  originalHeadCommit: string,// HEAD at creation time
  sessionId: string,
  tmuxSessionName?: string   // if --tmux was used
};
```

### Key Functions (demangled)

- `createWorktreeForSession(sessionId, name, tmuxName, options?)` — Creates worktree + sets global state
- `createAgentWorktree(name)` — Creates worktree for a subagent (no global state change)
- `removeAgentWorktree(path, branch, gitRoot)` — git worktree remove --force + branch delete
- `hasWorktreeChanges(path, headCommit)` — Checks for uncommitted files OR commits ahead of head
- `keepWorktree()` — Preserves worktree, resets cwd to original
- `cleanupWorktree()` — Removes worktree + branch, resets cwd
- `resolveMainRepoRoot(cwd)` — Detects if we're in a worktree, returns main repo path
- `getCurrentWorktreeSession()` — Returns global `yg` or null

### Worktree vs Agent Worktree

Two distinct code paths:
- **Session worktree** (`createWorktreeForSession`): Sets global state (`yg`), triggers exit prompt, used by `--worktree` flag and `EnterWorktree` tool
- **Agent worktree** (`createAgentWorktree`): Does NOT set global state, returns data, cleanup is handled by the Task tool's completion logic

### Settings Hooks (settings.json, NOT SDK)

```json
{
  "hooks": {
    "WorktreeCreate": [{
      "type": "command",
      "command": "echo 'worktree created: $WORKTREE_NAME'"
    }],
    "WorktreeRemove": [{
      "type": "command",
      "command": "echo 'worktree removed: $WORKTREE_PATH'"
    }]
  }
}
```

These fire during creation/removal but are informational — they can abort creation (exit code 2) but cannot prevent removal.

### Symlink Configuration

```json
{
  "worktree": {
    "symlinkDirectories": ["node_modules", ".cache"]
  }
}
```

Prevents duplicating large directories across worktrees. Must be explicitly configured.

---

## 5. Relevance to OBS Platform

### Current OBS Worktree Status

From `docs/research/vault-specs-cross-reference.md`:
- **V3: Worktree Support Proposal** — Partially implemented. `OBS_VAULT_PATH` env var works, but TASKNOTES_PORT forwarding, setup script, and doc updates NOT done.
- **Worktree parallel execution** — NOT implemented. Broader scope (separate Obsidian instances).

### Can We Use `isolation: "worktree"` for OBS Subagents?

**Technically yes, but it's tricky for our use case:**

1. **The isolation is for git repos.** Our vault is a git repo, so `isolation: "worktree"` would create a worktree of the vault. But the vault agent reads/writes vault files as its primary function — an isolated worktree copy of the vault would be *disconnected* from the main vault, Obsidian, and the TaskNotes server. This is useful for code changes but counterproductive for vault operations.

2. **For obs codebase changes by subagents:** This is where isolation shines. If we had subagents that modify obs code (e.g., writing scripts, updating configs), `isolation: "worktree"` would let them work without conflicts. But we don't currently do this — obs code changes happen in separate sessions.

3. **For parallel safe vault writes:** Worktrees don't solve this. Multiple worktrees of the same vault would create merge conflicts. The right solution is probably file-level locking or coordination, not git worktrees.

### What's Actually Useful for OBS

1. **`EnterWorktree` tool control via `disallowedTools`:** We already set tool restrictions for subagents. If worktree creation is undesirable (it usually is for vault work), we can explicitly disallow it.

2. **Agent frontmatter `isolation: worktree` for code-touching agents:** If we create `.claude/agents/` definitions that modify the obs codebase itself, `isolation: worktree` in their frontmatter would be the right pattern.

3. **`WorktreeCreate`/`WorktreeRemove` hooks (settings.json):** If we ever enable worktrees, these hooks could set up TASKNOTES_PORT forwarding, Obsidian CLI config, etc. This is what the V3 worktree support proposal was aiming at.

4. **SDK gap is blocking:** The SDK's `AgentDefinition` type doesn't include `isolation`. Even if we wanted to programmatically define agents with worktree isolation via `options.agents`, we can't set isolation from the SDK. We'd need to use `.claude/agents/` frontmatter files instead. This is a known gap — frontmatter supports it, SDK doesn't.

### Recommendations

- **Don't use worktree isolation for vault-operating subagents.** The disconnection from Obsidian, TaskNotes, and the live vault makes it counterproductive.
- **Consider worktree isolation for code-modifying agents** (future: agents that write/test obs platform code). Use `.claude/agents/` frontmatter since SDK `AgentDefinition` lacks `isolation`.
- **Watch for SDK `AgentDefinition.isolation`** in future SDK versions — once available, we could define code-modifying agents programmatically with worktree isolation.
- **Finish V3 worktree support** (TASKNOTES_PORT forwarding, setup script) before enabling any worktree-based flows. Without it, worktree agents lose vault access.

---

## 6. Key Answers

**Q: Is worktree per-subagent or per-session?**
Both exist:
- `EnterWorktree` / `--worktree` → per-session (the whole session moves into a worktree)
- `isolation: "worktree"` on Task tool → per-subagent (only the child runs in a worktree)

**Q: Can the agent itself enter/exit a worktree?**
- **Enter:** Yes, via `EnterWorktree` tool. Available since v2.1.49.
- **Exit:** Only via `ExitWorktree` tool, added in v2.1.72. NOT in our SDK version (v0.2.49).
- **Guard:** Can't enter if already in a worktree. Can't enter outside a git repo.

**Q: Does the Agent SDK expose worktree management?**
No. The SDK has:
- Tool type definitions (EnterWorktreeInput/Output) — read-only types
- No programmatic creation API
- No `isolation` on AgentDefinition
- No WorktreeCreate/Remove in SDK hooks

**Q: Can we use this in obs?**
Limited value currently. Useful pattern for code-modifying agents in the future, but not for vault operations (vault agents need the live vault, not an isolated copy).

---

## Appendix: Source Evidence

### cli.js Function Map (demangled)

| Minified | Demangled | Purpose |
|----------|-----------|---------|
| `IU6` | `createWorktreeForSession` | Session worktree creation + global state |
| `VuY` | `createAgentWorktree` | Subagent worktree creation (no global state) |
| `eS8` | `removeAgentWorktree` | Git remove + branch delete |
| `wf1` | `cleanupWorktree` | Session worktree cleanup |
| `zf1` | `keepWorktree` | Preserve worktree on exit |
| `Ah8` | `hasWorktreeChanges` | Check for uncommitted/committed changes |
| `hG6` | `resolveMainRepoRoot` | Detect if in worktree, return main repo |
| `cI` | `getCurrentWorktreeSession` | Return global session state |
| `rS8` | (internal) | Low-level git worktree add + branch setup |
| `aS8` | (internal) | Post-creation setup (settings copy, hooks, symlinks) |
| `TuY` | (internal) | Symlink configured directories |
| `MJY` | (internal) | Team worktree removal |
| `ak4` | (internal) | Team cleanup including member worktrees |

### Settings Schema (from cli.js Zod schemas)

```
worktree: {
  symlinkDirectories: string[]  // dirs to symlink from main repo to worktrees
}
```

### Team Member Config (includes worktree)

```javascript
// From team member iteration in cli.js
member = {
  name: string,
  agentId: string,
  agentType: string,
  model: string,
  prompt: string,
  status: 'running' | 'idle',
  color: string,
  cwd: string,
  worktreePath?: string,  // <-- set if member has isolation: worktree
  // ...
}
```
