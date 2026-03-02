/**
 * Spike 08: Cache TTL decay measurement.
 * Creates a session, then resumes at increasing delays to observe cache decay.
 *
 * Intervals: 0s, 1m, 4m, 9m, 15m, 45m, 65m
 * Total runtime: ~140 minutes
 *
 * Usage: bun run 08_cache_ttl_decay.ts
 */

import { Codex } from "@openai/codex-sdk";
import { appendFileSync, writeFileSync } from "fs";

const LOG = "/tmp/codex-cache-ttl.log";

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

async function main() {
  writeFileSync(LOG, `Cache TTL Decay Test - ${new Date().toISOString()}\n\n`);

  // ChatGPT subscription only supports gpt-5.x-codex models
  // gpt-5.1-codex is somewhat cheaper than gpt-5.3-codex
  const codex = new Codex({
    config: { model: "gpt-5.1-codex" },
  });

  // --- Build a session with some context ---
  log("=== Building baseline session ===");
  const thread = codex.startThread({
    workingDirectory: "/tmp/codex-spike",
    skipGitRepoCheck: true,
  });

  const u1 = await runTurn(thread, "Remember SECRET_A=DIAMOND. Reply ONLY: 'Stored.'");
  log(`Turn 1: in=${u1.input_tokens} cached=${u1.cached_input_tokens} rate=${(u1.cached_input_tokens/u1.input_tokens*100).toFixed(1)}%`);

  const u2 = await runTurn(thread, "Remember SECRET_B=EMERALD. Reply ONLY: 'Stored.'");
  log(`Turn 2: in=${u2.input_tokens} cached=${u2.cached_input_tokens} rate=${(u2.cached_input_tokens/u2.input_tokens*100).toFixed(1)}%`);

  const u3 = await runTurn(thread, "Remember SECRET_C=RUBY. Reply ONLY: 'Stored.'");
  log(`Turn 3: in=${u3.input_tokens} cached=${u3.cached_input_tokens} rate=${(u3.cached_input_tokens/u3.input_tokens*100).toFixed(1)}%`);

  const threadId = (thread as any)._id;
  log(`Thread: ${threadId}`);
  log(`Baseline built. Total input after 3 turns: ${u3.input_tokens} (cumulative from SDK)`);

  // --- Resume at increasing delays ---
  const delays = [
    { label: "0s",  ms: 0 },
    { label: "1m",  ms: 60_000 },
    { label: "4m",  ms: 4 * 60_000 },
    { label: "9m",  ms: 9 * 60_000 },
    { label: "15m", ms: 15 * 60_000 },
    { label: "45m", ms: 45 * 60_000 },
    { label: "65m", ms: 65 * 60_000 },
  ];

  const results: { label: string; delay_ms: number; input: number; cached: number; rate: string }[] = [];

  for (const { label, ms } of delays) {
    if (ms > 0) {
      log(`\nSleeping ${label} (${ms/1000}s)...`);
      await sleep(ms);
    }

    log(`\n=== Resume after ${label} ===`);
    const resumeThread = codex.resumeThread(threadId);
    const u = await runTurn(
      resumeThread,
      `What is SECRET_A? Reply ONLY the value. (delay=${label})`
    );
    const rate = (u.cached_input_tokens / u.input_tokens * 100).toFixed(1);
    log(`  in=${u.input_tokens} cached=${u.cached_input_tokens} rate=${rate}%`);
    results.push({ label, delay_ms: ms, input: u.input_tokens, cached: u.cached_input_tokens, rate });
  }

  // --- Summary ---
  log("\n\n=== CACHE TTL DECAY SUMMARY ===");
  log("Delay     | Input    | Cached   | Rate");
  log("----------|----------|----------|------");
  for (const r of results) {
    log(`${r.label.padEnd(10)}| ${String(r.input).padEnd(9)}| ${String(r.cached).padEnd(9)}| ${r.rate}%`);
  }

  log("\nDone. Full log at " + LOG);
}

main().catch(e => {
  log(`FATAL: ${e.message}`);
  console.error(e);
});
