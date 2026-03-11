# Context Fork in Skills Frontmatter — Spike Findings

Date: 2026-03-05
Spikes: `spike_22` through `spike_28` in `obs/spikes/`
Test project: `/tmp/context-fork-spikes/` with 11 test skills

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [Mechanism: It's Just the Task/Agent Tool](#mechanism-its-just-the-taskagent-tool)
- [JSONL Structure](#jsonl-structure)
- [Conversation Context: Fully Isolated](#conversation-context-fully-isolated)
- [CLAUDE.md: Loaded in Fork](#claudemd-loaded-in-fork)
- [Cache Behavior](#cache-behavior)
- [Tool Sets by Agent Type](#tool-sets-by-agent-type)
- [allowed-tools: No Effect on Forked Subagent](#allowed-tools-no-effect-on-forked-subagent)
- [Inline vs Forked Skill Flow](#inline-vs-forked-skill-flow)
- [Comparison: context:fork vs fork_session vs self_fork](#comparison-contextfork-vs-fork_session-vs-self_fork)
- [Practical Implications for OBS Agent](#practical-implications-for-obs-agent)
- [Spike Index](#spike-index)

---

## Executive Summary

**`context: fork` in skills frontmatter makes the skill run as a subagent via the same
mechanism as the Task/Agent tool.** It does NOT create a session fork (`fork_session=True`).

Key facts:
1. **Same mechanism as Task tool** — both produce JSONL in `{session}/subagents/`
2. **No conversation history** — the subagent starts fresh with only the skill content as prompt
3. **CLAUDE.md IS loaded** — project settings are inherited
4. **Cache is independent** — subagent has a different system prompt prefix (~15K vs ~21K tokens)
5. **Cache is shared between subagents** — second invocation of same skill type hits 100%
6. **Tool set is reduced** — 15 tools (default) vs 22 for main agent; varies by `agent:` type
7. **`allowed-tools` has no effect** on the forked subagent's tool set

---

## Mechanism: It's Just the Task/Agent Tool

When a skill with `context: fork` is invoked via the `Skill` tool:

1. The CLI detects `context: fork` in the skill's YAML frontmatter
2. The CLI spawns a subagent using the **same mechanism** as the Task/Agent tool
3. The `agent:` field determines the subagent type (default: `general-purpose`)
4. The skill's markdown content becomes the user prompt
5. The subagent runs to completion
6. Its output is returned to the main agent as a `ToolResultBlock`

**Evidence:**
- JSONL for forked skills and Task tool subagents both live in `{session_id}/subagents/agent-{id}.jsonl`
- Cache stats are nearly identical (~15K baseline for both)
- Tool sets match exactly (both get 15 tools for `general-purpose`)
- The `ToolResultBlock` for forked skills says `(forked execution)`

```
# Spike 27: All three subagent types in the same session
SUBAGENT #1 (forked-greeter):   read=15,277 create=596   total=15,875 (96% cached)
SUBAGENT #2 (forked-greeter):   read=15,873 create=0     total=15,875 (100% cached)
SUBAGENT #3 (Task tool):        read=15,277 create=534   total=15,814 (97% cached)
```

The numbers are nearly identical. Subagent #2 hitting 100% cache confirms subagents share
cache with each other (same system prompt prefix).

---

## JSONL Structure

### Main Session (inline skill)

```
[0] queue-operation                              ← metadata
[1] queue-operation                              ← metadata
[2] user           "Use the inline-greeter..."   ← user request
[3] assistant      thinking                      ← model thinks
[4] assistant      Skill({"skill":"inline-greeter"})  ← calls Skill tool
[5] user           tool_result "Launching skill" ← Skill tool ack
[6] user           text "Base directory...When this skill..." ← SKILL CONTENT INJECTED
[7] assistant      thinking                      ← model processes skill inline
[8] assistant      text "INLINE_GREETER_RESPONSE..." ← result in context
```

**Key: Inline skills inject the skill content as a user message (entry 6).** The main
agent processes it with full conversation history available.

### Main Session (forked skill)

```
[0] queue-operation                              ← metadata
[1] queue-operation                              ← metadata
[2] user           "Use the forked-greeter..."   ← user request
[3] assistant      thinking                      ← model thinks
[4] assistant      Skill({"skill":"forked-greeter"})  ← calls Skill tool
[5] user           tool_result 'Skill "forked-greeter" completed (forked execution).\nResult:\nFORKED_...'
                                                 ← COMPLETE RESULT RETURNED
[6] assistant      thinking + text               ← model summarizes
```

**Key: Forked skills return the complete result in the tool_result (entry 5).** The main
agent just sees the final output, not the skill content.

### Subagent Session (forked skill)

```
{session_id}/subagents/agent-{agent_id}.jsonl

[0] user    "Base directory for this skill: .../.claude/skills/forked-greeter\n\n[skill content]"
[1] assistant  text: "FORKED_GREETER_RESPONSE: ..."  (with cache stats)
```

**Key: The subagent JSONL is minimal — just user prompt + assistant response.** The user
prompt starts with a "Base directory" preamble followed by the full SKILL.md content.
No conversation history from the parent. No system prompt entries visible in the JSONL
(they're sent by the CLI to the API directly, not recorded as JSONL entries).

---

## Conversation Context: Fully Isolated

**The forked subagent has ZERO access to conversation history.**

### Experiment (Spike 24)

1. Turn 1: Tell main agent "secret code word: PURPLE_ELEPHANT_42"
2. Turn 2: Invoke `inline-recall` skill → **YES, recalls the code word** (inline = same context)
3. Turn 3: Invoke `forked-recall` skill → **NO, cannot recall anything** (forked = isolated)

```
INLINE_RECALL: YES - I can see previous context. The conversation mentioned:
You asked me to remember the secret code word "PURPLE_ELEPHANT_42"...

FORKED_RECALL: NO - I have no access to previous conversation history.
I only see this skill prompt.
```

**The subagent's JSONL confirms this** — it only contains the skill content as the user
prompt. No parent session entries are copied. This is fundamentally different from
`fork_session=True` which copies the entire parent JSONL.

---

## CLAUDE.md: Loaded in Fork

**The forked subagent DOES see CLAUDE.md content.** Project settings are inherited.

### Experiment (Spike 26)

The test CLAUDE.md contained: `TEST_MARKER: This text proves CLAUDE.md was loaded into context.`

```
Subagent response:
  PROBE_MARKER: Yes, I can see it in the system reminder. The full line is:
  "TEST_MARKER: This text proves CLAUDE.md was loaded into context."
```

This works because the CLI passes `setting_sources=["project"]` (or equivalent) to
the subagent process, which reads `.claude/` from the project directory. The CLAUDE.md
content is injected into the system prompt, not the conversation history.

---

## Cache Behavior

### System Prompt Sizes

| Context | System Prompt Tokens | Notes |
|---------|---------------------|-------|
| Main agent | ~21,200 | Full tool set (22 tools) + all skills metadata |
| Default subagent | ~15,800 | Reduced tool set (15 tools) + CLAUDE.md |
| Explore subagent | ~14,900 | Read-only tools (12 tools) + CLAUDE.md |
| Plan subagent | ~13,400-15,000 | Varies by model |
| Task tool subagent | ~15,800 | Same as default forked subagent |

### Cache Sharing

**Subagents do NOT share cache with the main agent.** Their system prompt prefix is
different (different tool set, different agent instructions). But they DO share cache
with each other.

```
# Spike 27: Two forked-greeter invocations in the same session
Subagent #1: cache_read=15,277  cache_create=596   (96% cached)
Subagent #2: cache_read=15,873  cache_create=0     (100% cached)
                        ^^^^^^
                        15,277 + 596 = 15,873 — perfect match
```

The second invocation gets 100% cache because:
1. The system prompt is identical (same agent type)
2. The user prompt is identical (same skill content)
3. API prefix cache from subagent #1 covers the entire prefix

### Main Agent Cache Is Unaffected

The main agent's cache continues to grow with its own conversation:

```
Main turn 0:  read=19,887 create=1,370 total=21,267  (94% cached)
Main turn 5:  read=21,336 create=248   total=21,592  (99% cached)
Main turn 12: read=22,080 create=337   total=22,425  (98% cached)
```

Subagent invocations don't disrupt the main agent's cache alignment.

---

## Tool Sets by Agent Type

### Main Agent (22 tools)

```
Task, TaskOutput, Bash, Glob, Grep, ExitPlanMode, Read, Edit, Write,
NotebookEdit, WebFetch, TodoWrite, WebSearch, TaskStop, AskUserQuestion,
Skill, EnterPlanMode, EnterWorktree, TeamCreate, TeamDelete, SendMessage, ToolSearch
```

### Default Forked Subagent (15 tools)

```
Bash, Glob, Grep, Read, Edit, Write, NotebookEdit, WebFetch, WebSearch,
Skill, EnterWorktree, TeamCreate, TeamDelete, SendMessage, TodoWrite
```

**Missing (7 tools):** Task, TaskOutput, AskUserQuestion, ExitPlanMode, TaskStop,
EnterPlanMode, ToolSearch

### Explore Agent Subagent (12 tools)

```
Bash, Glob, Grep, Read, WebFetch, WebSearch, Skill, EnterWorktree,
TeamCreate, TeamDelete, SendMessage, TodoWrite
```

**Additionally missing from Explore (vs default):** Edit, Write, NotebookEdit

### Plan Agent Subagent

Similar to Explore — focused on read-only tools. Exact set varies.

### Task Tool Subagent

Same 15-tool set as default forked subagent. Identical mechanism.

---

## allowed-tools: No Effect on Forked Subagent

### Experiment (Spike 28)

Skill with `allowed-tools: Read, Glob` and `context: fork`.

**Result:** The subagent still had all 15 tools (Bash, Edit, Write, etc.) and could
freely use Bash to run commands.

**Why:** `allowed-tools` controls **permission auto-approval** in the main session
context — it tells Claude Code which tools can run without user confirmation when the
skill is active. In `bypassPermissions` mode (as used by ClaudeSDKClient), all tools
are already auto-approved. And regardless, `allowed-tools` doesn't affect the
subagent's tool set — that's determined by the `agent:` field.

---

## Inline vs Forked Skill Flow

### Inline (`context:` not set or `context: inline`)

```
User → "Use skill X"
  ↓
Main Agent → calls Skill("X")
  ↓
CLI: returns "Launching skill: X"
  ↓
CLI: injects skill content as user message
  ↓
Main Agent → processes skill content WITH full conversation history
  ↓
Main Agent → produces output
```

**The skill content is additional context for the main agent.** The agent retains all
prior conversation, can reference earlier messages, and the response stays in the main
conversation flow.

### Forked (`context: fork`)

```
User → "Use skill X"
  ↓
Main Agent → calls Skill("X")
  ↓
CLI: detects context: fork in frontmatter
  ↓
CLI: spawns subagent (same mechanism as Task tool)
  ↓
Subagent: receives skill content as user prompt (NO conversation history)
  ↓
Subagent: processes and returns result
  ↓
CLI: returns result to main agent as tool_result with "(forked execution)" label
  ↓
Main Agent → sees the complete result, can summarize/relay
```

**The skill runs in complete isolation.** The subagent has no conversation history,
gets a smaller tool set, and its result is injected back as a tool result.

---

## Comparison: context:fork vs fork_session vs self_fork

| Feature | `context: fork` | `fork_session=True` | `self_fork` MCP tool |
|---------|-----------------|--------------------|--------------------|
| **Mechanism** | Task/Agent subagent | JSONL copy + resume | JSONL fork via query() |
| **Conversation history** | NONE | FULL (all parent entries) | FULL (all parent entries) |
| **CLAUDE.md** | Yes | Yes | Yes |
| **System prompt** | Subagent prompt (~15K) | Same as parent (~21K) | Same as parent (~21K) |
| **Cache shared with parent?** | NO (different prefix) | YES (same prefix, 89-95%) | YES (same prefix, 89-95%) |
| **Cache shared between instances?** | YES (100% on repeat) | Depends on timing | Depends on timing |
| **Tool set** | Reduced (15) | Full parent set | Full parent set |
| **JSONL location** | `{session}/subagents/` | New top-level JSONL | New top-level JSONL |
| **Can recall parent context?** | No | Yes | Yes |
| **Triggered by** | Skill tool | SDK option | Agent's MCP tool call |
| **Use case** | Isolated tasks | Context-aware branching | Agent-controlled subtasks |

### When to Use What

**`context: fork` (subagent):**
- Task is self-contained (doesn't need conversation history)
- You want tool isolation (smaller, safer tool set)
- Research, exploration, code generation from a spec
- Template-driven tasks that don't need conversational context

**`fork_session=True` (session fork):**
- Task needs full conversation context
- You want the fork to "remember" everything discussed
- Cost optimization via cache reuse (89-95% hit rate)
- Branching a conversation into parallel explorations

**`self_fork` MCP tool:**
- Agent decides when to fork (not user/skill-driven)
- Background tasks that should inherit full context
- Results delivered back via message queue

---

## Practical Implications for OBS Agent

### context:fork Does NOT Replace self_fork

The OBS agent's `self_fork` MCP tool creates **session forks** (`fork_session=True`) that
inherit the full conversation. `context: fork` in skills creates **isolated subagents**
with no conversation history. These are fundamentally different use cases.

### Potential Uses for context:fork Skills

1. **Structured research tasks** — skills like "analyze this code pattern" that don't need
   conversation context, just the skill instructions and codebase access
2. **Template execution** — skills that follow a fixed procedure (deploy, generate docs)
3. **Tool-restricted operations** — using `agent: Explore` to enforce read-only access
4. **Parallel skill execution** — the main agent can invoke multiple forked skills and
   they'll share cache with each other (100% on repeat)

### Cache Implications

- Forked skill subagents have a **~15K token system prompt** (vs ~21K for main agent)
- They do NOT benefit from the main agent's cache
- But multiple forked skill invocations DO benefit from each other's cache
- For cost optimization, prefer `fork_session=True` when you need context AND cache

### SDK Compatibility

`context: fork` works fully through `ClaudeSDKClient` with `setting_sources=["project"]`.
The Skill tool is available to the main agent, and the CLI handles all forking mechanics.
No special SDK configuration needed beyond ensuring skills are in `.claude/skills/`.

---

## Spike Index

| File | What it tests |
|------|--------------|
| `spike_22_context_fork_basic.py` | Basic invocation: inline vs forked skill via SDK |
| `spike_23_context_fork_jsonl.py` | Initial JSONL location search (wrong path) |
| `spike_23b_jsonl_deep_analysis.py` | Deep JSONL structure: entry types, cache stats, parent chains |
| `spike_24_context_fork_context_and_tools.py` | Context inheritance (code word), tool listing, CLAUDE.md |
| `spike_25_context_fork_agent_types.py` | Agent type comparison: default, Explore, Plan |
| `spike_26_context_fork_probes.py` | Full probe: CLAUDE.md marker, tools, cwd, history, model |
| `spike_27_context_fork_cache_relationship.py` | Cache analysis: main vs subagent vs repeat vs Task tool |
| `spike_28_context_fork_allowed_tools.py` | allowed-tools restriction test (no effect on fork) |

Test project: `/tmp/context-fork-spikes/` with 11 skills in `.claude/skills/`

All spikes run with: `.venv/bin/python spikes/<name>.py`
