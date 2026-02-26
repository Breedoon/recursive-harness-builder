---
lane: deterministic
profiles: smoke, feature, full
---
# Session Continuity

## Steps
1. Send: "Remember this code word for me: PINEAPPLE"
   Wait: 60
2. Send: "What was the code word I just told you?"
   Wait: 60

## Criteria
- The second response contains the word PINEAPPLE
- The agent demonstrates memory of the first message in the second response
