---
lane: deterministic
profiles: feature, full
continuation_timeouts: 180,30
---
# Telegram Inbound Batching

## Intent
- Verify rapid-fire same-chat user messages are held until the inbound quiet period expires, then injected together as one batch.
- Broken behavior: the bot starts work on the first fragment immediately, then later parts show up as queued-message delivery inside the active run.
- Broken behavior: the batch is split across multiple completed turns or loses one of the later parts.

## Steps
1. SendNowait: "Batch 1/3 of one forwarded corpus. Do not answer yet; wait for the rest of the batch. Section A covers Babbage's Analytical Engine and Ada Lovelace's algorithm, emphasizing programmability, punched-card control, and the idea that machines can manipulate symbols rather than just numbers. When you finally answer, include token ALPHA."
2. SendNowait: "Batch 2/3 of the same forwarded corpus. Section B covers Turing's universal machine, wartime computing, ENIAC, and the transistor-to-integrated-circuit transition, emphasizing why general-purpose computation became practical. When you finally answer, include token BRAVO."
3. SendNowait: "Batch 3/3 of the same forwarded corpus. Section C covers ARPANET, the personal computer revolution, the Web, smartphones, and modern machine learning. After the whole batch is received, reply with one line starting exactly `BATCH_OK: ALPHA BRAVO CHARLIE`, then one compact sentence summarizing all three sections together."
4. Sleep: 120

## Criteria
- The output contains the exact batch confirmation line with `ALPHA BRAVO CHARLIE`
- The output is a single collected completed turn, not multiple finished turns
- The output does not contain `queued message delivered`, which would indicate the bot started before the full batch arrived
- The output ends with the final completion summary (`context: ... / ...`)
