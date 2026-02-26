---
lane: deterministic
profiles: feature, full
continuation_timeouts: 60,20
---
# Telegram Background Auto Delivery

## Intent
- Validate true background behavior: launch quickly, continue without blocking, and auto-deliver results.
- Flag any sign that delivery required manual prompting or that fork launch semantics were broken.

## Steps
1. Send: "Hello. Reply with exactly READY."
   Wait: 30
2. SendNowait: "Use self_fork with background=true to read CLAUDE.md and summarize it in 2 bullets. Immediately reply with EXACTLY FORK_LAUNCHED before waiting for the fork result."
3. Sleep: 30

## Criteria
- The warm-up response contains READY
- The transcript contains FORK_LAUNCHED (proving immediate foreground response)
- The transcript later contains background-fork result content without any additional user message
- The transcript includes at least one concrete detail from CLAUDE.md in the delivered fork result
- The transcript includes the `(done)` sentinel
