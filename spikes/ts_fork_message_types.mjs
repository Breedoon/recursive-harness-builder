/**
 * TypeScript fork worker: forks from a specific message UUID in a session.
 * Simpler than ts_fork_only_worker — just forks and reports.
 *
 * Usage:
 *   node spikes/ts_fork_message_types.mjs <session_id> <message_uuid> [model] [cwd]
 *
 * Outputs JSON to stdout: { forkSessionId, response, events, error }
 * Diagnostics to stderr.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";
import path from "path";

const [sessionId, messageUuid, model = "claude-haiku-4-5-20251001", cwd] = process.argv.slice(2);

if (!sessionId || !messageUuid) {
  console.error("Usage: node ts_fork_message_types.mjs <session_id> <message_uuid> [model] [cwd]");
  process.exit(1);
}

console.error(`[TS] Forking session ${sessionId} at ${messageUuid} (model: ${model})`);

let forkSessionId = null;
const parts = [];
const events = [];
let error = null;

const options = {
  resume: sessionId,
  resumeSessionAt: messageUuid,
  forkSession: true,
  model: model,
  permissionMode: "bypassPermissions",
  maxTurns: 1,
};
if (cwd) options.cwd = cwd;

try {
  const conversation = query({
    prompt: "Reply with ONLY: ok",
    options,
  });

  for await (const message of conversation) {
    const msgType = message.type || (message.session_id ? "result" : "unknown");
    events.push(msgType);
    if (message.session_id) {
      forkSessionId = message.session_id;
    }
    if (message.content && Array.isArray(message.content)) {
      for (const block of message.content) {
        if (block.type === "text" && block.text) parts.push(block.text);
      }
    }
    if (message.message?.content && Array.isArray(message.message.content)) {
      for (const block of message.message.content) {
        if (block.type === "text" && block.text) parts.push(block.text);
      }
    }
  }
} catch (e) {
  error = e.message || String(e);
  console.error(`[TS] ERROR: ${error}`);
}

const result = {
  forkSessionId,
  response: parts.join("\n"),
  events,
  error,
};

console.log(JSON.stringify(result));
console.error(`[TS] Done. Session: ${forkSessionId}, Response: ${result.response.length} chars`);
