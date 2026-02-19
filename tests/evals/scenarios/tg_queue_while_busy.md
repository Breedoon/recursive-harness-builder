# Telegram Queue While Busy

## Steps
1. SendNowait: "List all the skills available to you. Be thorough and describe each one in detail."
2. Sleep: 4
3. SendNowait: "Also, what is 2 + 2? Reply with only the number."
4. Sleep: 120

## Criteria
- The output contains a substantive response about available skills
- The output also contains the number `4` for the queued follow-up question
- Both intents are addressed in the bot output (not just one)
- The output includes the `(done)` sentinel
