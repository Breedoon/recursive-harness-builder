---
lane: deterministic
profiles: smoke, feature, full
---
# Telegram Auth Guard

## Intent
- Verify authorized users are accepted and receive normal replies.
- Flag any sign of auth confusion, silent drops, or odd rejection behavior.

## Steps
1. Send: "Hello, are you there? Please respond with a greeting."
   Wait: 60
2. Send: "What is the capital of France? Answer in one word."
   Wait: 60

## Criteria
- The agent responds to both messages (proving the authorized user is accepted by the auth guard)
- The first response is a coherent greeting or acknowledgment
- The second response contains the word "Paris"
- Neither response is an error message, empty, or a rejection/unauthorized notice
