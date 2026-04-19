# Cache Control Breakpoint Spike Report

**Date**: 2026-04-19
**Model tested**: claude-haiku-4-5
**Script**: `spikes/cache_control_breakpoint_spike.py`

## Questions Investigated

1. Is `cache_control` metadata part of the cache prefix hash?
2. Does changing the number of `cache_control` blocks affect cache hits?
3. Does changing the position of `cache_control` blocks affect prefix matching?
4. Can request B read cache entries created by request A's breakpoints?
5. How do forks (truncated prefix) interact with breakpoints?
6. What happens when CC removes cache_control from previous messages on new turns?
7. What is the hard limit on cache_control blocks?

## Key Findings

### Finding 1: cache_control is NOT part of the cache prefix hash

Adding or removing `cache_control` from a specific block does not change the cache key. The cache key is based on content only.

| Request | cache_read | cache_creation | fresh |
|---------|-----------|---------------|-------|
| WARM (sys CC + user CC) | 0 | 11,033 | 3 |
| CTRL (identical) | 11,033 | 0 | 3 |
| NO CC on system | 11,033 | 0 | 3 |
| NO CC on user1 | 11,033 | 0 | 3 |
| NO CC anywhere | 0 | 0 | 11,036 |

**Interpretation**: Removing CC from system or user1 individually still hits cache (the remaining CC marker triggers the cache system). But removing ALL CC markers means no caching occurs at all.

### Finding 2: cache_control is a read/write CHECKPOINT, not a hash component

`cache_control` tells the API: "Create a cache entry at this prefix position (write) and check for existing cache at this prefix position (read)."

Without any `cache_control` markers in a request, the API does not engage the caching system — no reads, no writes, no nothing.

### Finding 3: More breakpoints = more granular cache reads

| Request | Breakpoints | cache_read | Rate |
|---------|------------|-----------|------|
| 3 BP (sys+u1+u2) | 3 | 16,537 | 99.9% |
| 1 BP (sys only) | 1 | 5,508 | 33.3% |
| 2 BP (sys+u2) | 2 | 16,537 | 99.9% |
| 4 BP (sys+u1+u2+u3) | 4 | 16,537 | 99.9% |

With only 1 breakpoint on system, the API reads only the system-level cache (5,508 tokens). Adding a breakpoint at user2 allows reading the entire prefix up to user2 from cache. The breakpoint tells the API "look for cache here" — without it, the API doesn't check.

### Finding 4: Cached entries are globally accessible

Request B can read cache entries that request A created, even if B has different (or fewer) breakpoints. The cache is content-keyed, not request-keyed.

| Request | Breakpoints | cache_read |
|---------|------------|-----------|
| A: 3 breakpoints | sys + user1 + last | 11,019 read + 15 create |
| B: 1 breakpoint | last only | 11,034 read (full hit!) |

### Finding 5: Forks get full cache hits for shared prefix

| Request | Content | cache_read |
|---------|---------|-----------|
| PARENT (4 user msgs) | Full conversation | 16,537 + 5,533 create |
| FORK-A (match CC) | 2 user msgs + new | 16,537 read |
| FORK-A (proxy CC) | 2 user msgs + new | 16,550 read |
| FORK-B (from turn 1) | 1 user msg + new | 11,019 read |

Forks don't need matching CC positions. Any CC marker in the fork triggers reading from cached entries. Fork-B (shorter prefix) reads less cache, as expected.

### Finding 6: Keeping CC on old messages provides SIGNIFICANTLY more cache

**This is the most important finding for the proxy design.**

| Turn 2 style | cache_read | Rate |
|-------------|-----------|------|
| CC-style (user1 loses CC) | 11,019 | 66.6% |
| Proxy-style (user1 keeps CC) | 16,536 | 100% |

When CC removes `cache_control` from user1 on turn 2, the API can only read the system-level cache (11,019 tokens). When the proxy KEEPS CC on user1, the API reads the full prefix including user1 (16,536 tokens) — 50% more cache read.

CC is not in the hash (the content matches either way), but without a CC marker at user1, the API has no instruction to check for cached content there. The marker is the trigger for the cache lookup.

### Finding 7: Hard limit is 4 cache_control blocks

- 3 blocks: works
- 4 blocks: works (maximum)
- 5 blocks: `400 {"type":"error","error":{"type":"invalid_request_error","message":"A maximum of 4 blocks with cache_control may be provided. Found 5."}}`
- Distribution (sys vs user) doesn't matter — only total count

## Implications for the Proxy

### The Bug

The proxy assumes 1 system CC block and adds 3 on user messages (total 4). But CC may also place `cache_control` on system blocks. The proxy doesn't strip system CC, so the total can reach 5 → API error.

### The Fix

The proxy must control ALL cache_control placement:

1. **Strip ALL `cache_control`** from the entire request — system blocks, user messages, everywhere
2. **Add exactly 4** strategically:
   - 1 on the last system block (stable, large)
   - 3 on the last 3 user messages (recent conversation)

### Optimal Distribution Strategy

Given the 4-block limit, where should breakpoints go?

**For continuation turns** (same session, next turn):
- Last user message matters most (the API reads the longest matching prefix)
- Earlier breakpoints help read intermediate segments

**For forks** (truncated prefix + new message):
- The fork's breakpoints trigger reading from the parent's cache
- Any breakpoint at or beyond the shared prefix reads it
- More breakpoints at different depths give more granular fork coverage

**Current strategy (last 3 user + system) is optimal for most cases:**
- System breakpoint covers the large, stable system prompt
- Last 3 user messages cover the recent conversation tail
- Forks from the end get full cache hits
- Forks from earlier points get system cache + whatever their breakpoints cover

**When it's suboptimal:** Very long conversations (50+ turns) where forks from the middle would benefit from evenly-distributed breakpoints. But in practice, OBS forks are usually from the end or near-end of conversations.

### Key Design Principle

**`cache_control` is not a cache KEY — it's a cache INSTRUCTION.** It says "look here for cached content" and "store content here for future lookups." The content prefix is the key. This means:

1. We don't need to exactly match parent/fork CC positions
2. We DO need at least one CC marker for caching to work at all
3. More markers = more opportunities for the API to find cached content
4. Removing a marker from a position doesn't invalidate that cache entry — it just means future requests won't READ from it unless they have their own marker there (or at a later position that covers it)

## Minimum Cacheable Thresholds (from Anthropic docs)

| Model | Minimum Tokens |
|-------|---------------|
| Opus 4.5/4.6/4.7, Haiku 4.5, Mythos | **4096** |
| Sonnet 4.6 | **2048** |
| Sonnet 4.5, Opus 4/4.1, Sonnet 4/3.7 | **1024** |
| Haiku 3.5, Haiku 3 | **2048** |

Below threshold: no caching at all, no error returned. Our first spike run (2,676 tokens) hit this — Haiku 4.5 needs 4096 minimum.

## 20-Block Lookback Window (from Anthropic docs — CRITICAL for long conversations)

The docs reveal a lookback mechanism:

> "On each request the system computes the prefix hash at your breakpoint and checks for a matching cache entry. If none exists, it walks backward one block at a time, checking whether the prefix hash at each earlier position matches something already in the cache. The system checks at most **20 positions** per breakpoint."

This means:
- **Writes only happen at breakpoints.** No intermediate positions get cached.
- **Reads search backward up to 20 blocks** from each breakpoint for previously written entries.
- **The lookback checks hashes** at each position — it finds entries that prior requests wrote, NOT stable content.

### Implications for long conversations

Each user+assistant turn pair adds ~2 content blocks. A 20-turn conversation has ~40+ blocks.

**Continuation turns work by chaining**: Each turn's breakpoint is ~2 blocks after the previous turn's breakpoint. The lookback easily finds it. Cache entries form a chain that propagates forward.

**Forks need breakpoint relay stations**: If a fork goes back to turn 5 (~position 13) and the parent's latest breakpoints are at positions 37-43, the fork's breakpoints can't reach the parent's latest entries. But:
- The system breakpoint (position 3) is always refreshed — forks always get system cache
- Intermediate entries from early turns may have expired (5-min TTL)
- The fork's own breakpoints create new entries for future turns

**When the proxy's "last 3 user messages" breaks down**: In conversations > 30 turns, early entries expire and are more than 20 blocks from any current breakpoint. This only matters for forks — continuation turns chain naturally.

### Write-only-at-breakpoints gotcha

> "Common mistake: Breakpoint on content that changes every request. The lookback does not find stable content behind your breakpoint and cache it. It finds entries that prior requests already wrote, and writes happen only at breakpoints."

This means if you only ever place breakpoints at the END of the conversation, the lookback cannot "discover" that the middle of the conversation matches a cached entry — because no one ever wrote a cache entry at the middle position.

### Recommendation

For the proxy's 4 breakpoints:
- 1 on system prompt (stable, always refreshed)
- 3 on last 3 user messages (chains well for continuation turns)

This is optimal for >95% of real usage. The edge case (forks from middle of 30+ turn conversations) would benefit from spreading breakpoints, but that would sacrifice continuation-turn efficiency for a rare case. Also, TTL expiration of old entries limits the benefit anyway.

## Cache Key Content (from Anthropic docs)

**Included in cache hash**: Full prefix content — tools → system → messages (in that order) up to and including the marked block. The hash is cumulative.

**NOT included**: `cache_control` marker itself, `tool_choice`, output tokens.

**Invalidation cascade**: Changes to tools invalidate tools + system + messages. Changes to system invalidate system + messages. Changes to earlier messages invalidate all later messages.

## TTL Details

- Default: 5 minutes (refreshed each time cached content is read — free)
- Extended: 1 hour (2x write cost, requires `"ttl": "1h"`)
- Mixing: 1-hour entries must appear before 5-minute entries in the request
- Entries are available after the first response begins (not immediately)

## References

- Anthropic docs: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Spike script: `spikes/cache_control_breakpoint_spike.py`
- Proxy spec: `~/Documents/obs/docs/specs/cache-normalizing-proxy.md`
