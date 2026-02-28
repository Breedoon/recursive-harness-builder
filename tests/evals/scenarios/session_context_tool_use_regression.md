---
lane: deterministic
profiles: feature, full
---
# Session Context Tool-Use Regression

## Intent
Reproduce the observed failure mode where context usage spikes after tool-heavy
work and then drops unexpectedly on the next plain-text turn.

## Steps
1. Send: "Reply exactly READY."
   Wait: 60
2. Send: "Use your introspection tools and report session id + context used/window with raw token fields."
   Wait: 120
3. Send: "Read three small files (.claude/skills.md, Vault/Sessions.md, Misc/Drafts.md) and reply exactly DONE_READS."
   Wait: 150
4. Send: "Now run your introspection tools again and report session id + context used/window with raw token fields."
   Wait: 150
5. Send: "What is 2 + 2? one short line."
   Wait: 60
6. Send: "Run introspection tools one more time and report session id + context used/window with raw token fields."
   Wait: 150

## Criteria
- First, third, and fifth responses each include valid context snapshots with non-zero usage and no tool errors.
- Reported used tokens do not exceed the reported context window in any snapshot.
- The final snapshot does not show a sharp unexplained drop versus the post-tool snapshot (regression guard).
