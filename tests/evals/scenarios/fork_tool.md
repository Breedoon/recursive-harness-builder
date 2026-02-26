---
lane: deterministic
profiles: feature, full
---
# Fork Tool

## Steps
1. Send: "What is 2 + 2?"
   Wait: 60
2. Send: "Use the self_fork tool to ask your fork: 'What arithmetic question was I just asked in the parent conversation? Quote it exactly and give the answer.'"
   Wait: 120

## Criteria
- The fork's response references the "2 + 2" question from step 1, proving it inherited conversation history
- The agent does not report errors when using self_fork
- The response includes both the quoted question and the answer (4)
