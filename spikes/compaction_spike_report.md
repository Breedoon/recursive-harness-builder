# Compaction Spike Report

**Date**: 2026-02-26
**SDK Version**: claude-agent-sdk v0.1.35
**Model tested**: haiku

## Executive Summary

The Claude Agent SDK's compaction mechanism is **not deeply customizable** via the Python SDK. The `PreCompact` hook fires and allows arbitrary code execution (file I/O, forking), but **cannot actually block or customize compaction**. The hook's `block` decision is silently ignored. There is no way to supply a custom compaction prompt or change the compaction threshold via the SDK.

However, the hook provides a **valuable interception point** for knowledge extraction: you can fork the session, read the transcript, and write files — all during the hook callback, before the compacted conversation continues.

## Findings

### 1. PreCompact Hook: Does Fire, But Can't Block

**Input fields received:**
```python
{
    "session_id": "...",
    "transcript_path": "/Users/.../.claude/projects/.../session.jsonl",
    "cwd": "/Users/.../obs",
    "hook_event_name": "PreCompact",
    "trigger": "auto",          # "auto" or "manual"
    "custom_instructions": None  # Always None for auto-compact
}
```

**Blocking doesn't work**: Returning `{"decision": "block", "reason": "..."}` is silently ignored — compaction proceeds anyway. The hook fired once, the block was returned, yet `compact_boundary` SystemMessage appeared with `pre_tokens: 168423`. The SDK/CLI simply doesn't honor block decisions for PreCompact.

**Reason**: `PreCompactHookSpecificOutput` doesn't exist in the SDK types (unlike `PreToolUseHookSpecificOutput` which has `permissionDecision`). The generic `SyncHookJSONOutput` fields (`decision`, `reason`, `continue_`) are accepted by the hook callback mechanism but have no effect on compaction.

### 2. Custom Compaction Prompt: Not Possible

- `custom_instructions` field exists in `PreCompactHookInput` but is always `None` for auto-compact
- There is **no SDK parameter** to set a custom compaction prompt
- There is **no hook output field** to inject instructions into compaction
- The compaction prompt is internal to the Claude Code CLI and not exposed
- `ClaudeAgentOptions` has no compaction-related fields

### 3. Compaction Threshold: Has a Hard Ceiling (~167K tokens)

**`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` only works downward, not upward.**

| Setting | Compaction Turn | pre_tokens | Effect |
|---------|----------------|-----------|--------|
| default (~84%) | 33 | 167,595 | baseline |
| 50% | 18 | ~90,000 | earlier compaction (works!) |
| 95% | 33 | 167,589 | **same as default** |
| 99% | 33 | 167,557 | **same as default** |

There is a hard ceiling at ~167K tokens (haiku 200K context). Setting the threshold above
the CLI's internal maximum has no effect — it always compacts at the same point.
**You cannot push compaction later than default or disable it via this env var.**

### 3b. Disabling Compaction: THREE Methods Work!

Tested 5 approaches. **3 successfully disable compaction**, 1 fails, 1 works but for the
wrong reason:

| Method | Compacted? | Max Tokens | Notes |
|--------|-----------|-----------|-------|
| **A: `settings` JSON string** | **YES** (failed) | 168,419→4,481 | `settings='{"autoCompactEnabled":false}'` does NOT work |
| **B: `setting_sources=["user"]`** + global config | **NO** | 189,495 | Works! Needs `~/.claude.json` to have the flag |
| **C: `setting_sources=["user","project"]`** | **NO** | 194,454 | Works! Combines both sources |
| **D: `settings` file path** | **NO** | 188,217 | Works! File with `{"autoCompactEnabled":false}` |
| **E: JSON string + user sources** | **NO** | 189,495 | Works (but user source is doing the work) |

**Key insight**: The `--settings` CLI flag with an **inline JSON string** (method A) does NOT
disable compaction. But a **file path** to a JSON file (method D) DOES. And loading
`setting_sources=["user"]` with `autoCompactEnabled: false` in `~/.claude.json` (method B) also works.

**Context went to 194K tokens** (method C) with no compaction — well past the 167K hard
ceiling that existed with compaction enabled. This proves compaction is truly disabled,
not just delayed.

**For the OBS agent** the cleanest approaches are:
- **Method D**: Create a settings file with `{"autoCompactEnabled": false}` and pass its path
- **Method B/C**: Add `"user"` to `setting_sources` and set the flag globally (but affects all sessions)

Method D is per-session and doesn't require touching global config.

### 3c. Token Usage from ResultMessage: Precise and Reliable

`ResultMessage.usage` provides exact token counts every turn:
```python
{
    "input_tokens": 3,                    # Non-cached input
    "cache_read_input_tokens": 97816,     # Cached portion
    "cache_creation_input_tokens": 4974,  # New cache entries
    "output_tokens": 5                    # Model output
}
# Total context = input + cache_read + cache_creation
```

This means **custom threshold detection is trivial**: after each turn, check
`total_context > YOUR_THRESHOLD` and act accordingly. No need for approximations.

### 4. File I/O Inside Hook: Works Perfectly

Both reading and writing files work inside the PreCompact hook:
- **Read transcript**: Full JSONL transcript (985K chars) accessible at `transcript_path`
- **Write files**: Arbitrary file writes work (tested writing summary files)
- **Timing**: Hook runs synchronously before compaction proceeds

### 5. Forking From Inside Hook: Works!

The most powerful finding. You can fork the session from inside the PreCompact hook:
```python
async def on_pre_compact(hook_input, tool_use_id, context):
    # Fork the about-to-be-compacted session
    fork_opts = ClaudeAgentOptions(
        model="haiku",
        resume=hook_input['session_id'],
        fork_session=True,
        max_turns=1,
    )
    async with ClaudeSDKClient(fork_opts) as fork:
        await fork.query("Summarize the conversation")
        # ... collect response, write to file

    return {"continue_": True}  # Can't block anyway
```

The fork has access to the full pre-compaction conversation and can extract/summarize knowledge.

### 6. Post-Compaction Behavior

After compaction:
- Session continues with the same `session_id`
- Cost drops dramatically (e.g., $0.41 → increments of $0.007 per turn)
- Model loses most context (only has compacted summary)
- `compact_boundary` SystemMessage provides `pre_tokens` count

### 7. SystemMessage Flow During Compaction

```
1. PreCompact hook fires (if registered with HookMatcher)
2. SystemMessage(subtype="status", status="compacting")
3. [compaction happens internally]
4. SystemMessage(subtype="status", status=null)
5. SystemMessage(subtype="compact_boundary", compact_metadata={trigger, pre_tokens})
6. Normal response to the user's prompt
```

### 8. Critical Bug Found: HookMatcher vs Dict

**Plain dicts don't work for hook registration.** The SDK's `_convert_hooks_to_internal_format` uses `hasattr(matcher, "matcher")` which returns `False` for dicts but `True` for `HookMatcher` dataclass instances. This is why the OBS Agent's existing `on_pre_compact` function (defined but not registered) would also need to use `HookMatcher`.

```python
# WRONG — hook silently never fires:
hooks={"PreCompact": [{"matcher": None, "hooks": [callback]}]}

# CORRECT — hook fires:
hooks={"PreCompact": [HookMatcher(matcher=None, hooks=[callback])]}
```

## Comparison: SDK Compaction vs Manual Summary + New Session

| Aspect | SDK Compaction | Manual (fork + new session) |
|--------|---------------|---------------------------|
| **Custom prompt** | No control | Full control over summarization prompt |
| **What's preserved** | SDK decides (opaque) | You decide exactly what to extract |
| **File I/O** | Only in PreCompact hook | Full control |
| **Knowledge extraction** | None by default | Fork can summarize, write files, update vault |
| **Threshold control** | None | You decide when to "compact" |
| **Session continuity** | Same session_id | New session (or resume with summary in system prompt) |
| **Cost** | One compaction call | Fork cost + new session cost |

## Recommended Architecture for OBS Agent

The best approach combines both:

1. **Register PreCompact hook** (with proper `HookMatcher`)
2. **In the hook**: Fork the session and extract knowledge to vault files
3. **Allow compaction** (can't block anyway, but `continue_: True` is the right signal)
4. **Post-compaction**: The compacted context preserves basic continuity; extracted knowledge persists in vault files for future sessions

Alternatively, for maximum control:
1. **Monitor `compact_boundary` SystemMessages** in the response stream
2. When detected, know that compaction just happened
3. On the next turn, inject extracted knowledge as additional context
4. This avoids relying on the hook at all

## Architecture Options (Analysis)

### Option A: PreCompact Hook + Fork (Single-Turn Only)

```
Normal operation → context fills → PreCompact fires →
  fork session (full context) → extract knowledge → write to vault →
  allow compaction → session continues with SDK summary + vault knowledge
```

**Pros**: Simple, piggybacking on existing mechanism, session continuity preserved
**Cons**: Can't control compaction prompt. Fork inside hook is limited to `max_turns`
you set — for multi-turn extraction (read files, write files, git commit) this is
tight because the hook blocks compaction while running.

### Option B: Disable Compaction + Custom Threshold in Runner Loop (RECOMMENDED)

```
1. Disable compaction: settings file with {"autoCompactEnabled": false}
2. After each turn, check ResultMessage.usage:
   total = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
   if total > CUSTOM_THRESHOLD (e.g., 160K):
     a. Note session_id
     b. Start NEW multi-turn extraction session (resume + fork_session=True)
        - Full tool access, reads files, writes vault, commits git
        - No turn limit — as many turns as needed
     c. Start FRESH session with extracted knowledge in system prompt
```

**Pros**: Full control. Multi-turn extraction with tools. No fork-inside-hook limitation.
With compaction truly disabled (tested to 194K tokens!), you own the entire lifecycle.
Usage data is precise and available every turn.
**Cons**: Lose session_id continuity (new session). Must implement the monitoring loop.
Must handle the hard overflow (prompt too long) yourself — no safety net.

**Implementation**: In session.py, pass a settings file:
```python
settings_file = config.vault_path / ".claude" / "settings.json"
# settings.json contains: {"autoCompactEnabled": false}
options = ClaudeAgentOptions(
    settings=str(settings_file),
    ...
)
```
In runner.py, check usage after each turn:
```python
if isinstance(msg, ResultMessage) and msg.usage:
    total = (msg.usage.get("input_tokens", 0)
           + msg.usage.get("cache_read_input_tokens", 0)
           + msg.usage.get("cache_creation_input_tokens", 0))
    if total > CUSTOM_THRESHOLD:
        yield CompactionNeededEvent(session_id, total)
```

### Option C: Low Threshold + Hook (Hybrid)

```
Set CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50 → compaction fires earlier →
  PreCompact hook → fork → extract → allow compaction →
  more frequent but lighter compactions
```

**Pros**: More frequent knowledge extraction, less context lost per cycle
**Cons**: More fork overhead, more compaction API calls. Fork still single-turn.

### Option D: Detect compact_boundary in Response Stream

```
Normal operation → compaction happens → detect compact_boundary SystemMessage →
  on next turn, read vault files for context → inject as additional context
```

**Pros**: No hook needed, reactive instead of proactive
**Cons**: Knowledge extraction happens AFTER compaction (can't fork pre-compact state)

## Spike Files

| File | What It Tests |
|------|--------------|
| `compaction_spike_1_hook_observe.py` | Basic hook observation (original API issue) |
| `compaction_spike_fast.py` | Large messages (found overflow error) |
| `compaction_spike_fast2.py` | Moderate messages (first compaction trigger, hook not firing — dict bug) |
| `compaction_spike_hook_debug.py` | Debugging hook registration (confirmed dict vs HookMatcher) |
| `compaction_spike_final.py` | **Working hook** — proper HookMatcher, file I/O, transcript access |
| `compaction_spike_block.py` | **Block test + fork** — proved block is ignored, fork works |
| `compaction_spike_no_compact.py` | **Disable test** — settings JSON, fork extraction, threshold override |
| `compaction_spike_custom_threshold.py` | **Threshold ceiling test** — 95%, 99% (same as default!), custom token monitoring |
| `compaction_spike_disable.py` | **Disable compaction** — 5 methods tested, 3 work (settings file, user sources, combo) |

## Key Sessions (Reusable)

These sessions are already near compaction threshold (~167K tokens) and can be forked for further testing:

| Session ID | Pre-tokens | Notes |
|-----------|-----------|-------|
| `cec20d87-81f4-4ada-afcd-567f61b98091` | 167,595 | Base session from fast2 spike (50 turns of padding) |

## Cost Summary

All spikes combined: approximately $4.50 (haiku pricing)
