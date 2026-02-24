# Telegram Transport Desync Forced Concurrency

## Intent
- Reproduce production desync by explicitly stress-testing Telegram transport with a single huge concurrent-read request.
- We tell the agent it is running in Telegram and that this is a transport stress test, so it should maximize same-turn read concurrency and avoid phased/sequential chatter.
- Fail if turn 2 (ping) is polluted by leftover turn-1 tool output, or if `(done)` is emitted before output is actually finished.

## Steps
1. Send: "You are in a Telegram transport stress test right now. Goal: maximize read-tool concurrency in one assistant turn to stress message delivery. Issue as many concurrent Read tool calls as possible in one batch with no phased execution. Do NOT drip progress in multiple mini-batches if avoidable. Read ALL of these files and then reply with exactly `FORCED_STRESS_DONE` when complete: Ж/D/2019-12-24.md, Ж/D/2019-12-25.md, Ж/D/2019-12-26.md, Ж/D/2019-12-27.md, Ж/D/2019-12-28.md, Ж/D/2019-12-29.md, Ж/D/2019-12-30.md, Ж/D/2019-12-31.md, Ж/D/2020-01-01.md, Ж/D/2020-01-02.md, Ж/D/2020-01-03.md, Ж/D/2020-01-04.md, Ж/D/2020-01-05.md, Ж/D/2020-01-06.md, Ж/D/2020-01-07.md, Ж/D/2020-01-08.md, Ж/D/2020-01-09.md, Ж/D/2020-01-10.md, Ж/D/2020-01-11.md, Ж/D/2020-01-12.md, Ж/D/2020-01-13.md, Ж/D/2020-01-14.md, Ж/D/2020-01-15.md, Ж/D/2020-01-16.md, Ж/D/2020-01-17.md, Ж/D/2020-01-18.md, Ж/D/2020-01-19.md, Ж/D/2020-01-20.md, Ж/D/2020-01-21.md, Ж/D/2020-01-22.md, Ж/D/2020-01-23.md, Ж/D/2020-01-24.md, Ж/D/2020-01-25.md, Ж/D/2020-01-26.md, Ж/D/2020-01-27.md, Ж/D/2020-01-28.md, Ж/D/2020-01-29.md, Ж/D/2020-01-30.md, Ж/D/2020-01-31.md, Ж/D/2020-02-01.md, Ж/D/2020-02-02.md, Ж/D/2020-02-03.md, Ж/D/2020-02-04.md, Ж/D/2020-02-05.md, Ж/D/2020-02-06.md, Ж/D/2020-02-07.md, Ж/D/2020-02-08.md, Ж/D/2020-02-09.md, Ж/D/2020-02-10.md, Ж/D/2020-02-11.md, Ж/D/2020-02-12.md, Ж/D/2020-02-13.md, Ж/D/2020-02-14.md, Ж/D/2020-02-15.md, Ж/D/2020-02-16.md, Ж/D/2020-02-17.md, Ж/D/2020-02-18.md, Ж/D/2020-02-19.md, Ж/D/2020-02-20.md, Ж/D/2020-02-21.md"
   Wait: 300
2. Send: "ping"
   Wait: 120

## Criteria
- Turn 1 shows very high read-tool activity and includes `FORCED_STRESS_DONE`
- Turn 2 responds directly to `ping` (e.g. `pong`)
- Turn 2 is not contaminated by stale `Read:` lines from turn 1 before handling ping
- `(done)` should align with true completion, not appear before late backlog from the same logical turn
- Judge should fail on any backlog leak or order drift even if final content eventually appears
