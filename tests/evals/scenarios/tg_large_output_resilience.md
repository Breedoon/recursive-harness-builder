# Telegram Large Output Resilience

## Intent
This tests the full Telegram output pipeline under stress: the agent generates a large formatted response, the system converts markdown to Telegram HTML, splits it into multiple messages (4000-char limit), and delivers them. After all that, the session must still be alive for follow-up conversation.

A working system: the user asks for detailed Python explanations, receives a multi-message response that reads coherently with proper formatting (headers, code blocks, bullet points), and then asks a follow-up question that the agent answers with clear memory of what it just explained. From the user's perspective, it's a normal conversation that happens to produce a long answer.

A broken system looks like:
- The bot crashes silently — user gets no response, or one truncated message, or an error notice, and the follow-up gets a blank-slate response as if the conversation never happened
- The agent introduces itself in the follow-up or says something generic that shows it has no memory of what it just explained
- The output contains raw markdown or raw HTML tags visible as text (pipeline failed to convert), or YAML frontmatter / skill file contents dumped into chat
- The messages arrive garbled at split boundaries — a sentence cut in half, a code block opened but never closed

Suspicious: the follow-up response is vague ("you asked about Python") rather than specific ("you covered variables, control flow, functions..."). That might mean the session survived technically but context was lost. Also suspicious: a response far shorter than expected for a 5-topic explanation request — may indicate truncation. Also suspicious: the agent writing to a file instead of explaining in chat — it should respond inline when explicitly asked to.

## Steps
1. Send: "Explain the following Python topics to me right here in this chat — do NOT create any files. For each topic, give at least 2 paragraphs of explanation and one code example. Use headers, code blocks, and bullet points. Topics: (1) variables and types, (2) control flow with if/else/for/while, (3) functions with decorators, (4) classes with inheritance, (5) error handling with try/except."
   Wait: 180
2. Send: "Great. Now list the topics you just explained, in order, with one sentence summarizing each."
   Wait: 60

## Criteria
- The first response explains at least 4 of the 5 requested Python topics directly in the chat, with code examples and formatted structure (headers, bullets, or code blocks)
- The first response reads as coherent continuous content, not truncated mid-sentence or garbled at message boundaries
- The second response demonstrates the agent remembers what it explained — it lists specific topics from the first response, proving session continuity
- The conversation feels like a natural continuation, not a cold start or fresh introduction
- No raw HTML tags, markdown syntax, YAML frontmatter, or skill file contents are visible as plain text in the output
