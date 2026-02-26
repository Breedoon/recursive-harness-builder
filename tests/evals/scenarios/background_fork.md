---
lane: judge
profiles: feature, full
---
# Background Fork

## Intent
- Validate true background fork behavior: quick foreground response plus later result delivery.
- Broken behavior example: agent blocks waiting for the fork and never answers the immediate arithmetic request.
- Broken behavior example: agent claims fork launched but cannot later provide CLAUDE-grounded fork output.
- Suspicious behavior: generic third-turn summary without concrete CLAUDE details.

## Steps
1. Send: "Hello, just confirming you can hear me. Reply with a short greeting."
   Wait: 30
2. Send: "I need two things at once: (1) Use the self_fork tool with background=true — have the fork read CLAUDE.md and summarize what it contains, and (2) immediately tell me what 7 * 8 is without waiting for the fork. Do both in this single response."
   Wait: 30
3. Send: "Did the background fork finish? What did it find in CLAUDE.md?"
   Wait: 120

## Criteria
- The first response is a coherent greeting (warmup turn to establish the session)
- The second response includes the answer 56 (7 * 8), proving the agent answered immediately without blocking on the fork
- The second response mentions that a background fork was launched (e.g. "background fork" or "launched")
- The third response references actual content from CLAUDE.md that the fork found (not a generic guess), proving the fork result was delivered back to the agent
- The agent does not report errors about background forks failing
