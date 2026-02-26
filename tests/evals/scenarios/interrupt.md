---
lane: deterministic
profiles: feature, full
---
# Interrupt

## Steps
1. SendNowait: "Read CLAUDE.md and produce a 200-item numbered list where each item starts with INTERRUPT_TEST_ and includes one short sentence grounded in that file. Do not stop early."
2. Sleep: 5
3. SendNowait: "/stop"
4. Sleep: 30

## Criteria
- The output contains "(interrupting...)" indicating the /stop command was received
- The agent's response is cut short and does not contain a natural conclusion or complete 200-item list
- The agent does not produce a complete 200-item list
