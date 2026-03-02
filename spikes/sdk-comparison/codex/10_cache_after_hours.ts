/**
 * Spike 10: Cache check after many hours.
 * Single fork from the same baseline as spike 09, measuring if cache still hits.
 */
import { Codex } from "@openai/codex-sdk";
import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { randomUUID } from "crypto";

const SOURCE = "/Users/breedoon/.codex/sessions/2026/02/26/rollout-2026-02-26T11-24-21-019c9ac3-fd29-7553-ae6d-6f717d4a3d14.jsonl";

function createFork(sourcePath: string): string {
  const lines = readFileSync(sourcePath, "utf-8").split("\n").filter(Boolean);
  const forkUuid = randomUUID();
  const today = new Date().toISOString().split("T")[0].split("-");
  const sessionDir = `${process.env.HOME}/.codex/sessions/${today[0]}/${today[1]}/${today[2]}`;
  mkdirSync(sessionDir, { recursive: true });
  const now = new Date().toISOString().replace(/:/g, "-").replace(/\..+/, "");
  const forkPath = `${sessionDir}/rollout-${now}-${forkUuid}.jsonl`;

  const forkLines: string[] = [];
  for (const line of lines) {
    const entry = JSON.parse(line);
    if (entry.type === "session_meta") {
      entry.payload.id = forkUuid;
    }
    forkLines.push(JSON.stringify(entry));
  }
  writeFileSync(forkPath, forkLines.join("\n") + "\n");
  return forkUuid;
}

async function main() {
  const codex = new Codex({ config: { model: "gpt-5.1-codex" } });

  // Calculate time since last measurement
  const lastMeasure = new Date("2026-02-26T18:55:36Z");
  const now = new Date();
  const hoursSince = ((now.getTime() - lastMeasure.getTime()) / 3600000).toFixed(1);
  
  console.log(`\n=== Cache check after ~${hoursSince} hours ===`);
  console.log(`Last measurement: ${lastMeasure.toISOString()}`);
  console.log(`Current time:     ${now.toISOString()}`);

  const forkUuid = createFork(SOURCE);
  console.log(`Fork created: ${forkUuid}`);

  const thread = codex.resumeThread(forkUuid);
  const { events } = await thread.runStreamed("What is SECRET_A? Reply ONLY the value.");
  
  let usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 };
  for await (const event of events) {
    if (event.type === "turn.completed") {
      usage = event.usage as any;
    }
  }

  const uncached = usage.input_tokens - usage.cached_input_tokens;
  const rate = (usage.cached_input_tokens / usage.input_tokens * 100).toFixed(1);
  
  console.log(`\nResult:`);
  console.log(`  input=${usage.input_tokens} cached=${usage.cached_input_tokens} uncached=${uncached} rate=${rate}%`);
  console.log(`\nFor reference, spike 09 baseline (identical context):`);
  console.log(`  input=31403 cached=28544 uncached=2859 rate=90.9%`);
  
  if (usage.cached_input_tokens === 28544) {
    console.log(`\n=> Cache STILL HITTING at full rate after ${hoursSince}h!`);
  } else if (usage.cached_input_tokens > 0) {
    console.log(`\n=> Cache PARTIALLY degraded after ${hoursSince}h`);
  } else {
    console.log(`\n=> Cache MISS - fully evicted after ${hoursSince}h`);
  }
}

main().catch(e => { console.error("FATAL:", e.message); process.exit(1); });
