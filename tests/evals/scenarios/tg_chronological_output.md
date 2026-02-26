---
lane: judge
profiles: feature, full
---
# Telegram Chronological Output

## Intent
- Ensure observable chronology: tool/status lines and text should make sense together as one timeline.
- Broken behavior example: duplicated/misaligned done sentinels or disjoint turn ordering.
- Suspicious behavior: subtle ordering drift where content is mostly correct but timeline markers are inconsistent.
- Flag odd sequencing (for example duplicate done sentinels, disjointed tool text, or off-order content) even if the strict criteria pass.

## Steps
1. Send: "Read CLAUDE.md and then give me a 2-sentence summary. End your response with the exact token CHRONO_CHECK_OK."
   Wait: 120

## Criteria
- The response includes the exact token CHRONO_CHECK_OK
- The response includes visible tool/status indicators inline with content (for example Read/Grep/Bash/thinking)
- The response contains a coherent summary grounded in CLAUDE.md content
- The response includes the `(done)` sentinel
