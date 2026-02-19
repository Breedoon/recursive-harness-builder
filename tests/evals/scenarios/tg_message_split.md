# Telegram Message Split

## Intent
- Ensure long outputs remain coherent across split Telegram messages.
- Flag if the output looks truncated, duplicated oddly, or semantically broken at chunk boundaries.

## Steps
1. Send: "Write a detailed explanation of 5 major milestones in computing history: Babbage's Analytical Engine, Turing's contributions, ENIAC, the transistor, and the Internet. Write 2-3 paragraphs per milestone. Make it comprehensive."
   Wait: 120

## Criteria
- The response is substantive and covers computing history milestones
- The response mentions at least 3 of these topics: Babbage, Turing, ENIAC, transistor, Internet
- The response is at least 1500 characters long (proving multi-message delivery works)
- The content is coherent and reads as a continuous narrative, not cut off mid-sentence
- The response does not contain error messages or timeout notices
