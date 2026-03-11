# Recursive Subagents & Custom Task Tool: Feasibility Report

**Date**: 2026-03-03
**Builds on**: `2026-03-03-sdk-subagent-team-protocol-report.md`

## Executive Summary

**Recursive subagents via MCP tools: PROVEN WORKING (Spike 18).** A custom `spawn_subagent` MCP tool that creates a `ClaudeSDKClient` internally achieves full three-level recursion: `Main Agent → Task (subagent) → MCP tool → ClaudeSDKClient (sub-subagent)`. Both foreground and background subagents can access MCP tools — the old limitation appears to have been lifted or was only for external MCP servers.

**Team integration: PARTIALLY ACHIEVABLE.** We cannot fake native team membership (env vars alone don't inject TaskCreate/TaskList tools — spike 17). However, we can replicate ~90% of team behavior by providing our own MCP equivalents of team tools and managing the file-based inbox/task protocol ourselves.

---

## 1. Key Spike Results (New)

| Spike | What | Result |
|-------|------|--------|
| 15/15b | MCP tool spawns sub-subagent | **Works after fixing tool signature** (args is a dict, not kwargs) |
| 16 | Background subagent MCP access | **MCP tools visible to both foreground and background** (same invocation error on both — no background-specific limitation) |
| 17 | Fake teammate via env vars | **Team tools NOT injected** — CLI needs internal spawning mechanism, not just env vars |
| 18 | Fixed MCP spawn (3-level recursion) | **FULLY WORKING** — Main→Subagent→MCP→Sub-subagent proven. 2 sub-subagents spawned, costs tracked |

### Spike 18 Proof (Three-Level Chain)

```
Main agent (haiku)
  → Task tool → delegator subagent (haiku)
    → mcp__recursive_tools__spawn_subagent → ClaudeSDKClient (haiku)
      → "Calculate 12 * 12" → "144"
    ← sub-subagent result: "144" (cost=$0.0076, 997ms, 1 turn)
  ← delegator: "The answer is 144"
← main: "Sub-subagent completed the calculation"

Total cost: $0.0702, 2 main turns
```

---

## 2. Background Agents and MCP Tools

**Finding: NOT a limitation anymore (or was only for external MCP servers).**

Spike 16 tested both foreground and background subagents calling an in-process MCP tool. Both had identical behavior — the tool was registered and callable from both contexts. The errors were due to incorrect function signatures (accepting `()` instead of `(args: dict)`), not background-specific restrictions.

**Caveat**: This was tested with in-process SDK MCP servers (`create_sdk_mcp_server`). External MCP servers (stdio/HTTP) might still have restrictions for background agents. We use in-process servers, so this doesn't affect us.

---

## 3. Architecture: What We Need to Build

### The Surgical Edit Point

The cleanest integration point is an **MCP server registered at `ClaudeAgentOptions.mcp_servers`** that provides:

1. **`spawn_subagent`** — replaces the Task tool for sub-subagent spawning
2. **`task_create` / `task_list` / `task_update` / `task_get`** — replaces the CLI-injected team task tools
3. **`send_message`** — writes to the native inbox JSON format
4. **`read_inbox`** — reads from the native inbox JSON

These MCP tools run in-process in the SDK host, so they can:
- Spawn new `ClaudeSDKClient` instances (recursion)
- Read/write the `~/.claude/teams/` and `~/.claude/tasks/` JSON files (team compat)
- Shell out to other CLIs like Codex or Gemini (cross-SDK)

### What We Rewrite vs. Reuse

| Component | Rewrite? | Notes |
|-----------|----------|-------|
| **Subagent spawning** | YES — new MCP tool | `spawn_subagent` creates `ClaudeSDKClient` instead of using Task tool |
| **Team config** | NO — read/write existing JSON | Same `~/.claude/teams/{name}/config.json` format |
| **Inbox protocol** | NO — read/write existing JSON | Same `~/.claude/teams/{name}/inboxes/{agent}.json` format |
| **Task management** | PARTIAL — MCP tools mimic TaskCreate/List/Update | Write to same `~/.claude/tasks/{name}/` files |
| **Polling/delivery** | YES — our MCP tools handle | But trivial: read JSON, check `read: false` |
| **Idle/shutdown** | PARTIAL — our coordinator handles | Write shutdown_request to inbox, read response |
| **tmux integration** | NO — skip (use in-process) | We use asyncio tasks, not tmux panes |
| **Rate limiting** | NO — each CLI process handles its own | Claude subscription auth handles this |
| **Codex/Gemini** | YES — new MCP tools per SDK | Each gets its own `spawn_codex`/`spawn_gemini` tool |

### Difficulty Assessment

| Task | Effort | Risk |
|------|--------|------|
| MCP spawn_subagent (Claude) | **Low** — proven in spike 18 | Low — works today |
| MCP task tools (CRUD) | **Low** — just JSON file I/O | Low — format is simple |
| MCP inbox read/write | **Low** — just JSON array ops | Low — format is known |
| Team coordinator logic | **Medium** — polling, task assignment, lifecycle | Medium — async coordination complexity |
| Native team interop | **Medium** — writing correct config.json so native agents can coexist | Medium — untested edge cases |
| Recursive depth control | **Low** — counter param in spawn_subagent | Low — straightforward |
| MCP spawn_codex | **Medium** — need to understand Codex CLI I/O | High — Codex CLI protocol unknown |
| MCP spawn_gemini | **Medium** — need to understand Gemini CLI I/O | High — Gemini CLI protocol unknown |

---

## 4. Implementation Plan

### Phase 1: Core MCP Task Tool (MVP)

Build a single Python module: `src/obs_agent/agent_tools.py`

```python
# MCP tools that replace the native Task tool
spawn_subagent     # Spawns ClaudeSDKClient, returns result
spawn_background   # Same but non-blocking, result via inbox
task_create        # Write to ~/.claude/tasks/{team}/
task_list          # Read all from ~/.claude/tasks/{team}/
task_update        # Modify task status/owner
send_to_inbox      # Append message to inbox JSON
read_inbox         # Read unread messages from inbox JSON
```

**Key design decisions:**
- `spawn_subagent` accepts `agents` dict to enable sub-subagents getting their own MCP tools (recursion!)
- Each spawned agent gets the same MCP server registered, enabling arbitrary depth
- Depth counter passed as parameter, decremented each level, hard limit at e.g. 5
- The MCP server instance is a singleton shared across all spawn calls in the same process

### Phase 2: Team Integration

Make our MCP-spawned agents compatible with native teams:
1. Write to the same config.json format when adding a new member
2. Write to the same inbox format so native agents can read our messages
3. Read from native agent inboxes so we can receive their messages
4. A native team lead can see our custom-spawned agents as team members

### Phase 3: Cross-SDK Agents

Add `spawn_codex` and `spawn_gemini` MCP tools:
1. Research Codex CLI (`codex`) input/output format
2. Research Gemini CLI (`gemini`) input/output format
3. Each tool shells out to the respective CLI
4. Parse stdout/stderr into our standard result format
5. These agents participate in the same team inbox system

---

## 5. Unknowns and Risks

### Known Unknowns

1. **Concurrent MCP tool calls**: Can the SDK handle multiple concurrent `spawn_subagent` calls from the same agent? (Each spawns a new CLI process.) Likely yes — asyncio handles this — but untested.

2. **Cost accumulation**: Each sub-subagent is a full CLI process with its own context window. A 3-level chain costs ~3x a single agent. Need cost tracking and budget limits.

3. **Startup latency**: Each `ClaudeSDKClient` connects to a new CLI process. The `initialize` call takes ~1-2 seconds. For latency-sensitive flows, this adds up.

4. **Context window isolation**: Sub-subagents don't share context with their parent. Passing context requires explicit serialization in the prompt. Large contexts will eat into the sub-subagent's context window.

5. **Session persistence**: Can we resume a sub-subagent's session? The `session_id` is returned in `ResultMessage` — theoretically we can pass `resume=session_id` in a subsequent `ClaudeAgentOptions`. Untested.

6. **Native team member detection**: When a native team lead lists team members, will it see our manually-written config.json entries? Likely yes (it reads the file), but the `backendType` and `tmuxPaneId` fields might cause issues.

7. **Codex/Gemini CLI availability**: Need to verify `codex` and `gemini` CLIs are installed, what their auth looks like, and whether they support the same stdin/stdout interaction model.

### Future Feature Risks

8. **Anthropic may add native recursion**: If Claude Code gets native recursive subagents, our MCP approach becomes redundant but harmless — we can migrate gracefully.

9. **Team protocol changes**: The `~/.claude/teams/` format is undocumented and could change between CLI versions. We should pin the CLI version and test after updates.

10. **MCP tool limitations in subagents**: Currently works for in-process SDK servers. If Anthropic restricts MCP access for subagents in a future release, our approach breaks. Low risk — MCP in subagents is useful for many use cases.

---

## 6. @tool Decorator — Correct Usage

**CRITICAL**: The `@tool` handler receives a single dict argument, NOT keyword arguments.

```python
# WRONG — causes slice object or TypeError
@tool("my_tool", "desc", {...})
async def my_tool(prompt: str, model: str = "haiku"):
    ...

# CORRECT — single dict argument
@tool("my_tool", "desc", {...})
async def my_tool(args):
    prompt = args.get("prompt", "")
    model = args.get("model", "haiku")
    ...

# CORRECT — and return the proper format
    return {"content": [{"type": "text", "text": "result"}]}
    # or for errors:
    return {"content": [{"type": "text", "text": "error msg"}], "is_error": True}
```

---

## 7. Codex/Gemini Integration Difficulty

### vs. Claude sub-subagents

With Claude, we control everything: `ClaudeSDKClient` gives us full access to the bidirectional protocol, message streaming, session management, etc.

With Codex/Gemini, we're shelling out to an opaque CLI binary:
- **Input**: command-line args or stdin
- **Output**: stdout text (format unknown, probably plain text or markdown)
- **No session persistence**: each invocation is stateless
- **No streaming**: we wait for the full response
- **No tool access**: the other SDKs don't have access to our MCP tools (unless they have their own MCP support)

**Implication**: Codex/Gemini teammates would be "dumb workers" — they receive a prompt, do their work, return a result. They can't participate in the rich team protocol (task assignment, inbox polling, etc.) without significant wrapper code.

**The effort to add Codex/Gemini is roughly equal** whether we rewrite team logic or not, because the bottleneck is understanding their CLI protocols, not our team infrastructure. Our team infra (inbox/task files) is simple JSON — the hard part is making a foreign CLI produce useful output and parsing it.

---

## 8. Recommended Approach

1. **Start with Phase 1**: Build `spawn_subagent` MCP tool (already proven in spike 18). Add task CRUD and inbox tools. Register as a single MCP server.

2. **Test with our existing daemon**: Register the MCP server in `session.py` alongside the existing `self_fork` tool. The main agent and all subagents get recursive spawning for free.

3. **Iterate on team compat**: Write to native config.json and inbox formats. Test with a real team lead (me, in this conversation) sending messages to MCP-spawned agents.

4. **Defer Codex/Gemini**: Research their CLIs separately. Build `spawn_codex`/`spawn_gemini` as standalone spikes before integrating into the team system.

---

## Appendix: All Spike Files

```
spikes/
├── 2026-03-03-sdk-subagent-team-protocol-report.md   # First report
├── 2026-03-03-recursive-subagent-feasibility-report.md  # This report
├── spike_01_basic_agent.py          # Basic AgentDefinition ✓
├── spike_02_agent_with_hooks.py     # SubagentStart/Stop hooks (don't fire)
├── spike_03_recursive_agent.py      # Coordinator→Worker (blocked)
├── spike_04_agent_tools.py          # Tool restriction ✓
├── spike_05_team_from_sdk.py        # TeamCreate from SDK ✓
├── spike_06_agent_with_mcp_tools.py # MCP tools in subagent (signature issue)
├── spike_07_agent_spawns_agent.py   # Recursive spawn (hangs)
├── spike_08_team_spawn_teammate.py  # Full team lifecycle ✓
├── spike_09_tool_introspection.py   # Tool lists: 22 main vs 19 subagent
├── spike_10_allowed_tools.py        # Can't grant Task tool
├── spike_11_pretool_hook.py         # PreToolUse doesn't fire for Task
├── spike_12_raw_messages.py         # SystemMessage types observed
├── spike_13_custom_transport.py     # Custom transport wrapper
├── spike_14_full_protocol.py        # Full protocol dump
├── spike_15_mcp_spawn_agent.py      # MCP spawn (wrong signature)
├── spike_15b_mcp_spawn_debug.py     # Debug version
├── spike_16_background_mcp.py       # Background subagent MCP ✓
├── spike_17_fake_teammate.py        # Fake team member (env vars not enough)
├── spike_18_mcp_spawn_fixed.py      # *** WORKING RECURSIVE SPAWN ***
└── spike_env.py                     # Env setup helper
```
