# Agent Self-Awareness & Code Execution — Feature Spec

## Original User Request (lightly edited for clarity)

### Message 1: Initial Feature Request

I want to give the agent an MCP tool that would let it see its own session ID and all the different metadata about the session itself. So whatever we receive in the callback function for a tool call — I just want to echo it back at the agent so it can know its session ID, it can know other metadata (context window, if that's what's passed, etc.).

Another thing is I want to give it access to the actual code so that it could potentially run code live. We might put some eval functions — maybe we send this MCP tool to basically just call an eval function from within the code itself, so that the agent can potentially do what this first MCP tool I described without us having to pre-define the MCP tool.

I would think of this as a very general purpose thing — basically giving agent access to the code that it's currently running so it can run something on top of it. Like maybe launch an extra thing, or maybe it's gonna find and figure out these different features that we haven't gotten to yet. Like for example, downloading files from Telegram so it can process them. Versus asking us to dedicate an MCP tool for it. I don't want to specifically make file processing because there will be a lot of things like this that are kind of a one-time thing. I just want to give the agent a lot of power to run code within its own agent loop.

I think it's fine to do it through an MCP tool call. Ideally I would have it modify its own code — the Python code — and have it hot-applied to the currently running session. But I don't know if that's possible.

I want to make it safe so that it doesn't break itself when it just runs bad code — it should just return an exception. If it runs code that throws an exception, that exception is automatically handled. And if it runs some long elaborate code that breaks the whole loop, it doesn't actually break the loop.

I think it could be as simple as an exception handler around the exact statement. I wonder if that's going to be too limiting — that it can't actually hot-modify its own code. For now, hot-modifying code is not the thing I want per se. I kind of just want to give it better access to the currently running code.

I want it to be very meta — like a self-aware software. It should be able to fork itself without having a special MCP tool, spawn sub-agents as SDK clients instead of the native sub-agent tool, see actual JSON of messages it's ingested, etc.

It is a high-stakes environment with lots of complexities and edge cases. I want a 99% safety guarantee, not 100% at the cost of narrowing capabilities too much.

### Message 2: Follow-up Concerns

On the high level, the goal is self-aware software. It should be able to find some variable somewhere else.

**Async wrapper concern:** Why would we add this wrapper? Maybe the agent wants to do non-async code. We can just give it a suggestion that it should wrap itself — I don't want to take away flexibility. I would rather give it a suggestion.

**Timeout:** I want the agent to be able to override it. If something times out and the agent is annoyed by it, it can set a longer timeout. That would be good.

**Telegram stuff:** The agent is running on my computer, so it can see the actual code and modify it or get API keys. So I don't know if we need the run_code tool for Telegram — it can just run Bash and do the same thing. But will it know that a file arrived? Is it just getting skipped at the fundamental level so the agent is completely unaware?

**Persistent namespace:** Makes sense. But can the agent access OTHER variables in the process? Is there a way without wrapping the process in a debugger? Is that actually feasible? Could it redefine a function on the fly — an async function that gets called elsewhere — inject a new version of a hook function or define a custom tool on the fly?

**SDK client access:** Yes, give the agent access to the actual SDK client, with warnings about what we understand.

**Namespace overhead:** Does it add a lot? Do you have to manage that namespace? Is the definition going to be within a function?

**Telegram context:** I only want to make sure the agent knows it received an image or something. Then I can actually ask it to figure out how to use the Bot API. Though this can interfere with polling?

### Message 3: Background Execution Concern

Is there any added complexity with this being async? Is the agent stuck waiting for the async function to come back? The agent should not be blocked waiting.

### Message 4: Telegram Subagent Result Delivery

I noticed that in Telegram evals, when the agent dispatches subagents and then stops talking, it doesn't get the result of subagents finishing. So the same would be the case for self-forking and running code. This is something we could probably refactor along the way.

---

## Spike Results

### Spike 1: `exec()` with Top-Level Await (VERIFIED)

**File:** `spikes/exec_async_spike.py`

**Critical finding:** `exec()` always returns `None`, even for async code. You MUST use `eval()` on a compiled code object to get the coroutine back.

**The correct pattern:**
```python
import ast
from inspect import CO_COROUTINE

compiled = compile(code, '<run_code>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
is_async = bool(compiled.co_flags & CO_COROUTINE)

if is_async:
    coro = eval(compiled, namespace)
    result = await asyncio.wait_for(coro, timeout=timeout)
else:
    exec(compiled, namespace)
```

**All tests passed:**
- Pure sync code: works normally
- Async code with bare `await`: works, returns coroutine via `eval()`
- Mixed sync + async: entire block becomes async, both work
- Sync exception: `ZeroDivisionError` caught normally
- Async exception: `ValueError` caught when awaiting
- Timeout (5s sleep, 1s limit): `asyncio.TimeoutError` raised correctly
- stdout/stderr capture: `contextlib.redirect_stdout/stderr` works for both sync and async
- Namespace persistence: variables accumulate across multiple calls

### Spike 2: SDK ResultMessage Fields (VERIFIED)

**File:** `spikes/sdk_message_fields.md`

**ResultMessage fields available:**
- `session_id: str` — unique session identifier
- `num_turns: int` — cumulative turn count
- `duration_ms: int` — total elapsed time
- `duration_api_ms: int` — API time specifically
- `total_cost_usd: float | None` — cumulative cost
- `usage: dict | None` — token usage (structure varies)
- `is_error: bool` — whether turn had errors
- `result: str | None` — optional result string
- `structured_output: Any` — optional structured output

**AssistantMessage fields:** `content`, `model`, `parent_tool_use_id`, `error`

**Not available:** Context window remaining, per-turn cost breakdown, no Session object with cumulative state.

**Implication:** Must cache the latest `ResultMessage` in session/hook state since it's ephemeral. The `session_info` tool reads from this cache.

### Spike 3: Telegram Non-Text Message Handling (VERIFIED)

**Finding:** All non-text messages are **silently dropped**.

- Handler registration uses `filters.TEXT & ~filters.COMMAND` — only text reaches the agent
- No handlers for photo, document, voice, video, sticker, etc.
- No `file_id` extraction, no forwarding of file metadata
- The agent has ZERO awareness when someone sends a file

**Fix needed:** Add media handlers that forward structured metadata to the agent, e.g.:
`[File received: document "report.pdf" file_id=ABC123 mime_type=application/pdf size=45000]`

### Spike 4: Monkey-Patching Bound Hooks (VERIFIED)

**File:** `spikes/monkey_patch_spike.py`

**Key findings:**
- **List mutation (`_checks[i] = new_fn`):** Works. HookPipeline iterates the list on each call, always sees current entries.
- **List reassignment (`_checks = [...]`):** Does NOT work. Creates a new list; old references still point to original.
- **Closure capture:** Python closures are late-binding — they DO see rebound variables (corrected the original hypothesis).
- **Object attribute replacement:** Works — attribute lookup is dynamic.

**Implication:** The agent can replace hook check functions on the fly by mutating `HookPipeline._checks` in place. It CANNOT define new MCP tools mid-session (tools are registered at connect time).

---

## Design Decisions

### D1: No Forced Async Wrapping

The `run_code` tool does NOT wrap agent code in `async def`. Instead, it uses `compile()` with `PyCF_ALLOW_TOP_LEVEL_AWAIT` and checks `CO_COROUTINE` flag. If the agent wrote `await`, we use `eval()` to get the coroutine and await it. If sync, we `exec()` normally. The agent writes whatever it wants — no restrictions.

### D2: Agent-Controlled Timeout

Timeout is a parameter of the tool: `run_code(code="...", timeout=60)`. Default ~30s. The agent can set it to 300 if it wants more time.

### D3: Persistent Namespace (REPL-like)

The namespace dict persists across `run_code` calls within a session. Variables, functions, imports defined in one call are available in the next. Near-zero overhead — it's just a dict on the tool closure or hook state. Cleared on session reset.

### D4: Exposed Objects in Namespace

Pre-populated with:
- `config` — OBSConfig instance
- `hook_state` — HookState (message_queue, interrupt_flag, background_tasks, session_id)
- `session_manager` — SessionManager (has the SDK client)
- `asyncio` — for async operations
- `Path` — for file operations
- Standard builtins

The agent can also `import` anything in the Python environment (full access to all installed packages).

### D5: SDK Client Access — Allowed With Warnings

The agent gets access to the `ClaudeSDKClient` via `session_manager._client`. Tool description warns that calling `client.query()` on the main client would interfere with the current conversation turn.

### D6: Background Execution

Same pattern as `self_fork` background mode:
- `background=false` (default): execute and return result
- `background=true`: launch as `asyncio.create_task()`, return immediately, results enqueued to `hook_state.message_queue`
- For sync code in background: use `asyncio.to_thread()` to avoid blocking event loop
- Task reference added to `hook_state.background_tasks` so daemon keeps SSE stream open

### D7: Safety — Exception Handling + Timeout

- All code execution wrapped in try/except
- Exceptions return traceback as tool result (not crash the process)
- Timeout via `asyncio.wait_for` (async) or thread timeout (sync background)
- Agent can't define new MCP tools mid-session (SDK limitation), but `run_code` IS the universal tool

### D8: stdout/stderr Capture

Using `contextlib.redirect_stdout` + `io.StringIO`. Both sync and async code output is captured. Return format: `{"stdout": "...", "stderr": "...", "result": "...", "error": "..."}`.

### D9: Hot Code Modification — Deferred

Not in scope for this sprint. `importlib.reload()` doesn't update existing instances. The agent CAN:
- Monkey-patch hook functions via `_checks[i] = new_fn`
- Define new functions/classes in the persistent namespace
- Import and modify module-level attributes

But it CANNOT:
- Add new MCP tools mid-session
- Reload modules and have running instances update

### D10: Telegram File Awareness — Prerequisite Fix

Separate from `run_code`. Add handlers for non-text messages in `telegram.py` that forward file metadata as structured text. The agent then uses `run_code` (or Bash) to download files via Bot HTTP API. No interference with polling — `getFile` is independent of `getUpdates`.

### D11: ResultMessage Caching for session_info

Cache the latest `ResultMessage` in `HookState` or `SessionManager`. The `session_info` tool reads from this cache. Updated after each turn in `ConversationRunner` where we already have `last_message`.

### D12: Telegram Background Fork/Code Result Delivery

The user observed that when the agent dispatches subagents (background forks) via Telegram and then stops talking, results are never delivered. This is the same issue that would affect background `run_code`. The background fork wake-up loop in `ConversationRunner` only runs within `runner.run()` — if the SSE/Telegram response stream has already closed, there's no loop to drain the queue.

**Fix:** The Telegram bot needs to poll `hook_state.message_queue` after the response stream ends, or the wake-up loop needs to be decoupled from the response stream. This may require refactoring the ConversationRunner or adding a persistent background task in the Telegram bot that watches the queue.

---

## Work Items

### Item 1: `session_info` MCP Tool (Small)
- Cache latest `ResultMessage` in runner/session state
- New `@tool("session_info", ...)` in `tools.py` that returns session_id, num_turns, duration, cost, etc.
- Unit test + eval scenario ("what is your session ID?" → uses tool → reports it)

### Item 2: `run_code` MCP Tool (Large)
- New `@tool("run_code", ...)` in `tools.py`
- `compile()` + `PyCF_ALLOW_TOP_LEVEL_AWAIT` + `eval()`/`exec()` pattern
- Persistent namespace with pre-populated objects
- `timeout` parameter (agent-controlled)
- `background` parameter (reuse existing background task infrastructure)
- stdout/stderr capture
- Exception handling → return traceback
- Tool description with usage guidance and warnings
- Unit tests for sync, async, exception, timeout, namespace persistence, background
- Eval scenarios:
  - "Use run_code to find your session ID" (introspection)
  - "Write code that throws an exception" (safety)
  - "Run something in the background" (background execution)

### Item 3: Telegram File Awareness (Medium)
- Add handlers for photo, document, voice in `telegram.py`
- Forward `[File received: ...]` metadata as text to the agent
- Agent uses run_code or Bash to download via Bot HTTP API
- Eval scenario: user sends file → agent acknowledges it (requires Telegram eval infrastructure)

### Item 4: Background Result Delivery Fix (Medium)
- Fix the issue where background fork/code results are lost when the response stream closes
- Affects both CLI daemon (SSE) and Telegram bot
- May require refactoring ConversationRunner's wake-up loop or adding persistent queue watchers
- Eval scenario: background fork completes after response → agent still receives result

---

## Architecture Context

### Current Tool Infrastructure (`tools.py`)
- `create_obs_tools(config, get_session_id, hook_state)` → MCP server
- Currently has one tool: `self_fork` (foreground + background modes)
- Background forks use `asyncio.create_task()` + `hook_state.background_tasks` + `hook_state.message_queue`
- Daemon's `ConversationRunner` has background fork wait loop that drains queue

### Key Files
- `src/obs_agent/tools.py` — MCP tools (add session_info + run_code here)
- `src/obs_agent/hooks.py` — HookState, HookPipeline, check functions
- `src/obs_agent/session.py` — SessionManager, ClaudeSDKClient lifecycle
- `src/obs_agent/runner.py` — ConversationRunner (background fork wait loop)
- `src/obs_agent/telegram.py` — TelegramBot, FragmentBuffer, handlers
- `src/obs_agent/daemon.py` — FastAPI SSE endpoints
- `tests/evals/scenarios/` — eval scenario markdown files
- `tests/evals/test_evals.py` — eval test runner

### Spike Files (reference implementations)
- `spikes/exec_async_spike.py` — exec/eval async pattern
- `spikes/monkey_patch_spike.py` — hook mutation tests
- `spikes/sdk_message_fields.md` — SDK type field inventory

---

## Open Questions

1. **Namespace reset:** Should `run_code` namespace reset on session reset, or persist across sessions? (Probably reset — matches session lifecycle.)
2. **What to name the tool:** `run_code`? `exec_code`? `python`? (Agent-facing name matters for discoverability.)
3. **Telegram file download path:** Should downloaded files go to a temp dir, the vault, or configurable? (Let the agent decide via run_code.)
4. **Background result delivery architecture:** Decouple from response stream (persistent watcher) vs. extend response stream (keep-alive)? Needs design.
5. **Eval for Telegram file awareness:** Requires sending actual files via Telegram test infrastructure — may need telethon or similar in eval harness.
