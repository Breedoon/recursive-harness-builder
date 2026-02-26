"""Spike: Python SDK fork cache behavior baseline.

Creates a session with a few turns to build up context, then forks it.
Measures cache hit rates on resumed turns vs the fork.

Usage: .venv/bin/python spikes/python_fork_cache.py
       cat /tmp/python_fork_cache.log
"""

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, TextBlock, query

sys.path.insert(0, str(Path(__file__).parent))
from cache_utils import extract_cache_stats, print_cache_report

MODEL = "claude-haiku-4-5-20251001"
LOG = Path("/tmp/python_fork_cache.log")
_log_lines: list[str] = []


def log(s: str = ""):
    _log_lines.append(s)


def flush_log():
    LOG.write_text("\n".join(_log_lines))


async def run_query(prompt: str, options: ClaudeAgentOptions) -> tuple[str | None, str]:
    session_id = None
    parts = []
    async for message in query(prompt=prompt, options=options):
        if hasattr(message, "session_id"):
            session_id = message.session_id
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return session_id, "\n".join(parts)


async def main():
    log("=" * 60)
    log("  SPIKE: Python Fork Cache Behavior")
    log("=" * 60)

    # --- Turn 1: long response to build context ---
    log("\n[1] Creating session — turn 1 (long response)...")
    flush_log()
    opts = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Answer concisely but thoroughly.",
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    session_id, text = await run_query(
        "Give me a detailed timeline of the Roman Empire from founding to fall. "
        "Include major emperors, battles, and cultural developments. "
        "Be thorough — aim for at least 2000 words.",
        opts,
    )
    log(f"  Session: {session_id}")
    log(f"  Response: {len(text)} chars")
    flush_log()

    # --- Turn 2: resume (should cache) ---
    log("\n[2] Turn 2 — resume (no fork)...")
    flush_log()
    opts2 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text2 = await run_query("Now summarize that in 3 bullet points.", opts2)
    log(f"  Response: {len(text2)} chars")
    flush_log()

    # --- Turn 3: more context ---
    log("\n[3] Turn 3 — resume (more context)...")
    flush_log()
    opts3 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text3 = await run_query(
        "Now compare the fall of Rome to modern geopolitical trends. "
        "Write at least 1000 words.",
        opts3,
    )
    log(f"  Response: {len(text3)} chars")
    flush_log()

    # --- Fork from end ---
    log("\n[4] Fork from END of session...")
    flush_log()
    fork_opts = ClaudeAgentOptions(
        resume=session_id,
        fork_session=True,
        model=MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    fork_id, fork_text = await run_query(
        "Based on our conversation, what was the single most important turning point?",
        fork_opts,
    )
    log(f"  Fork session: {fork_id}")
    log(f"  Response: {len(fork_text)} chars")
    flush_log()

    # --- Resume original (control) ---
    log("\n[5] Resume original (control — max cache expected)...")
    flush_log()
    opts5 = ClaudeAgentOptions(
        resume=session_id, model=MODEL, permission_mode="bypassPermissions", max_turns=1,
    )
    _, text5 = await run_query("What did we discuss? One sentence.", opts5)
    log(f"  Response: {len(text5)} chars")
    flush_log()

    # --- Reports ---
    # Redirect print_cache_report to our log
    import io, contextlib
    buf = io.StringIO()

    log("\n\n" + "#" * 60)
    log("  CACHE REPORTS")
    log("#" * 60)

    orig_stats = extract_cache_stats(session_id)
    with contextlib.redirect_stdout(buf):
        print_cache_report(f"Original session ({session_id[:12]}...)", orig_stats)
    log(buf.getvalue())
    buf.truncate(0); buf.seek(0)

    fork_stats = []
    if fork_id:
        fork_stats = extract_cache_stats(fork_id)
        with contextlib.redirect_stdout(buf):
            print_cache_report(f"Python fork ({fork_id[:12]}...)", fork_stats)
        log(buf.getvalue())
        buf.truncate(0); buf.seek(0)

    # Comparison
    if orig_stats and fork_stats:
        last_orig = orig_stats[-1]
        last_fork = fork_stats[-1]
        log(f"\n{'=' * 60}")
        log(f"  COMPARISON")
        log(f"{'=' * 60}")
        log(f"  {'Scenario':<30} {'Cache Read':>12} {'Total In':>12} {'Rate':>8}")
        log(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*8}")
        for i, s in enumerate(orig_stats):
            log(f"  {'Original turn ' + str(i+1):<30} {s['cache_read']:>12,} {s['total_input']:>12,} {s['cache_rate']:>7.1%}")
        log(f"  {'Fork (from end)':<30} {last_fork['cache_read']:>12,} {last_fork['total_input']:>12,} {last_fork['cache_rate']:>7.1%}")

    flush_log()
    # Also print to stderr so we see something
    import sys
    sys.stderr.write(f"\nDone. Results in {LOG}\n")


if __name__ == "__main__":
    asyncio.run(main())
