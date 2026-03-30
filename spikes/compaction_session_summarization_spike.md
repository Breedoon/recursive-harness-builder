# Compaction + Session Summarization Spike

**Date**: 2026-03-13
**SDK Version**: claude-agent-sdk v0.1.44 (upgraded from v0.1.35 since prior spikes)
**Context**: Designing hierarchical session summarization. Investigating compaction integration and whether we can reliably disable compaction.

## Critical Finding: `autoCompactEnabled` Is NOT a settings.json Property

The 2026-02-26 spike report (`compaction_spike_report.md`) claimed `settings.json` could disable compaction. **This was always wrong** — `autoCompactEnabled` is an app-state property that lives exclusively in `~/.claude.json`, not in project `settings.json`. The SDK silently ignores it when placed in project settings. This is by design, not a regression.

**Latest SDK version**: v0.1.50 (installed: v0.1.44). No version fixes this because it's not a bug — `autoCompactEnabled` was never a project-level setting.

**Fix**: `claude config set -g autoCompactEnabled false` (writes to `~/.claude.json`) + `setting_sources` must include `"user"` for the SDK to read it.

## Threshold Override Experiments (Run 2026-03-13)

### Can We Push the Threshold to 99% Instead of Disabling?

Tested `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` env var at multiple values (haiku, ~15K tokens/turn):

| Override Value | Compacted? | Pre-compact Tokens | Effect |
|---------------|-----------|-------------------|--------|
| 50 | YES | 101,482 | Earlier — works downward |
| 80 | YES | 146,521 | Earlier — works downward |
| 90 | YES | 176,547 | Same as default (ceiling) |
| 95 | YES | 176,547 | Same as default (ceiling) |
| 99 | YES | 176,558 | Same as default (ceiling) |
| Baseline | YES | 176,547 | Default behavior |
| Project `autoCompactThreshold=99` | YES | 172,750 | Ignored |

**Conclusion: Override only works DOWNWARD.** There's a hard ceiling at ~176K tokens (haiku). Values above ~85% have no effect — you cannot push the threshold past the SDK's built-in ceiling. Setting it to 99% does NOT effectively disable compaction.

**The ONLY way to disable compaction remains: `setting_sources` including `"user"` + `autoCompactEnabled: false` in `~/.claude.json`.**

## Disable Compaction Experiments (Run 2026-03-13)

### Test 1: Can We Disable Compaction?

Ran `compaction_spike_quick_verify.py` — four tests with haiku, ~30K chars/msg padding:

| Method | Compacted? | Max Tokens | Notes |
|--------|-----------|-----------|-------|
| `setting_sources=["project"]` + vault cwd | **YES** ❌ | 169,858 → 12,933 | **THIS IS PRODUCTION CONFIG** |
| `settings=<vault settings file path>` | **YES** ❌ | 176,550 → 4,606 | File path doesn't work |
| `settings=<temp file>` with `{autoCompactEnabled: false}` | **YES** ❌ | 176,550 → 4,512 | Temp file doesn't work either |
| Baseline (no disabling) | **YES** | 176,550 → 4,633 | Expected behavior |

**All four compacted at ~170K tokens.** The vault's `.claude/settings.json` with `autoCompactEnabled: false` is NOT being honored via `setting_sources=["project"]`.

### Test 2: Alternative Disable Methods

Ran `compaction_spike_env_var.py`:

| Method | Compacted? | Max Tokens | Notes |
|--------|-----------|-----------|-------|
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100` | **YES** ❌ | 176,550 | Env var doesn't work |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=999` | **YES** ❌ | 176,550 | Absurd value doesn't work |
| `setting_sources=["user"]` + global config | **NO** ✅ | 185,346 → "Prompt too long" | WORKS! |
| `setting_sources=["user","project"]` + vault cwd | **NO** ✅ | 193,646 → "Prompt too long" | WORKS! |

### Test 3: Confirming Why Project Fails

Ran `compaction_spike_project_debug.py`:

| Method | Compacted? | Max Tokens | Notes |
|--------|-----------|-----------|-------|
| Clean tmpdir + `["project"]` | **YES** ❌ | ~170K | Even isolated, project source doesn't work |
| Tmpdir + CLAUDE.md + `["project"]` | **YES** ❌ | ~170K | Adding CLAUDE.md doesn't help |
| Vault + `["user","project"]` + global config | **NO** ✅ | 193,643 | Confirmed: user source is what works |
| Direct `settings=<file path>` | **YES** ❌ | ~170K | File path confirmed broken |

## Definitive Conclusion

**On SDK v0.1.44, the ONLY way to disable compaction is via the `"user"` setting source** (`~/.claude.json`).

- `setting_sources=["project"]` → **does NOT disable compaction** (the `autoCompactEnabled` flag in project settings is ignored)
- `settings=<file path>` → **does NOT disable compaction**
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` → **does NOT disable compaction** (neither 100 nor 999)
- `setting_sources=["user"]` or `["user","project"]` with `~/.claude.json` containing `autoCompactEnabled: false` → **WORKS** ✅

## Impact on Production

**The OBS agent is currently compacting despite the settings file saying otherwise.** The production code at `session.py:117` uses `setting_sources=["project"]`, which does NOT honor `autoCompactEnabled: false`.

### Fix

Change `session.py:117` from:
```python
setting_sources=["project"],
```
to:
```python
setting_sources=["user", "project"],
```

AND set `autoCompactEnabled: false` in `~/.claude.json`.

**Side effects to consider**: The `"user"` source loads ALL user-level settings from `~/.claude.json`. This is a large file (~90KB) with many settings (tips, cached gates, dynamic configs). Most are UI-related and shouldn't affect SDK behavior, but this needs monitoring.

## PreCompact Hook Behavior (Confirmed Still Working)

The PreCompact hook still fires correctly with `HookMatcher` (not plain dicts). Key behaviors:
- Hook fires at ~170K tokens (haiku)
- `hook_input` contains `session_id`, `transcript_path`, `trigger` ("auto")
- Returning `{"decision": "block"}` is still silently ignored
- Returning `{"continue_": True}` proceeds with compaction
- Forking from inside the hook was confirmed in prior spikes (not re-tested — same mechanism)

## Revised Architecture for Session Summarization + Compaction

### Phase 1 (now): Schedule-based summarization
- 55-min interval schedule triggers fork → fork runs summarization skill
- **Bug fix needed**: Change `setting_sources` to `["user", "project"]` + set global config
- With this fix, compaction is truly disabled for all sessions
- Sessions that approach context limit will hit "Prompt too long" error instead of compacting
- The 55-min schedule ensures summarization happens before this

### Phase 2: Context threshold monitoring
- Monitor `ResultMessage.usage` after each turn
- When total context > threshold (e.g., 160K for haiku):
  - Fork the session for summarization (same skill as phase 1)
  - Reset the original session (fresh start)
  - Session card gets a segment file (1.md, 2.md, etc.)
- This prevents the "Prompt too long" error from phase 1

### Phase 3: Custom compaction
- After summarization fork writes the session card, extract key context
- Inject into fresh session as system prompt
- The agent continues with full knowledge of what was discussed
- No dependency on SDK compaction at all

## Compaction Segments for Long Sessions

```
KB/Sessions/
  2026-03-13 1430 Research.md          ← parent note
  2026-03-13 1430 Research/
    1.md                                ← first context window
    2.md                                ← after first threshold hit
    task-researcher.md                  ← child agent
```

`type: segment` vs `type: root`/`type: fork`/`type: subagent` in frontmatter.

## Spike Scripts

| Script | What It Tests | Result |
|--------|--------------|--------|
| `compaction_spike_quick_verify.py` | 4 disable methods (production mirror, file path, temp file, baseline) | ALL COMPACTED |
| `compaction_spike_env_var.py` | Env vars, user sources, user+project, extra_args | Only user source works |
| `compaction_spike_project_debug.py` | Clean tmpdir, CLAUDE.md, user+project, standalone file | Confirmed: project source broken |

## Prior Spike Scripts (2026-02-26, SDK v0.1.35)

Results from these are UNRELIABLE on current SDK version:
- `compaction_spike_report.md` — claimed settings file works (now disproven)
- `compaction_spike_disable.py` — tested 5 methods (results no longer valid)
- `compaction_spike_3_fork_at_compact.py` — fork from hook (mechanism likely still works)
- `compaction_spike_fast.py` — trigger compaction fast (still useful technique)

## Dead Code in Platform

- `hooks.py:on_pre_compact()` — defined but not registered in `create_hook_matchers()`
- `hooks.py:on_stop()` — same
- `fork.py:ForkRunner.extract_memory()` — stale paths (`.claude/memory/`, `.claude/topics/`)
