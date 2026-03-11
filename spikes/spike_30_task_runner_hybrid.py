"""
Spike 30: Hybrid Task Runner — Native tasks + SDK workers.

Architecture:
1. SDK agent creates tasks via TaskCreate (native tool, env vars enabled)
2. Python code monitors ~/.claude/tasks/{team}/ directory
3. When tasks appear, Python spawns ClaudeSDKClient workers
4. Workers fork from main session (getting full context)
5. Workers complete tasks and update status via TaskUpdate
6. Our code streams worker outputs

This is the "best of both worlds" approach.

Writes to /tmp/spike_30.log
"""
import asyncio
import json
import os
import shutil
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_30.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)

TEAM = "spike-30-hybrid"
TASK_DIR = Path.home() / ".claude" / "tasks" / TEAM
CWD = "/Users/breedoon/Documents/obs"

# Enable native task tools
os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
os.environ["CLAUDE_CODE_TASK_LIST_ID"] = TEAM
os.environ["CLAUDE_CODE_TEAM_NAME"] = TEAM

worker_outputs = {}  # task_id -> output text


async def run_worker(task_id, task_data, main_session_id=None):
    """Spawn an SDK worker to handle a task."""
    log(f"  [WORKER] Starting for task {task_id}: {task_data.get('subject', '?')}")

    opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=5,
        cwd=CWD,
    )
    # If we have main session, fork from it
    if main_session_id:
        opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model="haiku",
            max_turns=5,
            cwd=CWD,
            resume=main_session_id,
            fork_session=True,
        )

    client = ClaudeSDKClient(opts)
    texts = []

    try:
        async with client:
            prompt = (
                f"You are working on task: {task_data.get('subject', '?')}\n"
                f"Description: {task_data.get('description', '?')}\n"
                f"Complete this task and report your result."
            )
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                            log(f"  [WORKER {task_id}] {b.text[:200]}")
                elif isinstance(msg, ResultMessage):
                    log(f"  [WORKER {task_id}] Done. SID={msg.session_id[:12]}... Cost=${msg.total_cost_usd:.4f}")
    except Exception as e:
        log(f"  [WORKER {task_id}] ERROR: {e}")
        texts.append(f"ERROR: {e}")

    worker_outputs[task_id] = "\n".join(texts)

    # Update task status
    task_file = TASK_DIR / f"{task_id}.json"
    if task_file.exists():
        data = json.loads(task_file.read_text())
        data["status"] = "completed"
        data["owner"] = f"sdk-worker-{task_id}"
        task_file.write_text(json.dumps(data, indent=2))
        log(f"  [WORKER {task_id}] Task marked completed")


async def task_monitor(main_session_id):
    """Monitor task directory and spawn workers."""
    log("\n  [MONITOR] Starting task monitor...")
    seen_tasks = set()
    workers = []

    for _ in range(30):  # Check for 30 seconds
        await asyncio.sleep(1)

        if not TASK_DIR.exists():
            continue

        for f in TASK_DIR.iterdir():
            if f.suffix != ".json" or f.stem.startswith("."):
                continue

            task_id = f.stem
            if task_id in seen_tasks:
                continue

            data = json.loads(f.read_text())
            status = data.get("status", "")
            if status == "pending":
                seen_tasks.add(task_id)
                log(f"  [MONITOR] Found new task: {task_id} — {data.get('subject', '?')}")
                # Spawn worker
                worker = asyncio.create_task(run_worker(task_id, data, main_session_id))
                workers.append(worker)

    # Wait for all workers
    if workers:
        log(f"  [MONITOR] Waiting for {len(workers)} workers...")
        await asyncio.gather(*workers, return_exceptions=True)
    log(f"  [MONITOR] Done. Outputs: {list(worker_outputs.keys())}")


async def main():
    log("=== Spike 30: Hybrid Task Runner ===")

    # Cleanup
    for d in [TASK_DIR, Path.home() / ".claude" / "teams" / TEAM]:
        if d.exists():
            shutil.rmtree(d)

    # Step 1: Create main agent session
    log("\n--- Step 1: Main agent creates tasks ---")
    main_client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=8,
        cwd=CWD,
    ))
    main_session_id = None

    async with main_client:
        await main_client.query(
            "Remember: I am the OBS assistant. The user's name is Alex.\n\n"
            "Now do these steps:\n"
            "1. Use TaskCreate: subject='Calculate fibonacci', "
            "description='Calculate the 10th fibonacci number (should be 55)', "
            "activeForm='Calculating fibonacci'\n"
            "2. Use TaskCreate: subject='Write a haiku', "
            "description='Write a haiku about programming', "
            "activeForm='Writing haiku'\n"
            "3. Say 'Tasks created' and list them."
        )
        async for msg in main_client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        log(f"  Main: {b.text[:300]}")
            elif isinstance(msg, ResultMessage):
                main_session_id = msg.session_id
                log(f"  Main SID: {main_session_id}")
                log(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Step 2: Run task monitor to spawn workers
    log(f"\n--- Step 2: Task monitor spawns workers ---")

    # Check tasks were created
    if TASK_DIR.exists():
        tasks = list(TASK_DIR.glob("*.json"))
        log(f"  Tasks in directory: {len(tasks)}")
        for t in tasks:
            data = json.loads(t.read_text())
            log(f"    {t.stem}: {data.get('subject')} [{data.get('status')}]")
    else:
        log("  TASK DIR NOT CREATED!")
        LOG.close()
        return

    await task_monitor(main_session_id)

    # Step 3: Report
    log(f"\n--- Step 3: Final Report ---")
    for task_id, output in worker_outputs.items():
        log(f"\n  Task {task_id} output:")
        log(f"    {output[:500]}")

    if TASK_DIR.exists():
        log(f"\n  Final task states:")
        for f in sorted(TASK_DIR.glob("*.json")):
            if not f.stem.startswith("."):
                data = json.loads(f.read_text())
                log(f"    {f.stem}: status={data.get('status')}, owner={data.get('owner')}, subject={data.get('subject')}")

    log(f"\n=== VERDICT ===")
    log(f"  Main agent created tasks: {TASK_DIR.exists() and len(list(TASK_DIR.glob('*.json'))) > 0}")
    log(f"  Workers spawned: {len(worker_outputs)}")
    log(f"  Workers completed: {sum(1 for v in worker_outputs.values() if 'ERROR' not in v)}")

    # Cleanup
    for d in [TASK_DIR, Path.home() / ".claude" / "teams" / TEAM]:
        if d.exists():
            shutil.rmtree(d)

    LOG.close()


asyncio.run(main())
