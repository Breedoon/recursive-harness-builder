# Vault Specs Cross-Reference Report

**Agent**: Vault Specs Cross-Reference (Agent 3)
**Date**: 2026-03-11
**Purpose**: Cross-reference all vault documentation about the OBS agent platform with the actual codebase to determine what was implemented vs. deferred vs. abandoned.

---

## 1. Specs & Proposals Inventory

### A. Codebase Specs (`docs/specs/`)

| # | Document | Date | Status Field | Actual Status |
|---|----------|------|-------------|---------------|
| S1 | `telegram-orchestration-platform.md` | 2026-03-01 | Spec (scoping) | **Vision document** — defines the north star. Most features are Tier 2+ and NOT implemented. Tier 0 (topic-routed messaging, per-topic session isolation) IS implemented. |
| S2 | `telegram-topics-phase1-scope.md` | ~2026-03-03 | "Implemented in worktree and live-validated" | **Fully implemented** — route-keyed session registry, per-topic session state, inline forks, /fork command, 54 deterministic test scenarios. |
| S3 | `telegram-forktask-phase2-scope.md` | 2026-03-03 | Proposed | **Fully implemented** — AgentTask/ForkTask MCP tools exist in `tools.py`, child topic creation, parent callback delivery, ForkTaskRecord in `telegram.py`. The tool was later generalized beyond fork-only to also support `fork=false` (fresh session). |
| S4 | `telegram-topic-scheduling-phase1.md` | 2026-03-10 | (implied complete) | **Fully implemented** — CronCreate/List/Delete tools, interval and on_topic_stop triggers, SQLite persistence, unified completion messages. Validation: 208+ tests passing. |
| S5 | `telegram-topic-scheduling-phase2.md` | 2026-03-10 | Approved for implementation | **Fully implemented** — real cron wall-clock mode, schedule_mode parameter, reset_session bool, inheritance (none/fork/all), from/until window, /unschedule. Validation: 208 passed + 5 live smoke + 1 stop test. |
| S6 | `agent-self-awareness.md` | undated | N/A (spec + spike results) | **Partially implemented** — `session_info` and `context_info` tools ARE implemented. `run_code` tool is NOT implemented. Telegram file awareness IS implemented (separate spec). SDK UUID monkey-patch IS implemented (`_sdk_patch.py`). |
| S7 | `telegram-file-ingestion.md` | 2026-02-28 | In progress | **Fully implemented** — `telegram_ingest.py` exists, `<system-note>` tags used, all file types handled, voice transcription pipeline, media group handling. Build notes confirm implementation + testing. |

### B. Codebase Plans (`docs/plans/`)

| # | Document | Date | Status Field | Actual Status |
|---|----------|------|-------------|---------------|
| P1 | `2026-02-11-obs-agent-design.md` | 2026-02-11 | N/A (initial design) | **Foundational document** — defines vault-as-knowledge-graph, skill system, session management, file conventions. All core architecture concepts from this document are implemented. MVP scope is complete. |
| P2 | `2026-02-19-telegram-message-flow-design.md` | 2026-02-19 | Approved, pending implementation | **Fully implemented** — per-turn messages, TurnEndEvent (→ StatusEvent in events.py), inline tool summaries, 200-char tool truncation, background queue auto-delivery, `(done)` sentinel, deletion of StatusMessageManager and typing loop. |
| P3 | `2026-02-25-eval-overhaul-implementation-plan.md` | 2026-02-25 | Proposed | **Partially implemented** — deterministic vs judge lane separation EXISTS (files: `deterministic.py`, `test_deterministic.py`, `test_falsifiability.py`, `README.md`). Profile system (`OBS_EVAL_PROFILE`) EXISTS. BUT: no `test_deterministic_cli.py` or `test_deterministic_telegram.py` separate files as planned. Fixture isolation improvements [UNCERTAIN — need to verify conftest.py changes]. Phase 6 falsifiability hardening partially done (test_falsifiability.py exists). |
| P4 | `2026-03-03-telegram-forktask-implementation-plan.md` | 2026-03-03 | Planning | **Fully implemented** — all 8 steps completed: ForkTaskRecord added, shared child-topic launch helper extracted (`_create_child_fork_topic`), ForkTask MCP tool added (now AgentTask with fork parameter), child user turn, parent callback, terminal states, timeout, done suppression. |

### C. Vault Proposals (`Drafts/`)

| # | Document | Location | Actual Status |
|---|----------|----------|---------------|
| V1 | Session Tree Proposal v3 | `Drafts/2026-03/session-tree-proposal.md` | **NOT implemented** — No fork markers, no service fork spawning, no session directory helper, no janitor trigger, no custom compaction prompt for fork points. Codebase grep confirms none of these exist. |
| V2 | Session Summarize Skill Draft v0.1 | `Drafts/2026-03/session-summarize-skill-draft.md` | **NOT implemented** — Draft skill spec only. Current `session-offboard` skill exists but doesn't follow the tree-aware model. The skill file references needing a rewrite. |
| V3 | Worktree Support Proposal | `Drafts/2026-03/worktree-support-proposal.md` | **Partially implemented** — `OBS_VAULT_PATH` env var in `config.py` works. BUT: TASKNOTES_PORT SDK env forwarding NOT done, setup script NOT created, doc updates NOT done. Vault task confirms this is still open. |
| V4 | ForkTask vs Task Stress Test Report | `Drafts/2026-03/forktask-vs-task-report.md` | **Research document** — 12-scenario comparison. Key finding: ForkTaskOutput was unusable (raw JSONL). Since then, tools were generalized to AgentTask with fork parameter and AgentTaskOutput/AgentTaskStop. [UNCERTAIN whether ForkTaskOutput fix was completed — the tool exists but quality of output unclear]. |
| V5 | OpenClaw Branching Research | `Drafts/2026-02/openclaw-branching-research.md` | **Research document** — Informed session tree design (V1). Pre-compaction memory flush pattern was adopted (D022 in hooks.py). |
| V6 | Compaction Prompt Research | `Drafts/2026-02/compaction-prompt-research.md` | **Research document** — Informed the decision to deny compaction (D022). No custom compaction prompt was implemented (the approach was to deny compaction entirely). |

### D. Vault Tasks (tagged @obs-code)

| Task | Size | Status | Implementation Status |
|------|------|--------|----------------------|
| Scheduled autonomous agent execution | L | none | **Partially covered** — CronCreate enables scheduling, but autonomous task pickup (agent wakes up and finds work to do) is NOT built. |
| Implement worktree support | L | none | **Partially covered** — See V3 above. |
| Expand agent lifecycle hooks | M | none | **Partially covered** — PreCompact hook exists. But generalized hook system for idle, pre-cache-expiry NOT built. |
| Hierarchical fork-aware session summaries | L | none (blocked) | **NOT implemented** — Depends on session tree (V1). |
| Add session resume command and MCP tool | M | none | **NOT implemented** — No resume-by-UUID or resume-by-JSONL-path tool exists. |
| Design better Telegram topic organization | L | none | **NOT implemented** — No pinning, icons, or color-coding for topics. |
| Add TTS voice response | M | none | **NOT implemented** — Research done (MLX-Audio + Kokoro-82M) but no code. |
| Worktree parallel execution | L | none | **NOT implemented** — Broader scope than V3 (separate Obsidian instances). |
| Add vector search to vault | L | none | **NOT implemented** — No embedding or semantic search code. |

---

## 2. Implementation Status Matrix

### Legend
- ✅ = Fully implemented and tested
- ⚠️ = Partially implemented
- ❌ = Not implemented
- 📋 = Spec/design exists but no code

### Core Runtime

| Feature | Status | Source Spec | Code Location | Notes |
|---------|--------|------------|---------------|-------|
| Agent CLI surface | ✅ | P1 | `cli.py` | Interactive terminal client |
| HTTP daemon surface | ✅ | P1 | `daemon.py` | FastAPI + SSE |
| Telegram adapter surface | ✅ | P1, P2 | `telegram.py` | Full per-turn message flow |
| OBSConfig with env vars | ✅ | P1 | `config.py` | 15+ env vars supported |
| SessionManager | ✅ | P1 | `session.py` | SDK client lifecycle |
| ConversationRunner | ✅ | P2 | `runner.py` | Queue continuation + fork wake-up |
| Event stream (Text/Status/Done) | ✅ | P2 | `events.py`, `runner.py` | Per-turn event model |
| Immutable file guard | ✅ | P1 | `hooks.py` | PreToolUse blocks writes to patterns |
| Hook pipeline (chained checks) | ✅ | S6 | `hooks.py` | HookPipeline with CheckFn chaining |
| HookState shared mutable state | ✅ | S1 | `hooks.py` | Per-route isolation achieved |
| PreCompact deny + memory extraction | ✅ | V5, V6 | `hooks.py` | D022: deny compaction, flush to vault |
| ForkRunner (memory extraction) | ✅ | P1 | `fork.py` | Uses ClaudeAgentOptions resume+fork |
| SDK UUID monkey-patch | ✅ | S6 §12, S1 | `_sdk_patch.py` | Preserves UUIDs from SDK messages |
| JSONL fork utility | ✅ | S1, S3 | `jsonl_fork.py` | Parent-chain traversal + copy |
| Context probe / stats | ✅ | S6 | `context_probe.py`, `context_stats.py`, `context_jsonl.py` | Token usage estimation |

### MCP Tools

| Tool | Status | Source Spec | Code Location | Notes |
|------|--------|------------|---------------|-------|
| AgentTask (fork=true/false) | ✅ | S3, V4 | `tools.py` | Generalized beyond original ForkTask-only scope |
| AgentTaskOutput | ✅ | S3 | `tools.py` | [UNCERTAIN] Whether output quality was fixed per V4 recommendations |
| AgentTaskStop | ✅ | S3 | `tools.py` | |
| ForkTask (compat alias) | ✅ | S3 | `tools.py` | Backward compatibility wrapper |
| ForkTaskOutput (compat alias) | ✅ | S3 | `tools.py` | |
| ForkTaskStop (compat alias) | ✅ | S3 | `tools.py` | |
| CronCreate | ✅ | S4, S5 | `tools.py` | Phase 2 params: schedule_mode, from/until, inherit |
| CronList | ✅ | S4, S5 | `tools.py` | |
| CronDelete | ✅ | S4, S5 | `tools.py` | |
| SendInboxMessage | ✅ | (no formal spec) | `tools.py` | File-based JSON inbox |
| ReadInbox | ✅ | (no formal spec) | `tools.py` | With include_read, mark_read, limit |
| session_info | ✅ | S6 Item 1 | `tools.py` | D11: ResultMessage caching |
| context_info | ✅ | S6 | `tools.py` | Context probe + snapshot |
| run_code | ❌ | S6 Item 2 | — | Full spec with spike verification exists but NOT implemented |

### Telegram Features

| Feature | Status | Source Spec | Notes |
|---------|--------|------------|-------|
| Per-turn chronological messaging | ✅ | P2 | Replaced editable StatusMessageManager |
| Inline tool summaries (200 char) | ✅ | P2 | Verbose tool observability |
| `(done)` sentinel notification | ✅ | P2 | Final completion marker |
| Background queue auto-delivery | ✅ | P2 | 3s polling |
| Forum topic support | ✅ | S2 | Route-keyed session registry |
| Per-topic session isolation | ✅ | S1, S2 | HookState + SessionManager per route |
| /fork user command | ✅ | S1, S2, S3 | Creates topic from reply context |
| Agent-initiated fork to topic | ✅ | S3, P4 | Via AgentTask/ForkTask MCP tool |
| ForkTaskRecord tracking | ✅ | P4 | In-memory task records in telegram.py |
| Child topic launch helper | ✅ | P4 | `_create_child_fork_topic()` |
| Parent callback delivery | ✅ | P4 | Queue-based callback to parent route |
| Terminal state handling | ✅ | P4 | completed/timed_out/interrupted/cleared/deleted/failed |
| File ingestion (all types) | ✅ | S7 | `telegram_ingest.py` with `<system-note>` tags |
| Voice transcription | ✅ | S7 | Calls transcribe.sh, injects transcript |
| Media group handling | ✅ | S7 | Aggregated into one agent turn |
| /tmp/obs-agent lifecycle | ✅ | S7 | Boot-ID subdirs, startup purge |
| Per-topic scheduling (interval) | ✅ | S4, S5 | Inactivity-based re-anchoring |
| Per-topic scheduling (cron) | ✅ | S5 | Wall-clock 5-field expressions |
| On-stop scheduling | ✅ | S4 | interval_seconds=0 trigger |
| Schedule persistence (SQLite) | ✅ | S4 | `telegram_state_store.py` |
| /unschedule command | ✅ | S5 | Topic-local schedule removal |
| Schedule inheritance | ✅ | S5 | none/fork/all modes |
| Unified completion message | ✅ | S4 | Context + optional schedule info |
| Reply-to for system messages | ✅ | S1 Phase 0 | `reply_to_message_id` threaded through send paths |
| In-memory message mapping | ✅ | S1 Phase 0 | telegram_msg_id → jsonl_uuid |
| Fork via reply | ✅ | S1 Phase 0 | Detects reply target, forks if not latest |
| Topic naming from description | ✅ | P4 | Parent name - child description |
| Reply-to-message threading | ✅ | S1 | Across telegram.py, hooks.py, runner.py, queueing.py |
| Pinned service messages | ❌ | S1 Tier 1 | Not yet implemented |
| /tree navigation command | ❌ | S1 Tier 1 | Not yet implemented |
| /sessions flat list | ❌ | S1 Tier 1 | Not yet implemented |
| Topic icons/colors by status | ❌ | S1 Tier 2 | Not yet implemented |
| SQLite message mapping persistence | ❌ | S1 Tier 2 | In-memory only |
| /totopic move command | ❌ | S1 Tier 2 | Not yet implemented |
| Background command topics | ❌ | S1 Tier 2 | Not yet implemented |
| Multi-SDK backend abstraction | ❌ | S1 Tier 3 | Not yet implemented |
| Codex CLI backend | ❌ | S1 Tier 3 | Not yet implemented |
| Delegated permissions | ❌ | S1 Tier 3 | Not yet implemented |
| Multi-user support | ❌ | S1 Tier 4 | Not yet implemented |
| Multi-bot load distribution | ❌ | S1 Tier 4 | Config supports multiple tokens; distribution logic NOT built |
| Userbot automation | ❌ | S1 Tier 4 | Not yet implemented |
| Chat lifecycle cleanup | ❌ | S1 Tier 4 | Not yet implemented |
| DM control plane | ❌ | S1 | Not yet implemented |

### Eval System

| Feature | Status | Source Spec | Notes |
|---------|--------|------------|-------|
| Judge-based eval lane | ✅ | P3 | `judge.py`, scenario .md files |
| Deterministic eval lane | ✅ | P3 | `deterministic.py`, `test_deterministic.py` |
| Profile system (smoke/feature/full) | ⚠️ | P3 | `OBS_EVAL_PROFILE` exists in test_evals.py. No separate test_deterministic_cli.py / test_deterministic_telegram.py as planned. |
| Falsifiability checks | ⚠️ | P3 | `test_falsifiability.py` exists. Coverage unclear. |
| Fixture isolation (ephemeral copies) | [UNCERTAIN] | P3 | Planned in Phase 5. Need to verify conftest.py changes. |
| Timing improvements (bounded idle) | [UNCERTAIN] | P3 | Planned in Phase 1. Current state unclear without reading platform code. |
| Scenario metadata tags | [UNCERTAIN] | P3 | Planned in Phase 2. Scenario files have tags visible in grep. |

### Session Management

| Feature | Status | Source Spec | Notes |
|---------|--------|------------|-------|
| Session tree with prefix segmentation | ❌ | V1 | No fork markers, service forks, janitor, directory helper |
| Hierarchical session summaries | ❌ | V2 | Draft skill spec only |
| Session resume by UUID | ❌ | Task | No MCP tool |
| Custom compaction prompt | ❌ | V1 | Current approach: deny compaction entirely |
| Git commits at fork points | ❌ | V1 | Not implemented |

### Infrastructure

| Feature | Status | Source Spec | Notes |
|---------|--------|------------|-------|
| Worktree support (OBS_VAULT_PATH) | ⚠️ | V3 | Env var works, but TASKNOTES_PORT forwarding, setup script, doc updates NOT done |
| run_code MCP tool | ❌ | S6 Item 2 | Full spike verification done, persistent namespace designed, NOT built |
| TTS voice response | ❌ | Task | Research only |
| Vector search / embeddings | ❌ | Task | Not started |

---

## 3. Design Decisions Log

Key design decisions extracted from specs, plans, and code, organized chronologically.

### D001 — Vault as Knowledge Graph (2026-02-11, P1)
Agent knowledge lives in vault markdown; runtime code lives outside. Files follow universal lifecycle (start small → grow → split). Summaries generated lazily.
**Status**: Adopted and enforced.

### D002 — Skills as Markdown Instructions (2026-02-11, P1)
Prefer loose skill instructions (markdown) over rigid code. Skills loaded natively via SDK `--append-system-prompt`.
**Status**: Adopted. 16 skills in `.claude/skills/`.

### D003 — SDK Session Caching Strategy (2026-02-11, P1)
Leverage SDK's ~1 hour prompt cache. If within window: continue. If expired: start fresh with context reload.
**Status**: Adopted. Cache window configurable via `OBS_CACHE_WINDOW`.

### D004 — Per-Turn Separate Messages (2026-02-19, P2)
Replace editable StatusMessageManager with per-turn separate messages. Tool summaries inline. All silent except final `(done)`.
**Status**: Fully implemented. StatusMessageManager deleted.

### D005 — Simple Polling Over Event-Driven Wake (2026-02-19, P2)
After researching OpenClaw's heartbeat wake pattern, chose simple 3s polling for background queue delivery. Simpler to implement and debug.
**Status**: Adopted. Background poller runs in telegram.py.

### D006 — 200-Character Tool Summary Truncation (2026-02-19, P2)
User wanted high observability. Bumped from 80 to 200 chars for all tool summaries.
**Status**: Adopted.

### D007 — Eval Two Lanes (2026-02-25, P3)
Deterministic (assertion-first, no LLM judge) and Judge (holistic behavioral evaluation). Three run profiles: smoke, feature, full.
**Status**: Partially adopted. Lane separation exists. Profile system exists. Full taxonomy migration incomplete.

### D008 — Fixture Isolation Per-Run (2026-02-25, P3)
Replace persistent mutable fixture vault with per-run ephemeral copies from template.
**Status**: [UNCERTAIN] — planned but implementation verification needed.

### D009 — Fork = JSONL Copy Not SDK fork_session (2026-03-01, S1)
SDK `fork_session=True` forks from HEAD only. JSONL copy allows forking from any point by parent-chain traversal.
**Status**: Adopted. `jsonl_fork.py` implements this. ForkRunner in `fork.py` uses SDK fork for memory extraction only.

### D010 — Flat Topics with Overflow (2026-03-01, S1)
One group chat = one agent tree. Depth > 1 overflows to new group chat. General topic = trunk agent.
**Status**: Partially adopted. Topic routing works. Overflow to new group chat NOT implemented.

### D011 — ResultMessage Caching for session_info (2026-03-01, S6)
Cache latest ResultMessage in HookState. session_info tool reads from cache.
**Status**: Adopted. `session_info` and `context_info` tools implemented.

### D012 — No Hot Code Modification (S6 D9)
Agent CAN monkey-patch hooks, define functions in namespace, import modules. CANNOT add new MCP tools mid-session or reload modules.
**Status**: Deferred. `run_code` tool not built yet.

### D013 — Telegram File Awareness via system-note (2026-02-28, S7)
All non-text messages normalized to `<system-note>` blocks with metadata. Voice messages auto-transcribed. Media groups aggregated.
**Status**: Fully adopted. `telegram_ingest.py`.

### D014 — ForkTask as Agent-Initiated Topic Creation (2026-03-03, S3, P4)
Agent calls ForkTask MCP tool → new topic created → child runs → parent gets callback. Replaced self_fork.
**Status**: Fully adopted. Later generalized to AgentTask with fork boolean parameter, supporting both fork and fresh-session modes.

### D015 — Text-Based Callback Payload (2026-03-03, P4)
Keep child completion callback text-based so it works with existing continuation system. No rich structured payloads yet.
**Status**: Adopted.

### D016 — Launcher Callback Pattern (2026-03-03, P4)
tools.py receives a launcher callback from Telegram runtime. If runtime can't provide launcher, tool returns "not available." Avoids pushing Telegram internals into SessionManager.
**Status**: Adopted. `hook_state.fork_task_launcher` is the callback slot.

### D017 — Session Tree as Tree Not DAG (2026-03-01, V1)
One UUID = one linear thread. Service forks at branch points. Prefix segmentation. Directory structure IS the tree.
**Status**: Designed only, NOT implemented. No fork markers, no service forks, no janitor.

### D018 — Pre-Compaction Memory Flush (V5, V6)
Before compaction, flush important memories to vault files. Then deny compaction so daemon restarts with fresh session.
**Status**: Adopted as D022 in hooks.py. `on_pre_compact()` extracts memories then returns `{"decision": "deny"}`.

### D019 — Schedule Mode: Interval + Cron (2026-03-10, S4, S5)
Two modes: interval (inactivity-based re-anchoring) and cron (wall-clock 5-field). interval_seconds=0 maps to on_topic_stop.
**Status**: Fully adopted.

### D020 — Schedule Hybrid Reliability (2026-03-10, S5)
Poller authoritative for due execution. Stop-hook re-anchors intervals. Start guard prevents concurrent fires. Per-route lock blocks duplicate launches.
**Status**: Fully adopted. `schedule_run_active` and `execution_active` flags in HookState.

### D021 — Inbox Tool for Team Communication (no formal spec)
File-based JSON inbox with async locks and notification callbacks. SendInboxMessage + ReadInbox tools.
**Status**: Adopted. Implementation exists in tools.py.

### D022 — Deny Compaction Strategy (V5, V6)
Instead of implementing custom compaction prompts, deny compaction entirely. Extract memories first, then deny. Agent restarts fresh.
**Status**: Adopted. `on_pre_compact()` in hooks.py.

---

## 4. Orphaned Specs (specs with no or unclear implementation path)

### Orphaned / Stale

1. **`run_code` MCP Tool (S6 Item 2)** — Full spike verification completed (exec/eval async pattern, persistent namespace, timeout, background execution). 4 spikes verified. All design decisions made. Implementation was scoped as "Large" work item. **No code exists.** Not blocked by anything technical — seems deprioritized in favor of other features.

2. **Background Result Delivery Fix (S6 Item 4, D12)** — User observed that background fork results are lost when the response stream closes. This affects both CLI daemon and Telegram. Spec identifies the fix: decouple wake-up loop from response stream or add persistent queue watchers. **[UNCERTAIN] Whether this was fixed in the AgentTask implementation** — the existing completion callback system may have solved this for Telegram, but the fundamental issue (ConversationRunner wake-up loop only runs within runner.run()) may still exist for CLI.

3. **Session Tree Proposal (V1)** — Comprehensive v3 proposal with 7 platform changes required. All 7 are unimplemented: fork markers, service fork spawning, session directory helper, janitor trigger, pre-compaction hook for fork points, custom compaction prompt, git commits at fork points. **Appears deprioritized** — the vault task for hierarchical session summaries is blocked by "vault primitives redesign."

4. **Session Summarize Skill (V2)** — Draft v0.1 that would replace session-offboard. Depends on session tree infrastructure. **Not implementable until V1 is done.**

5. **Orchestration Platform Tiers 2-4 (S1)** — The following remain unimplemented from the grand vision:
   - Tier 2: SQLite message persistence, background command topics, topic metadata/icons, /totopic, compaction summary storage
   - Tier 3: SDK backend abstraction, Codex CLI backend, multi-SDK workspace, delegated permissions, slash command parity, multi-user
   - Tier 4: Multi-bot load distribution, userbot automation, chat lifecycle cleanup
   These are explicitly future work and were never scheduled for near-term.

6. **DM Control Plane (S1)** — Bot DM as workspace management surface. Designing/creating workspaces from DM. **Not implemented, not on any task list.**

### Discrepancies Found

1. **Immutable Patterns Mismatch**: `config.py` has `IMMUTABLE_PATTERNS = ["Misc/Meeting Notes"]` but vault CLAUDE.md says `Sources/` is immutable. The vault was restructured (Meeting Notes moved from `Misc/Meeting Notes/` to `Sources/Meeting Notes/`), but the code pattern wasn't updated. The hooks.py `_make_immutable_check()` also checks `.env` files separately. **The pattern in code is stale.**

2. **architecture.md is Stale**: The `docs/architecture.md` file references `self_fork` as the MCP tool (replaced by AgentTask/ForkTask). It lists only one MCP tool. The file hasn't been updated since the major feature additions (topics, scheduling, file ingestion, inbox tools).

3. **run_mode vs reset_session**: Phase 1 scheduling spec uses `run_mode` ("continue" | "reset_session"). Phase 2 spec replaces it with `reset_session: bool`. The code uses `reset_session` with backward-compat mapping from `run_mode`. Both specs exist without explicit deprecation marking on Phase 1.

4. **Telegram-orchestration-platform.md Status**: Lists `self_fork` as the current tool and "one session per daemon" as current assumption. Both are outdated — multiple sessions per daemon are now supported, and self_fork was replaced. The spec was last updated 2026-03-01 and predates the topic and forktask implementations.

5. **Eval Overhaul Incomplete**: The plan (P3) specified 6 phases. Phases 1-2 appear partially done (timing improvements + taxonomy). Phase 3 (separate deterministic test files) was NOT done as specified — no `test_deterministic_cli.py` or `test_deterministic_telegram.py` exist. Phases 4-6 status unclear.

---

## Summary Statistics

- **Total specs/plans/proposals found**: 17 documents (7 codebase specs, 4 codebase plans, 3 vault proposals, 3 vault research docs)
- **Fully implemented**: 9 (S2, S3, S4, S5, S7, P1, P2, P4 + generalized beyond original scope)
- **Partially implemented**: 4 (S6, P3, V3, S1 Phase 0)
- **Not implemented**: 3 (V1, V2 + S1 Tiers 2-4 as a group)
- **Research only / informational**: 3 (V4, V5, V6)
- **Vault tasks tagged @obs-code**: 9 (0 done, 0 in-progress, 9 open)
- **Stale/outdated docs needing refresh**: 2 (architecture.md, config.py IMMUTABLE_PATTERNS)
