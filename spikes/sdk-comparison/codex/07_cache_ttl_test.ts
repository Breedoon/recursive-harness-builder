/**
 * Spike 07: Cache TTL test.
 * Creates a session, waits various durations, then resumes to measure cache decay.
 * Also tests: fork cache hit rate vs resume cache hit rate.
 *
 * Usage: bun run 07_cache_ttl_test.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { execSync } from "child_process";
import { randomUUID } from "crypto";

interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
}

async function runAndGetUsage(thread: any, prompt: string): Promise<Usage> {
  const { events } = await thread.runStreamed(prompt);
  let usage: Usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 };
  for await (const event of events) {
    if (event.type === "turn.completed") {
      usage = event.usage as Usage;
    }
  }
  return usage;
}

async function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function main() {
  const codex = new Codex();

  // --- Create a baseline session ---
  console.log("=== Creating baseline session ===");
  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  // Build 3 turns of context
  const u1 = await runAndGetUsage(thread, "Remember: CACHE_TEST_KEY=DIAMOND. Reply 'Stored.'");
  console.log(`Turn 1: in=${u1.input_tokens} cached=${u1.cached_input_tokens} rate=${(u1.cached_input_tokens/u1.input_tokens*100).toFixed(1)}%`);

  const u2 = await runAndGetUsage(thread, "Remember: CACHE_TEST_VALUE=EMERALD. Reply 'Stored.'");
  console.log(`Turn 2: in=${u2.input_tokens} cached=${u2.cached_input_tokens} rate=${(u2.cached_input_tokens/u2.input_tokens*100).toFixed(1)}%`);

  const u3 = await runAndGetUsage(thread, "Remember: CACHE_TEST_EXTRA=RUBY. Reply 'Stored.'");
  console.log(`Turn 3: in=${u3.input_tokens} cached=${u3.cached_input_tokens} rate=${(u3.cached_input_tokens/u3.input_tokens*100).toFixed(1)}%`);

  const threadId = (thread as any)._id;
  console.log(`\nThread: ${threadId}`);
  writeFileSync("/tmp/codex-spike/cache_test_thread.txt", threadId);

  // --- Immediate resume (0s delay) ---
  console.log("\n=== Immediate resume (0s delay) ===");
  const thread_0s = codex.resumeThread(threadId);
  const u_0s = await runAndGetUsage(thread_0s, "What is CACHE_TEST_KEY? Reply ONLY the value.");
  console.log(`Resume 0s: in=${u_0s.input_tokens} cached=${u_0s.cached_input_tokens} rate=${(u_0s.cached_input_tokens/u_0s.input_tokens*100).toFixed(1)}%`);

  // --- Immediate fork (0s delay) ---
  console.log("\n=== Immediate fork (0s delay) ===");
  // Create fork by copying JSONL
  const originalPath = execSync(
    `find ~/.codex/sessions -name "*${threadId}*" -type f 2>/dev/null`
  ).toString().trim();

  const lines = readFileSync(originalPath, "utf-8").split("\n").filter(Boolean);

  // Find end of turn 3 in original (before the resume added more)
  let turn3End = -1;
  let turnCount = 0;
  for (let i = 0; i < lines.length; i++) {
    const entry = JSON.parse(lines[i]);
    if (entry.type === "event_msg" && entry.payload?.type === "task_complete") {
      turnCount++;
      if (turnCount === 3) {
        turn3End = i;
        break;
      }
    }
  }

  const forkUuid = randomUUID();
  const today = new Date().toISOString().split("T")[0].split("-");
  const sessionDir = `${process.env.HOME}/.codex/sessions/${today[0]}/${today[1]}/${today[2]}`;
  const now = new Date().toISOString().replace(/:/g, "-").replace(/\..+/, "");
  const forkPath = `${sessionDir}/rollout-${now}-${forkUuid}.jsonl`;

  const forkLines: string[] = [];
  for (let i = 0; i <= turn3End; i++) {
    const entry = JSON.parse(lines[i]);
    if (entry.type === "session_meta") entry.payload.id = forkUuid;
    forkLines.push(JSON.stringify(entry));
  }
  writeFileSync(forkPath, forkLines.join("\n") + "\n");

  const forkThread = codex.resumeThread(forkUuid);
  const u_fork_0s = await runAndGetUsage(forkThread, "What is CACHE_TEST_VALUE? Reply ONLY the value.");
  console.log(`Fork 0s:   in=${u_fork_0s.input_tokens} cached=${u_fork_0s.cached_input_tokens} rate=${(u_fork_0s.cached_input_tokens/u_fork_0s.input_tokens*100).toFixed(1)}%`);

  // --- Summary ---
  console.log("\n=== Cache Summary ===");
  console.log("Turn 3 (last build):   rate=" + (u3.cached_input_tokens/u3.input_tokens*100).toFixed(1) + "%");
  console.log("Resume (0s):           rate=" + (u_0s.cached_input_tokens/u_0s.input_tokens*100).toFixed(1) + "%");
  console.log("Fork (0s):             rate=" + (u_fork_0s.cached_input_tokens/u_fork_0s.input_tokens*100).toFixed(1) + "%");
  console.log("\nFor cache TTL: rerun this script after 5m, 30m, 1h to observe decay.");
  console.log("Thread ID for later: " + threadId);
}

main().catch(console.error);
