# Telegram Chronological Output

## Steps
1. Send: "Read CLAUDE.md and then give me a 2-sentence summary. End your response with the exact token CHRONO_CHECK_OK."
   Wait: 120

## Criteria
- The response includes the exact token CHRONO_CHECK_OK
- The response includes visible tool/status indicators inline with content (for example Read/Grep/Bash/thinking)
- The response contains a coherent summary grounded in CLAUDE.md content
- The response includes the `(done)` sentinel
