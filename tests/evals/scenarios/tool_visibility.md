---
lane: deterministic
profiles: feature, full
---
# Tool Visibility

## Steps
1. Send: "List the files in my .claude/skills directory"
   Wait: 90

## Criteria
- The agent responds with a list of skill names or directories
- The response contains at least two real skill names (e.g., file-conventions, update-context, session-offboard)
- The response is not a refusal or error
