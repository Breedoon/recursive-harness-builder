/**
 * Spike 05: Fork a session by copying its JSONL file.
 * Tests whether Codex can resume from a manually copied JSONL.
 *
 * Approach:
 * 1. Copy the JSONL from spike 03/04 to a new file with a new UUID
 * 2. Truncate at a specific point (after turn 2)
 * 3. Try to resume from the copy
 *
 * Usage: bun run 05_jsonl_fork.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { execSync } from "child_process";
import { randomUUID } from "crypto";

async function main() {
  const originalThreadId = readFileSync("/tmp/codex-spike/thread_id.txt", "utf-8").trim();
  console.log(`Original thread: ${originalThreadId}`);

  // Find original JSONL
  const originalPath = execSync(
    `find ~/.codex/sessions -name "*${originalThreadId}*" -type f 2>/dev/null`
  ).toString().trim();
  console.log(`Original JSONL: ${originalPath}`);

  const lines = readFileSync(originalPath, "utf-8").split("\n").filter(Boolean);
  console.log(`Original entries: ${lines.length}`);

  // Find the end of turn 2 (after task_complete for turn 2)
  let turn2End = -1;
  let turnCount = 0;
  for (let i = 0; i < lines.length; i++) {
    const entry = JSON.parse(lines[i]);
    if (entry.type === "event_msg" && entry.payload?.type === "task_complete") {
      turnCount++;
      if (turnCount === 2) {
        turn2End = i;
        break;
      }
    }
  }

  console.log(`Turn 2 ends at entry [${turn2End}]`);

  // --- Approach A: Copy with new UUID in session_meta, same filename format ---
  const newUuid = randomUUID();
  console.log(`\nFork UUID: ${newUuid}`);

  // Determine where to write the fork
  const today = new Date().toISOString().split("T")[0].split("-"); // [YYYY, MM, DD]
  const sessionDir = `${process.env.HOME}/.codex/sessions/${today[0]}/${today[1]}/${today[2]}`;
  mkdirSync(sessionDir, { recursive: true });

  const now = new Date().toISOString().replace(/:/g, "-").replace(/\..+/, "");
  const forkFilename = `rollout-${now}-${newUuid}.jsonl`;
  const forkPath = `${sessionDir}/${forkFilename}`;

  // Copy entries up to and including turn 2 end
  const forkLines: string[] = [];
  for (let i = 0; i <= turn2End; i++) {
    const entry = JSON.parse(lines[i]);

    // Patch the session_meta with new UUID
    if (entry.type === "session_meta") {
      entry.payload.id = newUuid;
    }

    forkLines.push(JSON.stringify(entry));
  }

  writeFileSync(forkPath, forkLines.join("\n") + "\n");
  console.log(`Fork written: ${forkPath}`);
  console.log(`Fork entries: ${forkLines.length} (turns 1-2 only)`);

  // --- Try to resume from the fork ---
  console.log("\n=== Resume from fork (new UUID) ===");
  const codex = new Codex();

  try {
    const forkThread = codex.resumeThread(newUuid);
    const { events } = await forkThread.runStreamed(
      "What is SECRET_ALPHA? Reply with ONLY the number."
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

    // Check the fork JSONL after
    const updatedFork = readFileSync(forkPath, "utf-8").split("\n").filter(Boolean);
    console.log(`\nFork entries after resume: ${updatedFork.length}`);

    // Show new entries
    console.log("New entries added:");
    for (let i = forkLines.length; i < updatedFork.length; i++) {
      const entry = JSON.parse(updatedFork[i]);
      const type = entry.type;
      const p = entry.payload || {};
      let detail = "";
      if (type === "response_item") {
        detail = `${p.role || "?"}/${p.type}`;
        if (p.content?.[0]?.text) detail += ` "${p.content[0].text.substring(0, 60)}"`;
      } else if (type === "event_msg") {
        detail = p.type;
      } else if (type === "turn_context") {
        detail = `turn=${p.turn_id?.substring(0, 12)}...`;
      }
      console.log(`  [${i}] ${type}  ${detail}`);
    }
  } catch (err: any) {
    console.log(`\nFork resume FAILED: ${err.message}`);

    // --- Approach B: Try using original UUID ---
    console.log("\n=== Approach B: Copy but keep original UUID ===");
    // The CLI might look up sessions by their session_meta.id, not filename
    // Let's try copying with the ORIGINAL ID but different filename

    const forkPath2 = `${sessionDir}/rollout-${now}-fork-test.jsonl`;
    const forkLines2: string[] = [];
    for (let i = 0; i <= turn2End; i++) {
      forkLines2.push(lines[i]); // Keep original entries unchanged
    }
    writeFileSync(forkPath2, forkLines2.join("\n") + "\n");
    console.log(`Fork2 written: ${forkPath2}`);

    try {
      const forkThread2 = codex.resumeThread(originalThreadId);
      const { events: events2 } = await forkThread2.runStreamed(
        "What is SECRET_BETA? Reply with ONLY the number."
      );

      for await (const event of events2) {
        if (event.type === "turn.completed") {
          const u = event.usage as any;
          console.log(`  input=${u.input_tokens} cached=${u.cached_input_tokens}`);
        }
        if (event.type === "item.completed" && event.item.type === "agent_message") {
          console.log(`  response: ${event.item.text}`);
        }
      }
    } catch (err2: any) {
      console.log(`Fork2 resume also FAILED: ${err2.message}`);
    }
  }
}

main().catch(console.error);
