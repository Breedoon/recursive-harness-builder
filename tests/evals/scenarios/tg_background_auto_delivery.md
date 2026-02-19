# Telegram Background Auto Delivery

## Steps
1. SendNowait: "Use self_fork with background=true to read CLAUDE.md and summarize it in 2 bullets. Immediately reply with EXACTLY FORK_LAUNCHED before waiting for the fork result."
2. Sleep: 30

## Criteria
- The transcript contains FORK_LAUNCHED (proving immediate foreground response)
- The transcript later contains background-fork result content without any additional user message
- The transcript includes at least one concrete detail from CLAUDE.md in the delivered fork result
- The transcript includes the `(done)` sentinel
