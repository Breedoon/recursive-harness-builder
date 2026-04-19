"""Spike: Does the 20-block lookback limit actually bite us?

Test 1: 30+ blocks, CC-native breakpoints (system + last user).
  Fork from middle — can the fork's breakpoint reach system cache 30 blocks back?

Test 2: Simulate what CC actually does per-tool-cycle — breakpoints every 2-3 blocks.
  Fork from middle — the chain of entries means lookback always finds something nearby.

Test 3: Explicitly test the boundary — exactly 19, 20, 21, 25 blocks between
  the fork's breakpoint and the nearest cached entry.

Uses raw anthropic SDK. No cache_control manipulation beyond what we're testing.

Usage:
    cd ~/Documents/obs && source .venv/bin/activate
    python spikes/cache_control_lookback_limit_spike.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import anthropic

# ~5K tokens per block, enough for caching but not so big we blow limits
CHUNK = "The quick brown fox jumps over the lazy dog. " * 500

def get_oauth_token():
    out = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        text=True,
    )
    return json.loads(out)["claudeAiOauth"]["accessToken"]


def make_client():
    token = get_oauth_token()
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={
            "anthropic-beta": "oauth-2025-04-20,prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
        },
    )


def call(client, label, system, messages):
    try:
        r = client.messages.create(
            model="claude-haiku-4-5", max_tokens=5,
            system=system, messages=messages,
        )
    except anthropic.APIStatusError as e:
        print(f"  [{label}] ERROR {e.status_code}: {e.message[:300]}")
        return None
    u = r.usage
    cr = u.cache_read_input_tokens
    cc = u.cache_creation_input_tokens
    inp = u.input_tokens
    total = cr + cc + inp
    prefix = cr + cc
    adj = cr / prefix * 100 if prefix > 0 else 0
    print(f"  [{label}] total={total:>7,}  read={cr:>7,}  create={cc:>7,}  fresh={inp:>5}  adj={adj:.1f}%")
    return {"cr": cr, "cc": cc, "fresh": inp, "total": total, "adj": adj}


def pause(sec=4):
    time.sleep(sec)


def test1_many_blocks_no_intermediate_breakpoints(client):
    """30+ content blocks. Breakpoints ONLY on system + last user msg.

    Build: system(CC) + 15 user/asst pairs + final user(CC) = 32 blocks.
    No intermediate breakpoints.

    Then fork from the middle (after turn 7, ~16 blocks in).
    Fork's breakpoint is on its last user msg (~16 blocks).
    Nearest cached entry: system at block ~1 (15 blocks back) — within 20.

    Then fork from near the end (after turn 13, ~28 blocks in).
    Fork's breakpoint ~28 blocks. System at block 1 = 27 blocks back — BEYOND 20!
    """
    print("\n" + "=" * 75)
    print("  TEST 1: Many blocks, no intermediate breakpoints")
    print("  Can lookback reach system cache from 15, 20, 27 blocks away?")
    print("=" * 75)

    sys_block = [{"type": "text", "text": "System: " + CHUNK, "cache_control": {"type": "ephemeral"}}]

    # Build parent with 15 turns (30 message blocks + system = ~31 blocks)
    # CC-style: only last user msg gets cache_control
    parent_msgs = []
    for i in range(15):
        is_last = (i == 14)
        user_block = {"type": "text", "text": f"Turn {i+1}: " + CHUNK[:200]}  # small to save cost
        if is_last:
            user_block["cache_control"] = {"type": "ephemeral"}
        parent_msgs.append({"role": "user", "content": [user_block]})
        if not is_last:
            parent_msgs.append({"role": "assistant", "content": f"Ack turn {i+1}."})

    print(f"\n  Parent: {len(parent_msgs)} messages ({len(parent_msgs) + 1} blocks incl system)")
    call(client, "PARENT (15 turns)", sys_block, parent_msgs)
    pause()

    # Fork from turn 7 (~15 blocks from system)
    fork_7 = []
    for i in range(7):
        fork_7.append({"role": "user", "content": [{"type": "text", "text": f"Turn {i+1}: " + CHUNK[:200]}]})
        fork_7.append({"role": "assistant", "content": f"Ack turn {i+1}."})
    fork_7.append({"role": "user", "content": [
        {"type": "text", "text": "New question after turn 7.", "cache_control": {"type": "ephemeral"}}
    ]})
    block_count_7 = len(fork_7) + 1  # +1 for system
    print(f"\n  Fork@7: {block_count_7} blocks. Lookback from last = {block_count_7-1} blocks to system")
    call(client, f"FORK@7 ({block_count_7-1} blocks back)", sys_block, fork_7)
    pause()

    # Fork from turn 10 (~21 blocks from system)
    fork_10 = []
    for i in range(10):
        fork_10.append({"role": "user", "content": [{"type": "text", "text": f"Turn {i+1}: " + CHUNK[:200]}]})
        fork_10.append({"role": "assistant", "content": f"Ack turn {i+1}."})
    fork_10.append({"role": "user", "content": [
        {"type": "text", "text": "New question after turn 10.", "cache_control": {"type": "ephemeral"}}
    ]})
    block_count_10 = len(fork_10) + 1
    print(f"\n  Fork@10: {block_count_10} blocks. Lookback from last = {block_count_10-1} blocks to system")
    call(client, f"FORK@10 ({block_count_10-1} blocks back)", sys_block, fork_10)
    pause()

    # Fork from turn 13 (~27 blocks from system)
    fork_13 = []
    for i in range(13):
        fork_13.append({"role": "user", "content": [{"type": "text", "text": f"Turn {i+1}: " + CHUNK[:200]}]})
        fork_13.append({"role": "assistant", "content": f"Ack turn {i+1}."})
    fork_13.append({"role": "user", "content": [
        {"type": "text", "text": "New question after turn 13.", "cache_control": {"type": "ephemeral"}}
    ]})
    block_count_13 = len(fork_13) + 1
    print(f"\n  Fork@13: {block_count_13} blocks. Lookback from last = {block_count_13-1} blocks to system")
    call(client, f"FORK@13 ({block_count_13-1} blocks back)", sys_block, fork_13)
    pause()


def test2_tool_result_simulation(client):
    """Simulate CC's agentic flow: tool_result blocks with cache_control.

    In a real CC session, each tool cycle adds:
    - assistant: tool_use (1 block)
    - user: tool_result (1 block, with cache_control on last cycle's result)

    We simulate 8 tool cycles, with cache_control on the last tool_result
    of each API request (CC sends one request per tool cycle).
    """
    print("\n" + "=" * 75)
    print("  TEST 2: Simulated tool_result blocks (agentic flow)")
    print("=" * 75)

    sys_block = [{"type": "text", "text": "System assistant: " + CHUNK, "cache_control": {"type": "ephemeral"}}]

    # Build a conversation that looks like an agentic session:
    # user text → asst tool_use → user tool_result → asst tool_use → user tool_result → ... → asst text
    #
    # In CC's flow, each API request includes all history + the latest tool_result.
    # CC places cache_control on the latest tool_result.
    #
    # We simulate the FINAL state (all tool cycles done), with cache_control
    # only on the very last user-role message (which is a tool_result).

    parent_msgs = [
        {"role": "user", "content": [{"type": "text", "text": "Read files A through H and summarize them."}]},
    ]
    # 8 tool cycles
    for i in range(8):
        tool_id = f"toolu_{i:04d}"
        # Assistant uses a tool
        parent_msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": f"/tmp/file_{chr(65+i)}.txt"}}
        ]})
        # Tool result comes back
        is_last_tool = (i == 7)
        result_block = {"type": "tool_result", "tool_use_id": tool_id,
                        "content": f"File {chr(65+i)} contents: " + CHUNK[:300]}
        if is_last_tool:
            result_block["cache_control"] = {"type": "ephemeral"}
        parent_msgs.append({"role": "user", "content": [result_block]})

    # Final user message
    parent_msgs.append({"role": "assistant", "content": "I've read all 8 files."})
    parent_msgs.append({"role": "user", "content": [
        {"type": "text", "text": "Now summarize.", "cache_control": {"type": "ephemeral"}}
    ]})

    msg_count = len(parent_msgs)
    print(f"\n  Parent: {msg_count} messages (user text + 8 tool cycles + final user)")
    call(client, "PARENT (8 tools)", sys_block, parent_msgs)
    pause()

    # Fork after tool cycle 4 (mid-tool-chain)
    # Shared: user text + 4 tool cycles = 1 + 8 = 9 messages
    fork_msgs = parent_msgs[:9]  # user + 4 tool cycles
    # Add new user message
    fork_msgs.append({"role": "assistant", "content": "I've read 4 files so far."})
    fork_msgs.append({"role": "user", "content": [
        {"type": "text", "text": "Stop, just summarize what you have.", "cache_control": {"type": "ephemeral"}}
    ]})

    print(f"\n  Fork after 4 tools: {len(fork_msgs)} messages")
    call(client, "FORK@4tools", sys_block, fork_msgs)
    pause()


def test3_exact_lookback_boundary(client):
    """Precisely test the 20-block lookback boundary.

    Create a cached entry at position 1 (system).
    Then fork with N intermediate blocks (no cache_control on any of them).
    The fork's breakpoint is at position N+2.
    Lookback goes back 20 positions from N+2.
    If N+2 - 1 > 20, system is out of range.

    Test with N = 17 (19 blocks to system — IN range)
                N = 18 (20 blocks to system — edge)
                N = 19 (21 blocks to system — OUT of range?)
                N = 23 (25 blocks to system — definitely out)
    """
    print("\n" + "=" * 75)
    print("  TEST 3: Exact 20-block lookback boundary")
    print("  System cached at block 1. Fork breakpoint at block N+2.")
    print("  Does lookback find system when it's 19/20/21/25 blocks back?")
    print("=" * 75)

    sys_block = [{"type": "text", "text": "System boundary test: " + CHUNK, "cache_control": {"type": "ephemeral"}}]

    # First, warm the system cache
    warm_msgs = [{"role": "user", "content": [
        {"type": "text", "text": "Hello.", "cache_control": {"type": "ephemeral"}}
    ]}]
    print("\n  Warming system cache...")
    call(client, "WARM", sys_block, warm_msgs)
    pause()

    for n_pairs in [8, 9, 10, 12]:
        # n_pairs user/asst pairs = 2*n_pairs message blocks
        # + system (1 block) + final user (1 block) = 2*n_pairs + 2 total blocks
        # Distance from final user to system = 2*n_pairs + 1
        msgs = []
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": f"Intermediate {i+1}."}  # NO cache_control
            ]})
            msgs.append({"role": "assistant", "content": f"Ack {i+1}."})
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": "Final question.", "cache_control": {"type": "ephemeral"}}
        ]})

        distance = 2 * n_pairs + 1  # blocks between final user and system
        total_blocks = 2 * n_pairs + 2  # including system and final user
        print(f"\n  {n_pairs} pairs → {total_blocks} blocks, {distance} blocks from system to breakpoint")
        call(client, f"{distance} blocks back", sys_block, msgs)
        pause()


def main():
    client = make_client()
    test1_many_blocks_no_intermediate_breakpoints(client)
    test2_tool_result_simulation(client)
    test3_exact_lookback_boundary(client)

    print("\n\n" + "=" * 75)
    print("  ALL TESTS COMPLETE")
    print("=" * 75)


if __name__ == "__main__":
    main()
