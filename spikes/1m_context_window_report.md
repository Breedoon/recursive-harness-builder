# 1M Context Window — Spike Report

**Date:** 2026-03-16
**SDK version:** 0.1.44 (Claude Code v2.1.59)
**Models tested:** opus[1m], haiku, sonnet[1m] (rate limited)

## Summary

The 1M context window is GA for Opus 4.6 and Sonnet 4.6 (March 13, 2026). It requires the `[1m]` model suffix in Claude Code / Agent SDK — the API supports 1M natively but the Claude Code subprocess uses `[1m]` to set its internal compaction threshold. Without `[1m]`, compaction fires at ~167K (200K window). With `[1m]`, compaction fires at ~920K (1M window).

**Per-session compaction control is possible** via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` passed in the `env` dict of `ClaudeAgentOptions`. Setting `PCT=99` pushes the threshold to ~990K — effectively the entire window.

## Changes Made to OBS

1. `src/obs_agent/session.py:116` — added `model="opus[1m]"` to ClaudeAgentOptions
2. `src/obs_agent/config.py:48` — changed `context_window_estimate_tokens` from 200,000 to 1,000,000

## Empirical Results

### Organic build-up (opus[1m], 80 turns)

| Turn | Tokens | Compacted? |
|------|--------|-----------|
| 1 | 9,807 | No |
| 34 | 204,573 | No (past old 200K boundary) |
| 68 | 405,241 | No |
| 80 | 476,065 | No |

Session: `d79e308b-9c64-4614-9a09-1bbe4557fdc1`

### Inflated session tests (single API call each)

| Session | Actual Tokens | Compacted? |
|---------|-------------|-----------|
| Original 80 turns | 476,065 | No |
| Inflated 1.38x | 759,553 | No |
| **Inflated 1.53x** | **875,218** | **No** |
| Inflated 1.69x | — | "Prompt is too long" (~960K + system prompt > 1M) |

### Compaction control experiments (forked from 875K session)

| Method | Single turn | Multi-turn (3) | Effect |
|--------|-----------|----------------|--------|
| Baseline (no override) | OK 875K | OK 875K | Default threshold ~920K |
| `PCT=50` | OK 875K | **Compacted at 875K on turn 2** | Works — lowers threshold |
| `PCT=95` | OK 875K | (not tested multi) | Works — raises threshold |
| `PCT=99` | OK 875K | OK 875K | **Works — threshold ~990K** |
| `autoCompactEnabled=false` (settings file) | OK 875K | (not tested) | Broken (bug #18264) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW=999999` | OK 875K | (not tested) | No observable effect |
| `WINDOW=999999+PCT=99` | OK 875K | (not tested) | No additional effect |

**Key finding:** Compaction is NOT retroactive. It only fires after the turn that pushes context past the threshold. Loading a session already above threshold doesn't trigger it — the next turn does.

## Per-Session Compaction Control

To disable compaction for a specific agent session:

```python
ClaudeAgentOptions(
    model="opus[1m]",
    env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "99"},  # threshold at ~990K
    # ... other options
)
```

This is fully per-session. Each SDK session gets its own `env` dict — no global flags, no settings files, no cross-session leakage.

### Use cases

- **Session summarization / offboarding**: Set `PCT=99` to prevent compaction, allowing the agent to read the full conversation before summarizing
- **Deep research sessions**: Keep full context for cross-referencing
- **Default sessions**: Leave at default (~92%) for normal cost-efficient operation

## What Doesn't Work

| Method | Status | Notes |
|--------|--------|-------|
| `autoCompactEnabled: false` in settings file | **Broken** | Bug #18264 — ignored by SDK |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` env var | **No effect** | Not recognized by current SDK version |
| `sonnet[1m]` model | **Persistent rate limit** | Separate, stricter rate limit pool from `sonnet`. Lasted 10+ hours. |
| Synthetic JSONL (appending turns) | **Ignored** | SDK tracks head pointer separately; appended messages not loaded |
| Synthetic JSONL (fattening existing messages) | **Works** | SDK loads all original messages with inflated content |

## Rate Limit Notes

- `opus[1m]` and `opus` share the same rate limit pool (both worked throughout testing)
- `sonnet[1m]` has a **separate, much stricter** rate limit pool from `sonnet`. Burning through it takes 10+ hours to recover. Avoid heavy sonnet[1m] usage.
- `haiku` is unaffected (no `[1m]` variant — 200K only)

## Architecture Notes

The `[1m]` suffix is a **client-side mechanism** in the Claude Code binary:
1. Claude Code strips `[1m]` before sending the model ID to the Anthropic API
2. Sets internal compaction threshold to ~92% of 1M instead of ~84% of 200K
3. The API natively supports 1M for Opus 4.6 / Sonnet 4.6 — no beta header needed (GA March 13)
4. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` adjusts the percentage (default ~92%)

The CLI auto-applies `[1m]` for Max/Team/Enterprise plans (v2.1.75+). The Agent SDK does not auto-apply — must be explicit.

## Reusable Sessions

These sessions are in `~/.claude/projects/-Users-breedoon-Documents-obs/`:

| Session ID | Tokens | Description |
|-----------|--------|-------------|
| `d79e308b-...` | 476K | Organic 80-turn opus[1m] build-up |
| `9920f3ba-...` | 760K | Inflated (1.38x padding) |
| `bbb490ea-...` | 875K | Inflated (1.53x padding) — best for threshold testing |

## Recommendations

1. **Use `model="opus[1m]"` in session.py** — already done
2. **For session summarization**: pass `env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "99"}` to give the summarizing agent near-full context
3. **Don't bother with** `autoCompactEnabled`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, or `sonnet[1m]`
4. **Monitor SDK updates** — bug #18264 (autoCompactEnabled broken) and #34435 (auto-upgrade to [1m]) may get fixed, simplifying configuration
