---
lane: deterministic
profiles: feature, full
---
# Session & Context Info

## Intent
Verify that the agent can report its own session metadata and context window usage
via the `session_info` and `context_info` MCP tools.

## Steps
1. Send: "Hi, what's 2 + 2?"
   Wait: 60
2. Send: "What is your current session ID and how much of your context window have you used? Use your introspection tools."
   Wait: 120

## Criteria
- The agent reports a session ID (a non-empty string, typically UUID-like)
- The agent reports non-zero token usage numbers (input_tokens and/or output_tokens must be greater than zero — zero counts indicate a bug)
- The agent mentions context window remaining as a percentage that is NOT 100% (100% means no tokens were counted, which is a bug)
- The agent does not report errors when using the tools
