---
lane: judge
profiles: smoke, feature, full
---
# Context Awareness

## Intent
- Validate baseline self/context awareness without file reads.
- Broken behavior example: agent refuses and says it must read files first for basic role/vault context.
- Broken behavior example: agent gives only generic assistant boilerplate with no specific vault/thread detail.
- Suspicious behavior: factual-looking details that conflict with known session context.

## Steps
1. Send: "Without reading any files, tell me: what is your role and what vault are you connected to? What are your current active threads or focus areas?"
   Wait: 90

## Criteria
- The agent describes itself as a personal assistant backed by an Obsidian vault
- The agent mentions at least one specific detail from its context that could not be guessed (e.g., a project name like "OBS Agent MVP", a decision ID like D018, a specific thread topic, or other specific factual content)
- The response does not say it needs to read files first to answer
