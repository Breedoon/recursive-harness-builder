# Codex as AgentTask Backend: Scope Report

Date: 2026-03-09
Author: Codex agent (analysis pass)

## Executive Summary

Adding Codex as another `AgentTask` type is feasible in phase 1 **without** refactoring the whole runtime into a full multi-SDK architecture.

Recommended approach for phase 1:
- Keep Claude path unchanged.
- Add a **task-level backend seam** (`claude` default, `codex` optional) only inside AgentTask launch/execute/resume/stop lifecycle.
- Implement Codex execution via a dedicated runner and persist Codex thread IDs for resume.

Not recommended for phase 1:
- Full `SessionManager`/`ConversationRunner` superclass refactor across all transports and tests.

Reason: the current system is deeply Claude-coupled in hooks, tool plumbing, session lifecycle, and Telegram orchestration. A full abstraction now is high-risk and slow.

## What Exists Today (Code Reality)

### 1) AgentTask is transport-orchestrated, not backend-abstracted
- `AgentTask`/`ForkTask` in `src/obs_agent/tools.py` route to hook callbacks (`fork_task_launcher`, `fork_task_outputter`, `fork_task_stopper`) rather than a backend interface.
- This makes AgentTask behavior owned by Telegram transport state, not by an SDK-neutral runtime.

Key references:
- `src/obs_agent/tools.py:79`
- `src/obs_agent/tools.py:93`
- `src/obs_agent/tools.py:151`
- `src/obs_agent/tools.py:211`

### 2) Session runtime is Claude-specific end-to-end
- `SessionManager` directly builds `ClaudeAgentOptions`, creates `ClaudeSDKClient`, injects Claude hook matchers and Claude MCP server.
- `ConversationRunner` imports Claude-specific classes (`TextBlock`, `ThinkingBlock`, `ToolUseBlock`, Claude CLI errors).

Key references:
- `src/obs_agent/session.py:17`
- `src/obs_agent/session.py:92`
- `src/obs_agent/session.py:101`
- `src/obs_agent/runner.py:17`
- `src/obs_agent/runner.py:198`

### 3) Telegram AgentTask execution assumes Claude fork/session model
- AgentTask records are `_ForkTaskRecord` with Claude-centric fields (`is_fork`, `parent_source_uuid`, `child_session_id`).
- Task launch path forks Claude JSONL when `fork=true`.
- Child execution is `_run_and_send(...)` using `ConversationRunner` (Claude), then result callback queues and lifecycle notifications.

Key references:
- `src/obs_agent/telegram.py:197`
- `src/obs_agent/telegram.py:1156`
- `src/obs_agent/telegram.py:1979`
- `src/obs_agent/telegram.py:4115`
- `src/obs_agent/telegram.py:4301`
- `src/obs_agent/telegram.py:4565`

### 4) Persistence has no backend dimension yet
- SQLite route/task persistence stores session IDs and mappings but no backend identifier.

Key references:
- `src/obs_agent/telegram_state_store.py:16`
- `src/obs_agent/telegram_state_store.py:49`
- `src/obs_agent/telegram_state_store.py:103`
- `src/obs_agent/telegram_state_store.py:152`

## Codex Capability Snapshot (Validated)

## Local package reality in workspace (`@openai/codex-sdk` 0.105.0)
- SDK offers `startThread()` and `resumeThread()`, no `forkThread()`.
- SDK streams events from `codex exec --experimental-json`.
- CLI supports `codex exec`, `codex exec resume`, `codex fork` (interactive), `codex app-server` (experimental), `codex mcp-server`.

Key references:
- `spikes/sdk-comparison/codex/node_modules/@openai/codex-sdk/dist/index.d.ts:262`
- `spikes/sdk-comparison/codex/node_modules/@openai/codex-sdk/dist/index.d.ts:270`
- `spikes/sdk-comparison/codex/node_modules/@openai/codex-sdk/dist/index.js:163`
- `spikes/sdk-comparison/codex/node_modules/@openai/codex-sdk/dist/index.js:208`

## Existing codex spike findings in repo
- Codex JSONL is sequential (no parent UUID DAG), resume appends to same file.
- Forking via manual JSONL copy works.
- App-server `thread/fork` exists as protocol option.
- Note: one older spike line about `thread.started` ID naming appears stale; current SDK typings/events use `thread_id`.

Key references:
- `spikes/sdk-comparison/codex/FINDINGS.md:28`
- `spikes/sdk-comparison/codex/FINDINGS.md:275`
- `spikes/sdk-comparison/codex/FINDINGS.md:292`
- `spikes/sdk-comparison/codex/FINDINGS.md:303`

## Additional check run on 2026-03-09
Generated current app-server protocol JSON schema locally (`codex app-server generate-json-schema`) and confirmed:
- request methods include `thread/start`, `thread/resume`, `thread/fork`, `turn/start`, `turn/interrupt`
- server can issue dynamic tool requests (`item/tool/call`)

Key references:
- `/tmp/codex-schema-1773087364/ClientRequest.json:45`
- `/tmp/codex-schema-1773087364/ClientRequest.json:69`
- `/tmp/codex-schema-1773087364/ClientRequest.json:93`
- `/tmp/codex-schema-1773087364/ClientRequest.json:429`
- `/tmp/codex-schema-1773087364/ClientRequest.json:477`
- `/tmp/codex-schema-1773087364/ServerRequest.json:106`

## Official docs checks (2026-03-09)
- Non-interactive mode documents JSON event streaming and resume.
- MCP docs show Codex can consume MCP servers from config.
- Agents SDK guide for Codex MCP server shows two tools (`codex`, `codex-reply`) for Python host use.

Links:
- https://developers.openai.com/codex/non-interactive-mode
- https://developers.openai.com/codex/mcp
- https://developers.openai.com/resources/guides/codex-with-agents-sdk

## Strategy Options

### Option A: Full SDK parent class abstraction now
Example idea:
- `BaseSessionManager` + `BaseConversationRunner`
- `ClaudeSessionManager`, `CodexSessionManager`
- all transports consume abstract interfaces

Pros:
- clean long-term architecture for multi-SDK
- one unified contract

Cons:
- high churn: `session.py`, `runner.py`, `tools.py`, `telegram.py`, hook pipeline, persistence, and a large test suite
- highest regression risk against currently working Claude behaviors

Verdict for phase 1: **not recommended**.

### Option B: Minimal reference/composition path (AgentTask-only backend seam)
- Keep current Claude main runtime untouched.
- Add backend switch only in AgentTask record + execute path.
- Codex child task execution handled by a dedicated codex runner.

Pros:
- lowest blast radius
- fastest time to first working Codex AgentTask
- easy fallback flag to disable Codex path

Cons:
- temporary duplication (Claude task execution path vs Codex task execution path)
- not yet a full multi-SDK platform

Verdict for phase 1: **recommended**.

### Option C: Hybrid, but app-server-first for Codex
- Same minimal seam as Option B
- Codex runner uses app-server protocol instead of `codex exec --json`

Pros:
- built-in `thread/fork`, `turn/interrupt`, richer protocol, dynamic tools path
- better future path to “same tools” parity

Cons:
- higher implementation complexity than `exec --json`
- protocol client work in Python

Verdict: strong phase 1.5/phase 2 candidate, optionally phase 1 if tool parity is mandatory immediately.

## Recommended Phase 1 Scope

Goal: Codex can be selected as AgentTask backend, execute work in child topic, report back, be resumed, and stopped.

### In scope
1. AgentTask backend selection
- Add optional tool arg: `backend` (default `claude`; optional `codex`)
- Keep existing behavior unchanged when omitted.

2. Codex child execution
- Add `CodexTaskRunner` module that can:
  - start new thread (`codex exec --json` flow)
  - resume thread (`codex exec resume <thread_id> --json` flow)
  - parse final assistant output and usage

3. Resume support
- Persist codex thread ID in task record (reuse `child_session_id` or add explicit `child_backend_session_id`)
- `resume=<task_id>` should relaunch same child with new prompt using saved thread ID.

4. Stop support
- terminate active codex subprocess and mark task stopped.

5. Parent callback and lifecycle behavior parity
- keep existing parent notification payload/callback flow so parent topic behavior stays stable.

### Explicitly out of scope for phase 1
- Cross-SDK fork semantics (`fork=true` from Claude session head into Codex context).
- Full hook parity (`PreToolUse`, `PostToolUse`, compaction hooks).
- Full “same tools” parity for Codex children.
- Full backend abstraction for main/topic sessions.

## Proposed Technical Design (Phase 1)

### A) Data model additions
- Extend `_ForkTaskRecord` with:
  - `backend: Literal["claude", "codex"] = "claude"`
  - `backend_model: str | None = None`
  - `backend_thread_id: str | None = None` (or repurpose `child_session_id` carefully)

Touchpoints:
- `src/obs_agent/telegram.py:197`

### B) Tool contract update
- `AgentTask` and `ForkTask` accept optional:
  - `backend` (`claude|codex`)
  - `model` (backend-specific model string)
- If `backend=codex` and `fork=true`, return explicit error in phase 1 (or force false with warning).

Touchpoints:
- `src/obs_agent/tools.py:93`
- `src/obs_agent/tools.py:211`

### C) Task launch and resume path
- `_launch_fork_task(...)` stores backend/model on record.
- `_resume_fork_task(...)` keeps same backend and thread id.

Touchpoints:
- `src/obs_agent/telegram.py:4115`

### D) Execution path split
- `_execute_fork_task(task_id)` branches by `record.backend`:
  - `claude`: existing `_run_and_send(...)`
  - `codex`: `CodexTaskRunner.run(...)`

Touchpoints:
- `src/obs_agent/telegram.py:4301`

### E) Stop/output path
- `_fork_task_stop(...)` supports backend-specific interrupt:
  - Claude: current `client.interrupt()`
  - Codex: terminate process handle tracked in task map

Touchpoints:
- `src/obs_agent/telegram.py:4604`

### F) Persistence
- add backend fields to SQLite task persistence so restarts do not silently reinterpret Codex tasks as Claude tasks.

Touchpoints:
- `src/obs_agent/telegram_state_store.py:49`
- `src/obs_agent/telegram_state_store.py:152`

## Risks and Mitigations

### 1) Semantic mismatch: `fork=true` and source UUID mapping
Risk:
- Current `fork=true` is Claude JSONL DAG-specific. Codex has no equivalent semantic in this runtime.

Mitigation:
- Explicitly gate phase 1: Codex AgentTask supports fresh/resume only.
- Keep fork semantics Claude-only until app-server fork support is integrated.

### 2) Hook parity gap
Risk:
- Codex path lacks current Claude hook integration assumptions.

Mitigation:
- keep Codex isolated to AgentTask children first.
- no attempt to share hook pipeline in phase 1.

### 3) CLI/protocol drift
Risk:
- Codex CLI and app-server are moving quickly.

Mitigation:
- pin supported Codex version.
- add contract tests over JSON event parsing and thread ID extraction.

### 4) Dependency/operational complexity
Risk:
- introducing Node/TS bridge adds operational burden in Python service.

Mitigation:
- phase 1 wrapper should call `codex` CLI directly from Python.
- only adopt TS/app-server bridge if needed for feature parity.

### 5) Task lifecycle regressions in Telegram
Risk:
- parent callbacks, state restoration, idle worker wakeups may regress.

Mitigation:
- do not alter callback payload contract.
- retain existing `_fork_task_*` registry behavior and add backend fields incrementally.

## Why I Recommend “Task-level seam first” over “parent class now”

- It satisfies your phase-1 product goal (Codex as AgentTask backend with resume/stop/reportback).
- It avoids destabilizing known-good Claude behavior and huge test surface.
- It creates a concrete seam you can later grow into full `AgentBackend` without a big-bang rewrite.

## Concrete Implementation Plan (Slice Order)

1. Add backend/model args to AgentTask tool schema and pass-through launcher args.
2. Extend `_ForkTaskRecord` + persistence schema for backend/thread ID.
3. Build `codex_task_runner.py` (exec JSON path).
4. Branch `_execute_fork_task` by backend.
5. Backend-specific stop path.
6. Add tests:
   - unit: launch arg routing and backend field persistence
   - unit: codex event parser
   - integration-smoke (guarded): codex launch + resume + stop

## Open Questions for You (Decisions Needed)

1. Should phase 1 hard-reject `backend=codex` with `fork=true`, or silently coerce to fresh session?
2. Do you want `backend` as explicit AgentTask arg, or model-name routing (e.g. model prefix selects Codex)?
3. For Codex tasks, should we reuse `child_session_id` as Codex thread ID, or keep separate fields for clarity?
4. Is immediate “same tools parity” required in phase 1, or can it be phase 2?
5. If tool parity is required early, do you prefer:
   - app-server dynamic tool handling, or
   - MCP server approach first?
6. Do you want Codex availability only for `AgentTask` children at first, with root topic always Claude?
7. Are we okay adding a hard runtime dependency check (`codex --version`) and returning actionable errors if missing?

## Appendix: Supporting References

Local code references:
- `src/obs_agent/tools.py`
- `src/obs_agent/session.py`
- `src/obs_agent/runner.py`
- `src/obs_agent/telegram.py`
- `src/obs_agent/telegram_state_store.py`

Existing codex research/spikes:
- `spikes/sdk-comparison/codex/FINDINGS.md`
- `spikes/sdk-comparison/codex/01_basic_thread.ts`
- `spikes/sdk-comparison/codex/05_jsonl_fork.ts`
- `spikes/sdk-comparison/codex/06_cli_fork_and_sdk_fork.ts`

Official docs:
- https://developers.openai.com/codex/sdk
- https://developers.openai.com/codex/non-interactive-mode
- https://developers.openai.com/codex/mcp
- https://developers.openai.com/codex/multi-agent
- https://developers.openai.com/codex/cli-command-reference
- https://developers.openai.com/codex/app-server
- https://developers.openai.com/resources/guides/codex-with-agents-sdk
