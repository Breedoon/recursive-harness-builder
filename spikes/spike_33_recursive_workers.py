"""
Spike 33: Recursive task creation — can workers create sub-tasks?

Tests the hybrid approach with recursion:
1. Main agent creates a task
2. Python monitors and spawns worker
3. Worker creates SUB-tasks (has TaskCreate via env vars)
4. Python monitors and spawns sub-workers
5. Sub-workers complete and report

Writes to /tmp/spike_33.log
"""
import asyncio
import json
import os
import shutil
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_33.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

TEAM = "spike-33-recursive"
TASK_DIR = Path.home() / ".claude" / "tasks" / TEAM
CWD = "/Users/breedoon/Documents/obs"

os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = TEAM
os.environ["CLAUDE_CODE_TEAM_NAME"] = TEAM

worker_outputs = {}
MAX_DEPTH = 2  # Max recursion depth


async def run_worker(task_id, task_data, depth=0):
    """Spawn SDK worker. If depth < MAX_DEPTH, worker can create sub-tasks."""
    prefix = "  " * (depth + 1)
    log(f"{prefix}[W-{task_id}] Starting (depth={depth}): {task_data.get('subject')}")

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=5,
        cwd=CWD,
    ))
    texts = []

    try:
        async with client:
            prompt = task_data.get("description", "Complete this task.")
            if depth < MAX_DEPTH:
                prompt += (
                    "\n\nIMPORTANT: If this task can be broken into smaller sub-tasks, "
                    "use TaskCreate to create them. Otherwise just complete it directly."
                )
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                            log(f"{prefix}[W-{task_id}] {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"{prefix}[W-{task_id}] Done. Cost=${msg.total_cost_usd:.4f}")
    except Exception as e:
        log(f"{prefix}[W-{task_id}] ERROR: {e}")
        texts.append(f"ERROR: {e}")

    worker_outputs[task_id] = "\n".join(texts)

    # Mark this task completed
    task_file = TASK_DIR / f"{task_id}.json"
    if task_file.exists():
        data = json.loads(task_file.read_text())
        data["status"] = "completed"
        data["owner"] = f"worker-{task_id}"
        task_file.write_text(json.dumps(data, indent=2))

    return texts


async def task_monitor(timeout=45, depth=0):
    """Monitor for new tasks and spawn workers. Recursive."""
    prefix = "  " * depth
    log(f"{prefix}[MON-{depth}] Monitoring (depth={depth})...")
    seen = set()
    workers = []

    for _ in range(timeout):
        await asyncio.sleep(1)
        if not TASK_DIR.exists():
            continue

        for f in TASK_DIR.iterdir():
            if f.suffix != ".json" or f.stem.startswith("."):
                continue
            tid = f.stem
            if tid in seen:
                continue

            data = json.loads(f.read_text())
            if data.get("status") == "pending" and not data.get("owner"):
                seen.add(tid)
                log(f"{prefix}[MON-{depth}] New task: {tid} — {data.get('subject')}")
                worker = asyncio.create_task(run_worker(tid, data, depth))
                workers.append((tid, worker))

    # Wait for workers
    if workers:
        log(f"{prefix}[MON-{depth}] Waiting for {len(workers)} workers...")
        for tid, w in workers:
            try:
                await asyncio.wait_for(w, timeout=60)
            except asyncio.TimeoutError:
                log(f"{prefix}[MON-{depth}] Worker {tid} timed out")

    # Check if any NEW tasks were created by workers (sub-tasks)
    if depth < MAX_DEPTH and TASK_DIR.exists():
        new_tasks = []
        for f in TASK_DIR.iterdir():
            if f.suffix == ".json" and f.stem not in seen and not f.stem.startswith("."):
                data = json.loads(f.read_text())
                if data.get("status") == "pending":
                    new_tasks.append(f.stem)

        if new_tasks:
            log(f"{prefix}[MON-{depth}] Found {len(new_tasks)} sub-tasks! Recursing...")
            await task_monitor(timeout=30, depth=depth + 1)

    log(f"{prefix}[MON-{depth}] Done.")


async def main():
    log("=== Spike 33: Recursive Task Workers ===")

    # Cleanup
    for d in [TASK_DIR, Path.home() / ".claude" / "teams" / TEAM]:
        if d.exists():
            shutil.rmtree(d)
    TASK_DIR.mkdir(parents=True, exist_ok=True)

    # Seed with an initial task
    log("\n--- Step 1: Create initial task ---")
    initial = {
        "id": "1",
        "subject": "Plan a dinner party",
        "description": (
            "Plan a simple dinner party. Create sub-tasks using TaskCreate for: "
            "1) Menu (subject='Choose menu', description='Pick 3 dishes'), "
            "2) Guest list (subject='Make guest list', description='List 5 names'). "
            "Then say 'Sub-tasks created'."
        ),
        "activeForm": "Planning dinner party",
        "status": "pending",
        "owner": "",
        "blocks": [],
        "blockedBy": [],
    }
    (TASK_DIR / "1.json").write_text(json.dumps(initial, indent=2))
    log(f"  Created initial task: {initial['subject']}")

    # Step 2: Run monitor
    log("\n--- Step 2: Task monitor with recursion ---")
    await task_monitor(timeout=30, depth=0)

    # Step 3: Report
    log(f"\n--- Step 3: Final Report ---")
    if TASK_DIR.exists():
        all_tasks = sorted(TASK_DIR.glob("*.json"))
        log(f"  Total tasks: {len(all_tasks)}")
        for f in all_tasks:
            if not f.stem.startswith("."):
                data = json.loads(f.read_text())
                log(f"    {f.stem}: [{data.get('status')}] {data.get('subject')} (owner: {data.get('owner')})")

    log(f"\n  Worker outputs: {len(worker_outputs)}")
    for tid, out in sorted(worker_outputs.items()):
        log(f"  Task {tid}: {out[:200]}")

    log(f"\n=== VERDICT ===")
    all_tasks_count = len(list(TASK_DIR.glob("*.json"))) if TASK_DIR.exists() else 0
    log(f"  Initial tasks: 1")
    log(f"  Total tasks (incl sub-tasks): {all_tasks_count}")
    log(f"  Recursion worked: {all_tasks_count > 1}")
    log(f"  Workers completed: {len(worker_outputs)}")

    # Cleanup
    for d in [TASK_DIR, Path.home() / ".claude" / "teams" / TEAM]:
        if d.exists():
            shutil.rmtree(d)

    LOG.close()


asyncio.run(main())
