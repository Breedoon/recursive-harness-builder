/**
 * Spike 02: Create a thread, capture thread ID properly, then resume it.
 * Test cache behavior on resume.
 *
 * Usage: bun run 02_thread_id_and_resume.ts
 */

import { Codex } from "@openai/codex-sdk";

async function main() {
  const codex = new Codex();

  // --- Turn 1: Create thread ---
  console.log("=== Turn 1: New thread ===");
  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  const turn1 = await thread.run(
    "Remember this secret code: CODEWORD_BRAVO_77. Reply with ONLY: 'Stored.'"
  );

  console.log(`Turn 1 response: ${turn1.finalResponse}`);
  console.log(`Turn 1 items: ${turn1.items.length}`);
  for (const item of turn1.items) {
    console.log(`  item type=${item.type}`);
    if (item.type === "agent_message") console.log(`    text=${item.text.substring(0, 100)}`);
    if (item.type === "reasoning") console.log(`    text=${item.text.substring(0, 100)}`);
  }

  // Get thread ID — check all properties
  const threadObj = thread as any;
  console.log(`\nThread object keys: ${Object.keys(threadObj).filter(k => !k.startsWith('_')).join(', ')}`);
  console.log(`Thread._id: ${threadObj._id}`);
  console.log(`Thread.id: ${threadObj.id}`);
  console.log(`Thread.threadId: ${threadObj.threadId}`);

  // Also check private fields
  for (const key of Object.getOwnPropertyNames(threadObj)) {
    const val = threadObj[key];
    if (typeof val === 'string' && val.length > 10 && val.length < 60) {
      console.log(`Thread.${key} = ${val}`);
    }
  }

  const threadId = threadObj._id || threadObj.id || threadObj.threadId;
  console.log(`\nResolved thread ID: ${threadId}`);

  if (!threadId) {
    console.log("Could not get thread ID. Searching for most recent JSONL...");
    const { execSync } = await import("child_process");
    const result = execSync(
      `find ~/.codex/sessions -name "*.jsonl" -newer /tmp/codex-spike -type f 2>/dev/null | sort | tail -5`
    ).toString().trim();
    console.log(`Recent files:\n${result}`);

    // Find the newest one
    const files = result.split("\n").filter(Boolean);
    if (files.length > 0) {
      const newest = files[files.length - 1];
      console.log(`\nUsing newest: ${newest}`);

      // Extract session ID from filename
      const fs = await import("fs");
      const lines = fs.readFileSync(newest, "utf-8").split("\n").filter(Boolean);
      const meta = JSON.parse(lines[0]);
      if (meta.type === "session_meta") {
        const sessionId = meta.payload.id;
        console.log(`Session ID from meta: ${sessionId}`);

        // --- Turn 2: Resume and check cache ---
        console.log("\n=== Turn 2: Resume same thread ===");
        const thread2 = codex.resumeThread(sessionId);
        const { events } = await thread2.runStreamed(
          "What was the secret code I told you? Reply with ONLY the code."
        );

        for await (const event of events) {
          if (event.type === "turn.completed") {
            console.log(`Turn 2 usage: ${JSON.stringify(event.usage)}`);
          }
          if (event.type === "item.completed" && event.item.type === "agent_message") {
            console.log(`Turn 2 response: ${event.item.text.substring(0, 200)}`);
          }
        }

        // Dump the JSONL structure
        console.log("\n=== JSONL Structure ===");
        const updatedLines = fs.readFileSync(newest, "utf-8").split("\n").filter(Boolean);
        console.log(`Total entries: ${updatedLines.length}`);

        for (let i = 0; i < updatedLines.length; i++) {
          const entry = JSON.parse(updatedLines[i]);
          const type = entry.type;
          const p = entry.payload || {};
          let detail = "";

          if (type === "session_meta") detail = `id=${p.id}`;
          else if (type === "response_item") {
            detail = `${p.role || "?"}/${p.type}`;
            if (p.content?.[0]?.text) detail += ` "${p.content[0].text.substring(0, 50)}"`;
          }
          else if (type === "event_msg") {
            detail = p.type;
            if (p.type === "token_count" && p.info) {
              const u = p.info.last_token_usage;
              if (u) detail += ` last_in=${u.input_tokens} cached=${u.cached_input_tokens} out=${u.output_tokens}`;
            }
            if (p.type === "user_message") detail += ` "${(p.message || "").substring(0, 50)}"`;
            if (p.type === "agent_message") detail += ` "${(p.message || "").substring(0, 50)}"`;
          }
          else if (type === "turn_context") detail = `turn=${p.turn_id?.substring(0, 12)}... model=${p.model}`;

          console.log(`  [${i}] ${type}  ${detail}`);
        }
      }
    }
    return;
  }

  // If we got the ID directly, use it
  console.log("\n=== Turn 2: Resume via thread ID ===");
  const thread2 = codex.resumeThread(threadId);
  const turn2 = await thread2.run(
    "What was the secret code I told you? Reply with ONLY the code."
  );
  console.log(`Turn 2 response: ${turn2.finalResponse}`);
}

main().catch(console.error);
