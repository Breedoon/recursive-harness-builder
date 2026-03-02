/**
 * Spike 04: Resume a thread from a new process.
 * Tests whether cache persists across process boundaries (pure API cache).
 *
 * Usage: bun run 04_resume_from_new_process.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync } from "fs";

async function main() {
  const threadId = readFileSync("/tmp/codex-spike/thread_id.txt", "utf-8").trim();
  console.log(`Resuming thread: ${threadId}`);

  const codex = new Codex();
  const thread = codex.resumeThread(threadId);

  // Turn 5: Resume and check cache
  console.log("\n=== Turn 5: Resume from new process ===");
  const { events } = await thread.runStreamed(
    "What are both SECRET_ALPHA and SECRET_BETA? Reply with ONLY: 'ALPHA=X, BETA=Y'"
  );

  for await (const event of events) {
    if (event.type === "turn.completed") {
      const u = event.usage as any;
      const cacheRate = u.cached_input_tokens / u.input_tokens;
      console.log(`  input=${u.input_tokens} cached=${u.cached_input_tokens} out=${u.output_tokens}`);
      console.log(`  cache_rate=${(cacheRate * 100).toFixed(1)}%`);
    }
    if (event.type === "item.completed" && event.item.type === "agent_message") {
      console.log(`  response: ${event.item.text}`);
    }
  }

  // Check the JSONL again
  const { execSync } = await import("child_process");
  const jsonlPath = execSync(
    `find ~/.codex/sessions -name "*${threadId}*" -type f 2>/dev/null`
  ).toString().trim();

  if (jsonlPath) {
    const lines = readFileSync(jsonlPath, "utf-8").split("\n").filter(Boolean);
    console.log(`\nTotal JSONL entries after resume: ${lines.length}`);

    // Show the new entries added by this turn
    console.log("\n=== New entries from this resume ===");
    // Find where turn 5 starts
    let turn5Start = -1;
    let turnCount = 0;
    for (let i = 0; i < lines.length; i++) {
      const entry = JSON.parse(lines[i]);
      if (entry.type === "event_msg" && entry.payload?.type === "task_started") {
        turnCount++;
        if (turnCount === 5) {
          turn5Start = i;
          break;
        }
      }
    }

    if (turn5Start >= 0) {
      // Show entries around turn 5 start — look for re-injected system/developer messages
      const start = Math.max(0, turn5Start - 5);
      for (let i = start; i < lines.length; i++) {
        const entry = JSON.parse(lines[i]);
        const type = entry.type;
        const p = entry.payload || {};
        let detail = "";

        if (type === "response_item") {
          detail = `${p.role || "?"}/${p.type}`;
          if (p.content?.[0]?.text) detail += ` "${p.content[0].text.substring(0, 60)}"`;
        } else if (type === "event_msg") {
          detail = p.type;
          if (p.type === "token_count" && p.info?.last_token_usage) {
            const u = p.info.last_token_usage;
            detail += ` last_in=${u.input_tokens} cached=${u.cached_input_tokens}`;
          }
        } else if (type === "turn_context") {
          detail = `turn=${p.turn_id?.substring(0, 12)}...`;
        }

        const marker = i === turn5Start ? " <<<" : "";
        console.log(`  [${i}] ${type}  ${detail}${marker}`);
      }
    }
  }
}

main().catch(console.error);
