---
lane: judge
profiles: feature, full
exploration_messages: 4
---
# Context Usage Stress (Judge)

## Intent
- Primary goal: try to break or falsify the `context_info` / session context reporting behavior.
- Treat this as adversarial validation, not a box-check exercise.
- Broken behavior example: context used appears to decrease sharply after additional work without session reset/compaction.
- Broken behavior example: used tokens exceed reported window, or contradictory values are reported across adjacent checks.
- Broken behavior example: tool reports values that look like cumulative billing while presenting them as current window occupancy.
- Suspicious behavior: large jumps after tiny file reads, unstable turn counters, or unclear distinction between billing totals and active window occupancy.
- Judge should probe for edge cases using the optional exploration budget if anomalies appear.

## Steps
1. Send: "Use your introspection tools and report your current context usage with raw numbers (session id, used, window, remaining, and any token fields you rely on)."
   Wait: 120
2. Send: "Read these small files, then summarize each in one short bullet: .claude/skills.md, Vault/Sessions.md, Misc/Drafts.md"
   Wait: 150
3. Send: "Now check context again using introspection tools and compare with the first snapshot. Explicitly call out whether the change looks reasonable."
   Wait: 150
4. Send: "Do a slightly tool-heavier check: grep for 'Session' in Vault/Sessions.md and read .claude/memory.md, then check context again and compare."
   Wait: 180
5. Send: "Final assessment: is your context tool likely reporting true active window occupancy or some other metric? Explain with concrete evidence from this run."
   Wait: 180

## Criteria
- The interaction includes at least three explicit context snapshots with concrete numbers.
- The agent provides a reasoned comparison between snapshots (not just raw dumps).
- Any impossible or suspicious patterns (e.g., used > window, sharp unexplained drops, contradictory counters) are explicitly flagged.
- The final assessment clearly distinguishes between likely window occupancy vs billing/cumulative interpretations.
