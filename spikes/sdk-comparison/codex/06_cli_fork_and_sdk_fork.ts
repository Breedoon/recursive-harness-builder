/**
 * Spike 06: Test the official `codex fork` CLI command via SDK/subprocess.
 * Also test if the SDK exposes any fork method.
 *
 * Usage: bun run 06_cli_fork_and_sdk_fork.ts
 */

import { Codex } from "@openai/codex-sdk";
import { readFileSync, writeFileSync, existsSync, readdirSync } from "fs";
import { execSync } from "child_process";

async function main() {
  const originalThreadId = readFileSync("/tmp/codex-spike/thread_id.txt", "utf-8").trim();
  console.log(`Original thread: ${originalThreadId}`);

  // --- Check SDK API for fork methods ---
  console.log("\n=== SDK API Inspection ===");
  const codex = new Codex();

  // Check Codex class methods
  const codexProto = Object.getOwnPropertyNames(Object.getPrototypeOf(codex));
  console.log(`Codex methods: ${codexProto.filter(m => m !== 'constructor').join(', ')}`);

  // Check Thread class methods
  const thread = codex.startThread({ workingDirectory: "/tmp/codex-spike", skipGitRepoCheck: true });
  const threadProto = Object.getOwnPropertyNames(Object.getPrototypeOf(thread));
  console.log(`Thread methods: ${threadProto.filter(m => m !== 'constructor').join(', ')}`);

  // Check for fork-related methods
  const forkMethods = [...codexProto, ...threadProto].filter(
    m => m.toLowerCase().includes("fork") || m.toLowerCase().includes("branch") || m.toLowerCase().includes("clone")
  );
  console.log(`Fork-related methods: ${forkMethods.length > 0 ? forkMethods.join(', ') : 'NONE'}`);

  // --- Try `codex exec resume --last` to see args ---
  console.log("\n=== CLI fork command check ===");
  try {
    const help = execSync("codex fork --help 2>&1", { timeout: 5000 }).toString();
    console.log("codex fork --help output:");
    console.log(help.substring(0, 500));
  } catch (err: any) {
    console.log(`codex fork --help error: ${err.message.substring(0, 200)}`);
    // Try stderr
    if (err.stderr) console.log(`stderr: ${err.stderr.toString().substring(0, 200)}`);
  }

  // --- Try non-interactive fork ---
  console.log("\n=== Attempting codex exec fork (non-interactive) ===");
  try {
    const result = execSync(
      `codex fork ${originalThreadId} 2>&1`,
      { timeout: 10000, env: { ...process.env, TERM: "dumb" } }
    ).toString();
    console.log(`Result: ${result.substring(0, 300)}`);
  } catch (err: any) {
    const output = err.stdout?.toString() || err.stderr?.toString() || err.message;
    console.log(`Fork attempt result: ${output.substring(0, 300)}`);
  }

  // --- Check what options codex exec supports ---
  console.log("\n=== codex exec --help ===");
  try {
    const help = execSync("codex exec --help 2>&1", { timeout: 5000 }).toString();
    console.log(help.substring(0, 800));
  } catch (err: any) {
    console.log(`Error: ${err.message.substring(0, 200)}`);
  }

  // --- Check the full codex --help ---
  console.log("\n=== codex --help (subcommands) ===");
  try {
    const help = execSync("codex --help 2>&1", { timeout: 5000 }).toString();
    console.log(help.substring(0, 800));
  } catch (err: any) {
    console.log(`Error: ${err.message.substring(0, 200)}`);
  }
}

main().catch(console.error);
