/**
 * Spike 09: Controlled cache TTL decay measurement.
 *
 * Approach: Build a 3-turn baseline, then at each delay interval create
 * an INDEPENDENT fork (JSONL copy) and resume it. This ensures every
 * measurement has the EXACT SAME context size, so cached_input_tokens
 * is directly comparable across delays.
 *
 * Intervals: 0s, 1m, 4m, 9m, 15m, 45m, 65m
 *
 * Usage: bun run 09_cache_ttl_controlled.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync, writeFileSync, appendFileSync, mkdirSync } from "fs";
import { execSync } from "child_process";
import { randomUUID } from "crypto";

const LOG = "/tmp/codex-cache-ttl-controlled.log";

interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
}

function log(msg: string) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  console.log(line);
  appendFileSync(LOG, line + "\n");
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function runTurn(thread: any, prompt: string): Promise<Usage> {
  const { events } = await thread.runStreamed(prompt);
  let usage: Usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 };
  for await (const event of events) {
    if (event.type === "turn.completed") {
      usage = event.usage as Usage;
    }
  }
  return usage;
}

function createFork(sourcePath: string, upToEntry: number): string {
  const lines = readFileSync(sourcePath, "utf-8").split("\n").filter(Boolean);
  const forkUuid = randomUUID();

  const today = new Date().toISOString().split("T")[0].split("-");
  const sessionDir = `${process.env.HOME}/.codex/sessions/${today[0]}/${today[1]}/${today[2]}`;
  mkdirSync(sessionDir, { recursive: true });

  const now = new Date().toISOString().replace(/:/g, "-").replace(/\..+/, "");
  const forkPath = `${sessionDir}/rollout-${now}-${forkUuid}.jsonl`;

  const forkLines: string[] = [];
  for (let i = 0; i <= upToEntry; i++) {
    const entry = JSON.parse(lines[i]);
    if (entry.type === "session_meta") {
      entry.payload.id = forkUuid;
    }
    forkLines.push(JSON.stringify(entry));
  }

  writeFileSync(forkPath, forkLines.join("\n") + "\n");
  return forkUuid;
}

async function main() {
  writeFileSync(LOG, `Controlled Cache TTL Decay Test - ${new Date().toISOString()}\n\n`);

  const codex = new Codex({
    config: { model: "gpt-5.1-codex" },
  });

  // --- Build baseline session ---
  log("=== Building baseline session (3 turns) ===");
  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  await runTurn(thread, "Remember SECRET_A=DIAMOND. Reply ONLY: 'Stored.'");
  log("Turn 1 done");
  await runTurn(thread, "Remember SECRET_B=EMERALD. Reply ONLY: 'Stored.'");
  log("Turn 2 done");
  await runTurn(thread, "Remember SECRET_C=RUBY. Reply ONLY: 'Stored.'");
  log("Turn 3 done");

  const threadId = (thread as any)._id;
  log(`Baseline thread: ${threadId}`);

  // Find the JSONL file
  const sourcePath = execSync(
    `find ~/.codex/sessions -name "*${threadId}*" -type f 2>/dev/null`
  ).toString().trim();
  log(`Source JSONL: ${sourcePath}`);

  // Find the last entry index (end of turn 3)
  const sourceLines = readFileSync(sourcePath, "utf-8").split("\n").filter(Boolean);
  const lastEntry = sourceLines.length - 1;
  log(`Source has ${sourceLines.length} entries, will fork up to [${lastEntry}]`);

  // --- At each delay: create fork, resume it, measure cache ---
  const delays = [
    { label: "0s",  ms: 0 },
    { label: "1m",  ms: 60_000 },
    { label: "4m",  ms: 4 * 60_000 },
    { label: "9m",  ms: 9 * 60_000 },
    { label: "15m", ms: 15 * 60_000 },
    { label: "45m", ms: 45 * 60_000 },
    { label: "65m", ms: 65 * 60_000 },
  ];

  const results: { label: string; input: number; cached: number; uncached: number; rate: string }[] = [];

  for (const { label, ms } of delays) {
    if (ms > 0) {
      log(`\nSleeping ${label} (${ms / 1000}s)...`);
      await sleep(ms);
    }

    log(`\n=== Measuring at ${label} delay ===`);

    // Create a fresh fork from baseline turn 3
    const forkUuid = createFork(sourcePath, lastEntry);
    log(`  Fork created: ${forkUuid}`);

    // Resume the fork with the same prompt every time
    const forkThread = codex.resumeThread(forkUuid);
    const u = await runTurn(forkThread, "What is SECRET_A? Reply ONLY the value.");

    const uncached = u.input_tokens - u.cached_input_tokens;
    const rate = (u.cached_input_tokens / u.input_tokens * 100).toFixed(1);

    log(`  input=${u.input_tokens} cached=${u.cached_input_tokens} uncached=${uncached} rate=${rate}%`);
    results.push({ label, input: u.input_tokens, cached: u.cached_input_tokens, uncached, rate });
  }

  // --- Summary ---
  log("\n\n========================================");
  log("  CONTROLLED CACHE TTL DECAY SUMMARY");
  log("========================================");
  log("Each measurement uses an INDEPENDENT fork");
  log("with identical context (3-turn baseline).");
  log("Same prompt each time.");
  log("");
  log("Delay     | Input    | Cached   | Uncached | Rate");
  log("----------|----------|----------|----------|------");
  for (const r of results) {
    log(
      `${r.label.padEnd(10)}| ${String(r.input).padEnd(9)}| ${String(r.cached).padEnd(9)}| ${String(r.uncached).padEnd(9)}| ${r.rate}%`
    );
  }
  log("\nDone. Full log at " + LOG);
}

main().catch(e => {
  log(`FATAL: ${e.message}`);
  console.error(e);
});
