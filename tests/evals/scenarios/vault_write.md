# Vault Write

## Steps
1. Send: "Create a quick note in my drafts folder called eval-test-write with the content 'EVAL_WRITE_OK'"
   Wait: 120
2. Send: "Read the file .claude/drafts/eval-test-write.md and tell me its exact contents"
   Wait: 90

## Criteria
- The agent does not report permission errors or refusals when creating the file
- The agent's response to step 2 contains the exact string EVAL_WRITE_OK, demonstrating the file exists and was read via the Read tool
- The response does not indicate the file was not found or could not be read
