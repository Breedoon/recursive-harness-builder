/**
 * TypeScript fork worker: forks a Python-created session from a specific message UUID.
 *
 * Usage:
 *   node spikes/ts_fork_worker.mjs <session_id> <message_uuid> [model]
 *
 * Outputs JSON to stdout with: { forkSessionId, response }
 * Prints diagnostics to stderr.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";

const [sessionId, messageUuid, model = "claude-haiku-4-5-20251001", cwd] = process.argv.slice(2);

if (!sessionId || !messageUuid) {
  console.error("Usage: node ts_fork_worker.mjs <session_id> <message_uuid> [model] [cwd]");
  process.exit(1);
}

console.error(`[TS] Forking session ${sessionId} from message ${messageUuid}`);
console.error(`[TS] Model: ${model}`);

let forkSessionId = null;
const parts = [];

const options = {
  resume: sessionId,
  resumeSessionAt: messageUuid,
  forkSession: true,
  model: model,
  permissionMode: "bypassPermissions",
  maxTurns: 1,
};
if (cwd) options.cwd = cwd;

console.error(`[TS] Options: ${JSON.stringify(options)}`);

const conversation = query({
  prompt: "Based on our conversation so far, what is the single most important point? Answer in 2-3 sentences.",
  options,
});

for await (const message of conversation) {
  const msgType = message.type || (message.session_id ? "result" : "unknown");
  console.error(`[TS] msg type=${msgType}`);
  if (message.session_id) {
    forkSessionId = message.session_id;
  }
  // AssistantMessage has content array
  if (message.content && Array.isArray(message.content)) {
    for (const block of message.content) {
      if (block.type === "text" && block.text) {
        parts.push(block.text);
      }
    }
  }
  // Also check message.message.content (some SDK versions nest it)
  if (message.message?.content && Array.isArray(message.message.content)) {
    for (const block of message.message.content) {
      if (block.type === "text" && block.text) {
        parts.push(block.text);
      }
    }
  }
}

const result = {
  forkSessionId,
  response: parts.join("\n"),
};

// Output JSON to stdout for Python to parse
console.log(JSON.stringify(result));
console.error(`[TS] Done. Response length: ${result.response.length} chars`);
