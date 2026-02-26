/**
 * TypeScript fork-only worker: attempts to fork a session WITHOUT sending a message.
 *
 * Tests whether TS SDK can create a usable session ID from a mid-conversation
 * fork point that Python can immediately resume — without TS adding any turns.
 *
 * Modes:
 *   "zero"    — maxTurns: 0, prompt provided but no turns executed
 *   "abort"   — maxTurns: 1, exit after grabbing first stream event
 *   "control" — maxTurns: 1, normal full execution (known to work)
 *
 * Usage:
 *   node spikes/ts_fork_only_worker.mjs <session_id> <message_uuid> <mode> [model] [cwd]
 *
 * Outputs JSON to stdout: { forkSessionId, response, mode, events, error }
 * Diagnostics to stderr.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";
import fs from "fs";
import path from "path";
import os from "os";

const [sessionId, messageUuid, mode = "zero", model = "claude-haiku-4-5-20251001", cwd] = process.argv.slice(2);

if (!sessionId || !messageUuid) {
  console.error("Usage: node ts_fork_only_worker.mjs <session_id> <message_uuid> <mode> [model] [cwd]");
  process.exit(1);
}

console.error(`[TS] Mode: ${mode}`);
console.error(`[TS] Forking session ${sessionId} from message ${messageUuid}`);
console.error(`[TS] Model: ${model}`);

// Scan for existing session files BEFORE we start (to detect new ones later)
const projectsDir = path.join(os.homedir(), ".claude", "projects");
function findAllSessionFiles() {
  const files = new Set();
  if (!fs.existsSync(projectsDir)) return files;
  for (const dir of fs.readdirSync(projectsDir)) {
    const full = path.join(projectsDir, dir);
    if (!fs.statSync(full).isDirectory()) continue;
    for (const f of fs.readdirSync(full)) {
      if (f.endsWith(".jsonl")) files.add(path.join(full, f));
    }
  }
  return files;
}

const beforeFiles = findAllSessionFiles();

let forkSessionId = null;
const parts = [];
const events = [];
let error = null;

const baseOptions = {
  resume: sessionId,
  resumeSessionAt: messageUuid,
  forkSession: true,
  model: model,
  permissionMode: "bypassPermissions",
};
if (cwd) baseOptions.cwd = cwd;

try {
  if (mode === "zero") {
    // Mode A: maxTurns: 0 — does the SDK even accept this?
    const options = { ...baseOptions, maxTurns: 0 };
    console.error(`[TS] Options: ${JSON.stringify(options)}`);

    const conversation = query({
      prompt: "Continue from where we left off.",
      options,
    });

    for await (const message of conversation) {
      const msgType = message.type || (message.session_id ? "result" : "unknown");
      events.push(msgType);
      console.error(`[TS] event: ${msgType}`);
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

  } else if (mode === "abort") {
    // Mode B: maxTurns: 1, but exit after first stream event
    const options = { ...baseOptions, maxTurns: 1 };
    console.error(`[TS] Options: ${JSON.stringify(options)}`);

    const conversation = query({
      prompt: ".",
      options,
    });

    for await (const message of conversation) {
      const msgType = message.type || (message.session_id ? "result" : "unknown");
      events.push(msgType);
      console.error(`[TS] event: ${msgType} (aborting after this)`);
      if (message.session_id) {
        forkSessionId = message.session_id;
      }
      // Grab whatever we can, then break out
      break;
    }

    // Give CLI a moment to flush the JSONL
    await new Promise(r => setTimeout(r, 2000));

  } else if (mode === "control") {
    // Mode C: normal fork with a real prompt (known to work)
    const options = { ...baseOptions, maxTurns: 1 };
    console.error(`[TS] Options: ${JSON.stringify(options)}`);

    const conversation = query({
      prompt: "Based on our conversation so far, what is the single most important point? Answer in 2-3 sentences.",
      options,
    });

    for await (const message of conversation) {
      const msgType = message.type || (message.session_id ? "result" : "unknown");
      events.push(msgType);
      console.error(`[TS] event: ${msgType}`);
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
  }

} catch (e) {
  error = e.message || String(e);
  console.error(`[TS] ERROR: ${error}`);
}

// Check for new session files
const afterFiles = findAllSessionFiles();
const newFiles = [...afterFiles].filter(f => !beforeFiles.has(f));
console.error(`[TS] New session files detected: ${newFiles.length}`);
for (const f of newFiles) {
  console.error(`[TS]   ${f}`);
}

// If we didn't get a session ID from the stream, try to extract from new files
if (!forkSessionId && newFiles.length > 0) {
  // The new file's name (minus .jsonl) is the session ID
  const newest = newFiles[newFiles.length - 1];
  forkSessionId = path.basename(newest, ".jsonl");
  console.error(`[TS] Inferred session ID from new file: ${forkSessionId}`);
}

const result = {
  forkSessionId,
  response: parts.join("\n"),
  mode,
  events,
  error,
  newFiles: newFiles.map(f => path.basename(f)),
};

console.log(JSON.stringify(result));
console.error(`[TS] Done. Session: ${forkSessionId}, Response: ${result.response.length} chars`);
