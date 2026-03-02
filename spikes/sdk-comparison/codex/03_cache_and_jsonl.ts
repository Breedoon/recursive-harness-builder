/**
 * Spike 03: Multi-turn session with cache tracking + JSONL analysis.
 * Creates 4 turns and examines cache behavior at each step.
 *
 * Usage: bun run 03_cache_and_jsonl.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync, existsSync } from "fs";
import { execSync } from "child_process";

interface TokenUsage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
}

async function runTurnStreamed(
  thread: any,
  prompt: string,
  turnLabel: string
): Promise<{ usage: TokenUsage | null; response: string }> {
  console.log(`\n=== ${turnLabel} ===`);
  console.log(`Prompt: ${prompt.substring(0, 80)}`);

  const { events } = await thread.runStreamed(prompt);
  let usage: TokenUsage | null = null;
  let response = "";

  for await (const event of events) {
    if (event.type === "turn.completed") {
      usage = event.usage as TokenUsage;
    }
    if (event.type === "item.completed" && event.item.type === "agent_message") {
      response = event.item.text;
    }
  }

  if (usage) {
    const cacheRate = usage.cached_input_tokens / usage.input_tokens;
    console.log(`  input=${usage.input_tokens} cached=${usage.cached_input_tokens} out=${usage.output_tokens}`);
    console.log(`  cache_rate=${(cacheRate * 100).toFixed(1)}%`);
  }
  console.log(`  response: ${response.substring(0, 100)}`);

  return { usage, response };
}

async function main() {
  const codex = new Codex();

  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  // Turn 1
  const t1 = await runTurnStreamed(
    thread,
    "Remember: SECRET_ALPHA=42. Reply 'Stored.'",
    "Turn 1 (store alpha)"
  );

  // Turn 2 (same thread, should hit cache)
  const t2 = await runTurnStreamed(
    thread,
    "Remember: SECRET_BETA=99. Reply 'Stored.'",
    "Turn 2 (store beta)"
  );

  // Turn 3 (recall — more context = more cache)
  const t3 = await runTurnStreamed(
    thread,
    "What is SECRET_ALPHA? Reply with ONLY the number.",
    "Turn 3 (recall alpha)"
  );

  // Turn 4
  const t4 = await runTurnStreamed(
    thread,
    "What is SECRET_BETA? Reply with ONLY the number.",
    "Turn 4 (recall beta)"
  );

  const threadId = (thread as any)._id;
  console.log(`\nThread ID: ${threadId}`);

  // Find and analyze the JSONL
  const result = execSync(
    `find ~/.codex/sessions -name "*${threadId}*" -type f 2>/dev/null`
  ).toString().trim();

  if (!result) {
    console.log("JSONL file not found!");
    return;
  }

  console.log(`\n=== JSONL Analysis: ${result} ===`);
  const lines = readFileSync(result, "utf-8").split("\n").filter(Boolean);
  console.log(`Total entries: ${lines.length}`);

  // Type breakdown
  const typeCounts: Record<string, number> = {};
  const eventSubtypes: Record<string, number> = {};

  for (const line of lines) {
    const entry = JSON.parse(line);
    typeCounts[entry.type] = (typeCounts[entry.type] || 0) + 1;
    if (entry.type === "event_msg") {
      const st = entry.payload?.type || "?";
      eventSubtypes[st] = (eventSubtypes[st] || 0) + 1;
    }
  }

  console.log("\nEntry types:", typeCounts);
  console.log("Event subtypes:", eventSubtypes);

  // Extract all token_count entries with last_token_usage
  console.log("\n=== Cache Progression ===");
  let turnNum = 0;
  for (let i = 0; i < lines.length; i++) {
    const entry = JSON.parse(lines[i]);
    if (entry.type === "event_msg" && entry.payload?.type === "task_started") {
      turnNum++;
    }
    if (entry.type === "event_msg" && entry.payload?.type === "token_count" && entry.payload?.info) {
      const last = entry.payload.info.last_token_usage;
      const total = entry.payload.info.total_token_usage;
      if (last && last.input_tokens > 0) {
        const cacheRate = last.cached_input_tokens / last.input_tokens;
        console.log(
          `  Turn ${turnNum} [${i}]: last_in=${last.input_tokens} cached=${last.cached_input_tokens} (${(cacheRate * 100).toFixed(1)}%) out=${last.output_tokens} reason=${last.reasoning_output_tokens || 0} | total_in=${total.input_tokens} total_cached=${total.cached_input_tokens}`
        );
      }
    }
  }

  // Save the thread ID for resume tests
  const outPath = "/tmp/codex-spike/thread_id.txt";
  const { writeFileSync } = await import("fs");
  writeFileSync(outPath, threadId);
  console.log(`\nThread ID saved to ${outPath}`);
}

main().catch(console.error);
