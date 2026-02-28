---
lane: deterministic
profiles: feature, full
---
# Telegram Tool Visibility

## Intent
- Confirm tool/status visibility is inline and understandable to a human operator.
- Flag responses where tool activity appears hidden, too vague, or disconnected from output chronology.

## Steps
1. Send: "List the files in my .claude/skills directory and tell me how many skills I have."
   Wait: 120

## Criteria
- The response includes a skill count and/or specific skill file names
- The response includes visible inline tool/status indicators (for example words like Read, Grep, Glob, Bash, thinking, or queue-delivered notes)
- The response includes the final completion summary (`context: ... / ...`)
- The response is not a refusal or runtime error
