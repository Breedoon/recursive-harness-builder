---
lane: deterministic
profiles: feature, full
continuation_timeouts: 60,20
---
# Telegram Queue While Busy

## Intent
- Verify the agent injects a second message into the active run instead of silently deferring it as a separate fresh turn after completion.
- Broken behavior: the user sees two independent completion notifications, with the simple follow-up handled only after the long first task has already fully finished.
- Flag ordering anomalies, swallowed follow-ups, or missing queue-delivery visibility even if both topics eventually appear.

## Steps
1. SendNowait: "List all the skills available to you. Be thorough and describe each one in detail."
2. Sleep: 4
3. SendNowait: "Also, what is 2 + 2? Reply with only the number."
4. Sleep: 120

## Criteria
- The output contains a substantive response about available skills
- The output also contains the number `4` for the queued follow-up question
- Both intents are addressed in a single collected bot run, not split into two completed turns
- The output includes visible evidence that the busy-time follow-up was queued/delivered during the active run
- The output ends with the final completion summary (`context: ... / ...`)
