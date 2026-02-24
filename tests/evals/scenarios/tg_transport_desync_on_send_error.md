# Telegram Transport Desync On Send Error

## Intent
This tests the specific desync failure mode reported in production: a large burst of tool/status output during one turn causes Telegram send errors, the bot emits `(done)` anyway, and then stale output from the previous turn leaks into the next user turn.

A working system: after the stress turn completes, the next user message (`ping`) gets a clean immediate ping response. No leftover tool/status lines from the previous turn appear in the ping turn.

A broken system looks like:
- `(done)` appears for the stress turn, but the next turn starts by dumping leftover `Read:` lines from the previous turn
- The ping turn is delayed or polluted by old backlog before the ping acknowledgement
- The bot claims completion but still has undelivered output that only appears when the user sends another message

Suspicious: intermittent `(error sending message — try again)` is acceptable only if chronology remains correct and ping turn is not contaminated by stale prior-turn output.

## Steps
1. Send: "Stress the Telegram transport. Use separate Read tool calls for each file below (do not summarize without reading). After reading all files, reply with exactly `FILE_STRESS_DONE`. Files: Ж/D/2020-12-21.md, Ж/D/2020-12-22.md, Ж/D/2020-12-23.md, Ж/D/2020-12-24.md, Ж/D/2020-12-27.md, Ж/D/2020-12-29.md, Ж/D/2021-01-01.md, Ж/D/2021-01-03.md, Ж/D/2021-01-04.md, Ж/D/2021-01-06.md, Ж/D/2021-01-07.md, Ж/D/2021-01-08.md, Ж/D/2021-01-09.md, Ж/D/2021-01-10.md, Ж/D/2021-01-11.md, Ж/D/2021-01-14.md, Ж/D/2021-01-15.md, Ж/D/2021-01-16.md, Ж/D/2021-01-18.md, Ж/D/2021-01-19.md"
   Wait: 240
2. Send: "ping"
   Wait: 120

## Criteria
- The first turn includes clear evidence of high tool/read activity and ends with `FILE_STRESS_DONE`
- The second turn responds to `ping` directly (e.g., `pong`/`ping`) as its primary intent
- The second turn does NOT contain stale `Read:` lines or other leftover stress-turn content before addressing `ping`
- The interaction demonstrates chronological integrity: no post-completion backlog leak from turn 1 into turn 2
- The transcript includes `(done)` sentinels and they align with actual turn completion behavior
