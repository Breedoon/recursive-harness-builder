/**
 * Spike 01: Create a basic thread, run a simple prompt, capture the thread ID
 * and examine the resulting JSONL session file.
 *
 * Usage: bun run 01_basic_thread.ts
 */

import { Codex } from "@openai/codex-sdk";

async function main() {
  const codex = new Codex();

  console.log("=== Starting new thread ===");
  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  console.log("Thread created (no ID yet until first run)");

  // Run a simple prompt and stream events
  const { events } = await thread.runStreamed(
    "Reply with EXACTLY: 'CODEWORD_ALPHA_42'. Nothing else."
  );

  let threadId: string | undefined;
  for await (const event of events) {
    switch (event.type) {
      case "thread.started":
        threadId = event.threadId;
        console.log(`\nThread started: ${threadId}`);
        break;
      case "turn.completed":
        console.log(`\nTurn completed.`);
        console.log(`  Usage: ${JSON.stringify(event.usage)}`);
        break;
      case "item.completed":
        const item = event.item;
        console.log(`  Item completed: type=${item.type}`);
        if (item.type === "agent_message") {
          console.log(`    Message: ${item.text.substring(0, 200)}`);
        }
        if (item.type === "reasoning") {
          console.log(`    Reasoning: ${item.text.substring(0, 200)}`);
        }
        break;
      case "item.started":
        console.log(`  Item started: type=${event.item.type}`);
        break;
    }
  }

  console.log("\n=== Thread ID ===");
  console.log(threadId);

  // Now locate and examine the JSONL file
  if (threadId) {
    const { execSync } = await import("child_process");
    const result = execSync(
      `find ~/.codex/sessions -name "*${threadId}*" -type f 2>/dev/null`
    ).toString().trim();
    console.log(`\nJSONL file: ${result}`);

    if (result) {
      // Count entries
      const fs = await import("fs");
      const lines = fs.readFileSync(result, "utf-8").split("\n").filter(Boolean);
      console.log(`Entry count: ${lines.length}`);

      // Parse and summarize
      for (let i = 0; i < lines.length; i++) {
        const entry = JSON.parse(lines[i]);
        const type = entry.type;
        const payload = entry.payload || {};
        let detail = "";

        if (type === "session_meta") {
          detail = `id=${payload.id} source=${payload.source}`;
        } else if (type === "response_item") {
          detail = `role=${payload.role || "?"} type=${payload.type}`;
          if (payload.content?.[0]?.text) {
            detail += ` text=${payload.content[0].text.substring(0, 60)}`;
          }
        } else if (type === "event_msg") {
          detail = `subtype=${payload.type}`;
          if (payload.type === "token_count" && payload.info) {
            detail += ` total_in=${payload.info.total_token_usage?.input_tokens} cached=${payload.info.total_token_usage?.cached_input_tokens}`;
          }
        } else if (type === "turn_context") {
          detail = `turn_id=${payload.turn_id?.substring(0, 16)}... model=${payload.model}`;
        }

        console.log(`  [${i}] ${type}  ${detail}`);
      }
    }
  }
}

main().catch(console.error);
