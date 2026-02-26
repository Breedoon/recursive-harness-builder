---
lane: judge
profiles: feature, full
---
# Telegram Stress Chronology

## Intent
- Stress mixed workload + follow-ups and verify nothing is lost.
- Broken behavior example: one or more user intents silently dropped or answered out of order.
- Flag subtle anomalies (late/misdirected replies, partial intent coverage, suspicious extra sentinels) even if baseline criteria pass.

## Steps
1. SendNowait: "Read CLAUDE.md and list 5 major sections with one-line explanations."
2. Sleep: 4
3. SendNowait: "how is it going?"
4. Sleep: 4
5. SendNowait: "ping - reply that you saw this message"
6. Sleep: 120

## Criteria
- The output includes a substantive response to the CLAUDE.md section-list task
- The output also acknowledges the "how is it going" and ping follow-up messages
- No message appears dropped; all three user intents are addressed in output
- The output includes the `(done)` sentinel
- The output does not contain timeout or internal error text
