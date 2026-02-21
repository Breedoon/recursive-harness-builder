# Telegram Message Split

## Intent
This tests whether Telegram's message-splitting pipeline delivers long, structured content correctly across multiple messages. Telegram enforces a hard limit per message, so the system must split the agent's response into multiple messages while preserving coherence, formatting, and completeness.

A working system: the user asks for a detailed historical narrative covering 10 distinct milestones. The response arrives across several Telegram messages that read as a continuous, well-formatted narrative. Each milestone is present, the prose flows naturally across message boundaries, and code examples or formatting (headers, bullets, bold) render correctly. The user doesn't notice the splitting at all — it just looks like a long, well-organized answer.

A broken system looks like:
- Only one message arrives, meaning the content was truncated to fit a single message and most milestones are missing
- Messages arrive but content is garbled at split boundaries — a sentence cut in half, a heading separated from its paragraph, a code block opened in one message but never closed
- The response covers only 2-3 milestones instead of the 10 requested, suggesting the content was silently dropped
- Raw HTML tags or markdown syntax appear as visible text, meaning the formatting pipeline failed

Suspicious: the response is short enough to fit in a single Telegram message (under 4000 chars), which means the splitting logic was never actually exercised. A proper test of message splitting requires content long enough to force multiple messages. Also suspicious: the response mentions the milestones by name but gives only a sentence each instead of the requested detail — that's technically responsive but dodges the stress test.

## Steps
1. Send: "Write a detailed explanation of 10 major milestones in computing history: (1) Babbage's Analytical Engine, (2) Ada Lovelace's first algorithm, (3) Turing's contributions and the Turing machine, (4) ENIAC and the dawn of electronic computing, (5) the transistor and integrated circuits, (6) ARPANET and the birth of the Internet, (7) the personal computer revolution, (8) the World Wide Web, (9) smartphones and mobile computing, (10) modern AI and machine learning. Write at least 2 detailed paragraphs per milestone with historical context and significance. Include at least one code example or technical diagram where appropriate."
   Wait: 180

## Criteria
- The response covers at least 7 of the 10 requested milestones with substantive content (not just a mention)
- The response reads as a coherent, continuous narrative — not truncated mid-sentence, not garbled at boundaries, not missing large sections
- The response uses structured formatting (headers, bold text, code blocks, or bullet points visible in the output)
- The content is genuinely detailed — each covered milestone has real historical context, not just a one-line summary
- No raw HTML tags, markdown syntax, or formatting artifacts appear as visible plain text
- No error messages, timeout notices, or session reset indicators in the output
