# Queue Message

## Steps
1. SendNowait: "Write a detailed explanation of how binary search works, including pseudocode, time complexity analysis, and three real-world examples of where it is used in production systems"
2. Sleep: 5
3. SendNowait: "Also tell me what 2+2 is"
4. Sleep: 120

## Criteria
- The agent produces a response about binary search
- The output contains "(queued)" indicating the second message was queued during streaming
- The output contains "(queued:" showing the daemon delivered the queued message notification back to the CLI
