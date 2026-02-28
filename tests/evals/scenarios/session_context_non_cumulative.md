---
lane: deterministic
profiles: feature, full
---
# Session Context Non-Cumulative

## Intent
Reproduce a realistic multi-turn introspection flow and verify context usage does
not inflate into impossible values (e.g., used > window) after several turns.

## Steps
1. Send: "What is your current session ID and how much context have you used? Use your introspection tools."
   Wait: 90
2. Send: "Do these numbers look reasonable? Briefly explain."
   Wait: 120
3. Send: "Check context again now and report session id + used/window using your introspection tools."
   Wait: 120

## Criteria
- Final response includes session ID + context usage details
- Token usage is non-zero
- If both used and window are present, used does not exceed window
- Response does not report tool errors
