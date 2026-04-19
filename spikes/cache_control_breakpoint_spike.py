"""Spike: How do cache_control breakpoints affect prompt cache behavior?

Questions:
1. Is cache_control metadata part of the cache prefix hash?
   - If request A has cache_control on a block and B doesn't, do they share cache?
2. Does changing the NUMBER of cache_control blocks affect cache hits?
3. Does changing the POSITION of cache_control blocks affect cache for unchanged prefix?
4. Do breakpoints created by request A help request B if B lacks them?
5. How do forks (truncated prefix) interact with breakpoints?

Uses raw anthropic SDK (not CC) for precise control.
Model: claude-haiku-4-5 (cheap, fast, supports caching).

Usage:
    source /Users/breedoon/Documents/PATH/obs-venv/bin/activate
    python spikes/cache_control_breakpoint_spike.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import anthropic

# Minimum cacheable seems to be ~4096+ tokens for Haiku 4.5 based on testing.
# Use ~3000 token blocks to ensure we're well above threshold.
FILLER = "The quick brown fox jumps over the lazy dog. " * 500  # ~5000 tokens
SYSTEM_FILLER = "You are a helpful assistant. " + FILLER  # ~5000+ tokens
MSG_FILLER_A = "Here is document A for reference: " + FILLER  # ~5000+ tokens
MSG_FILLER_B = "Here is document B for reference: " + FILLER  # ~5000+ tokens
MSG_FILLER_C = "Here is document C for reference: " + FILLER  # ~5000+ tokens


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


def call(client: anthropic.Anthropic, label: str, body: dict) -> dict | None:
    try:
        r = client.messages.create(**body)
    except anthropic.APIStatusError as e:
        print(f"  [{label}] ERROR {e.status_code}: {e.message[:300]}")
        return None
    u = r.usage
    cr = u.cache_read_input_tokens
    cc = u.cache_creation_input_tokens
    inp = u.input_tokens
    total = cr + cc + inp
    rate = cr / total * 100 if total > 0 else 0
    print(f"  [{label}] total={total:>7,}  read={cr:>7,}  create={cc:>7,}  fresh={inp:>5}  rate={rate:.1f}%")
    return {"cr": cr, "cc": cc, "in": inp, "total": total, "rate": rate}


def pause(sec=3):
    print(f"    (waiting {sec}s for cache propagation)")
    time.sleep(sec)


# ── Experiment 1: Is cache_control part of the cache hash? ──────────────

def exp1_cache_control_in_hash(client):
    """Send identical content, vary only whether cache_control is present.

    Warm: system (with CC) + user msg (with CC) → creates cache
    Probe A: IDENTICAL → should hit (control)
    Probe B: same content but NO cache_control on system → hit or miss?
    Probe C: same content but NO cache_control on user msg → hit or miss?
    Probe D: same content but NO cache_control anywhere → hit or miss?
    """
    print("\n" + "=" * 70)
    print("  EXP 1: Is cache_control part of the cache prefix hash?")
    print("=" * 70)

    base_system = [
        {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}},
    ]
    base_system_no_cc = [
        {"type": "text", "text": SYSTEM_FILLER},
    ]
    base_msgs_cc = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": [
            {"type": "text", "text": "Reply: DONE.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    base_msgs_no_user_cc = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A},  # no cache_control
        ]},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": [
            {"type": "text", "text": "Reply: DONE.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    base_msgs_no_cc = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A},
        ]},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": [
            {"type": "text", "text": "Reply: DONE."},
        ]},
    ]

    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Warm
    call(client, "WARM (sys CC + user CC)", {**common, "system": base_system, "messages": base_msgs_cc})
    pause()

    # Control: identical request
    call(client, "CTRL identical", {**common, "system": base_system, "messages": base_msgs_cc})
    pause()

    # Probe B: no CC on system
    r_b = call(client, "NO CC on system", {**common, "system": base_system_no_cc, "messages": base_msgs_cc})
    pause()

    # Probe C: no CC on first user msg (but still on system)
    r_c = call(client, "NO CC on user1", {**common, "system": base_system, "messages": base_msgs_no_user_cc})
    pause()

    # Probe D: no CC anywhere
    r_d = call(client, "NO CC anywhere", {**common, "system": base_system_no_cc, "messages": base_msgs_no_cc})
    pause()

    print("\n  VERDICT:")
    if r_b and r_b["cr"] > 100:
        print("  → Removing CC from system: STILL HITS cache → CC not in system hash")
    elif r_b:
        print("  → Removing CC from system: MISSES cache → CC IS in system hash")
    if r_c and r_c["cr"] > 100:
        print("  → Removing CC from user1: STILL HITS cache → CC not in user msg hash")
    elif r_c:
        print("  → Removing CC from user1: MISSES cache → CC IS in user msg hash")
    if r_d and r_d["cr"] > 100:
        print("  → Removing ALL CC: STILL HITS → CC fully excluded from hash")
    elif r_d:
        print("  → Removing ALL CC: MISSES → CC affects hash somewhere")


# ── Experiment 2: Does the NUMBER of breakpoints matter? ────────────────

def exp2_breakpoint_count(client):
    """Same content, different number of cache_control blocks.

    Warm: 3 breakpoints (system + user1 + user2)
    Probe A: 1 breakpoint (system only)
    Probe B: 2 breakpoints (system + user2)
    Probe C: 4 breakpoints (system + user1 + user2 + user3/last)
    """
    print("\n" + "=" * 70)
    print("  EXP 2: Does the NUMBER of cache_control blocks affect cache?")
    print("=" * 70)

    sys_block = {"type": "text", "text": SYSTEM_FILLER}
    sys_block_cc = {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}}

    def make_msgs(cc_on_user1=False, cc_on_user2=False, cc_on_user3=False):
        u1 = {"type": "text", "text": MSG_FILLER_A}
        u2 = {"type": "text", "text": MSG_FILLER_B}
        u3 = {"type": "text", "text": "Reply: DONE."}
        if cc_on_user1:
            u1["cache_control"] = {"type": "ephemeral"}
        if cc_on_user2:
            u2["cache_control"] = {"type": "ephemeral"}
        if cc_on_user3:
            u3["cache_control"] = {"type": "ephemeral"}
        return [
            {"role": "user", "content": [u1]},
            {"role": "assistant", "content": "Noted A."},
            {"role": "user", "content": [u2]},
            {"role": "assistant", "content": "Noted B."},
            {"role": "user", "content": [u3]},
        ]

    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Warm: 3 breakpoints
    call(client, "WARM 3 BP (sys+u1+u2)", {
        **common,
        "system": [sys_block_cc],
        "messages": make_msgs(cc_on_user1=True, cc_on_user2=True),
    })
    pause()

    # Control
    call(client, "CTRL 3 BP identical", {
        **common,
        "system": [sys_block_cc],
        "messages": make_msgs(cc_on_user1=True, cc_on_user2=True),
    })
    pause()

    # 1 breakpoint (system only)
    call(client, "1 BP (sys only)", {
        **common,
        "system": [sys_block_cc],
        "messages": make_msgs(),
    })
    pause()

    # 2 breakpoints (system + user2)
    call(client, "2 BP (sys+u2)", {
        **common,
        "system": [sys_block_cc],
        "messages": make_msgs(cc_on_user2=True),
    })
    pause()

    # 4 breakpoints (max)
    call(client, "4 BP (sys+u1+u2+u3)", {
        **common,
        "system": [sys_block_cc],
        "messages": make_msgs(cc_on_user1=True, cc_on_user2=True, cc_on_user3=True),
    })
    pause()


# ── Experiment 3: Does POSITION of breakpoints affect prefix cache? ─────

def exp3_breakpoint_position(client):
    """Same content, breakpoints at different positions.

    Warm: CC on user1
    Probe: CC on user2 instead (user1 has no CC)
    Question: Does the prefix up to user1 still match?
    """
    print("\n" + "=" * 70)
    print("  EXP 3: Does POSITION of cache_control affect prefix matching?")
    print("=" * 70)

    sys_block = {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}}

    def make_msgs(cc_on_user1=False, cc_on_user2=False):
        u1 = {"type": "text", "text": MSG_FILLER_A}
        u2 = {"type": "text", "text": "Reply: DONE."}
        if cc_on_user1:
            u1["cache_control"] = {"type": "ephemeral"}
        if cc_on_user2:
            u2["cache_control"] = {"type": "ephemeral"}
        return [
            {"role": "user", "content": [u1]},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": [u2]},
        ]

    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Warm: CC on user1
    call(client, "WARM CC on user1", {
        **common, "system": [sys_block],
        "messages": make_msgs(cc_on_user1=True),
    })
    pause()

    # Control
    call(client, "CTRL identical", {
        **common, "system": [sys_block],
        "messages": make_msgs(cc_on_user1=True),
    })
    pause()

    # Probe: CC on user2 instead
    r = call(client, "CC on user2 (not user1)", {
        **common, "system": [sys_block],
        "messages": make_msgs(cc_on_user2=True),
    })
    pause()

    print("\n  VERDICT:")
    if r and r["cr"] > 100:
        print("  → Moving CC from user1→user2: STILL HITS → CC not in prefix hash (just breakpoint hint)")
    elif r:
        print("  → Moving CC from user1→user2: MISSES → CC IS in prefix hash (byte-level matching)")


# ── Experiment 4: Can request B read breakpoints created by request A? ──

def exp4_cross_request_breakpoints(client):
    """Request A creates a breakpoint at user1. Request B has NO breakpoints on user1.
    Does B benefit from A's cached segment at user1?

    This tests whether cached segments are keyed by content only (not by
    whether the requesting call had a breakpoint there).
    """
    print("\n" + "=" * 70)
    print("  EXP 4: Can request B read breakpoints it didn't create?")
    print("=" * 70)

    sys_block_cc = {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}}
    sys_block = {"type": "text", "text": SYSTEM_FILLER}

    # Request A: breakpoint at system + user1
    msgs_a = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": "Reply: DONE.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    # Request B: breakpoint only on last user msg (not on system or user1)
    msgs_b = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A},  # no cache_control!
        ]},
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": "Reply: DONE.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Warm with request A (creates breakpoints at system, user1, user2)
    call(client, "A: 3 breakpoints", {**common, "system": [sys_block_cc], "messages": msgs_a})
    pause()

    # Probe with request B (breakpoint only on user2, not on system/user1)
    r = call(client, "B: 1 breakpoint (last)", {**common, "system": [sys_block], "messages": msgs_b})

    print("\n  VERDICT:")
    if r and r["cr"] > 100:
        print(f"  → B reads {r['cr']:,} cached tokens despite lacking A's breakpoints")
        print("  → Cached segments are content-keyed, not breakpoint-keyed")
    elif r:
        print("  → B gets NO cache from A's breakpoints → breakpoints are request-specific")


# ── Experiment 5: Fork simulation (truncated prefix) + breakpoints ──────

def exp5_fork_with_breakpoints(client):
    """Simulate forking: parent has 5 turns, fork truncates to 3.

    Parent: system + user1 + asst1 + user2 + asst2 + user3 + asst3 + user4 + asst4 + user5
    Fork A: system + user1 + asst1 + user2 + asst2 + user3_new (new msg)
    Fork B: system + user1 + asst1 + user2_new (fork after turn 1)

    Each fork has cache_control at the same positions as the parent for the
    shared prefix, then different positions for its own tail.
    """
    print("\n" + "=" * 70)
    print("  EXP 5: Fork simulation — truncated prefix + breakpoints")
    print("=" * 70)

    sys_block = {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}}
    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Parent: 4 user messages, CC on last 3 (user2, user3, user4)
    parent_msgs = [
        {"role": "user", "content": [{"type": "text", "text": MSG_FILLER_A}]},
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_B, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted B."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_C, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted C."},
        {"role": "user", "content": [
            {"type": "text", "text": "Summarize all.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    # Fork A: after turn 2 (user1 + asst1 + user2 + asst2 + new_user)
    # The fork's "last 3 user messages" are user1, user2, new_user
    # So CC would be on user1, user2, new_user — but parent had CC on user2, user3, user4
    # Shared prefix: user1 (no CC in parent, CC in fork) and user2 (CC in both)
    fork_a_msgs_matching_cc = [
        {"role": "user", "content": [{"type": "text", "text": MSG_FILLER_A}]},  # no CC (matches parent)
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_B, "cache_control": {"type": "ephemeral"}},  # CC (matches parent)
        ]},
        {"role": "assistant", "content": "Noted B."},
        {"role": "user", "content": [
            {"type": "text", "text": "Different question.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    # Fork A variant: CC on ALL user msgs (proxy-style: last 3 = all 3)
    fork_a_msgs_proxy_cc = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},  # CC (NOT in parent!)
        ]},
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_B, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted B."},
        {"role": "user", "content": [
            {"type": "text", "text": "Different question.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    # Fork B: after turn 1 (user1 + asst1 + new_user)
    fork_b_msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted A."},
        {"role": "user", "content": [
            {"type": "text", "text": "New direction.", "cache_control": {"type": "ephemeral"}},
        ]},
    ]

    print("\n  --- Parent conversation (4 user msgs, CC on last 3) ---")
    call(client, "PARENT", {**common, "system": [sys_block], "messages": parent_msgs})
    pause()

    print("\n  --- Fork A: from turn 2, matching CC positions ---")
    call(client, "FORK-A (match CC)", {**common, "system": [sys_block], "messages": fork_a_msgs_matching_cc})
    pause()

    print("\n  --- Fork A: from turn 2, proxy-style CC (all user msgs) ---")
    call(client, "FORK-A (proxy CC)", {**common, "system": [sys_block], "messages": fork_a_msgs_proxy_cc})
    pause()

    print("\n  --- Fork B: from turn 1 (shorter prefix) ---")
    call(client, "FORK-B (turn1)", {**common, "system": [sys_block], "messages": fork_b_msgs})
    pause()


# ── Experiment 6: Progressive turns — does removing old CC break cache? ──

def exp6_progressive_turns(client):
    """Simulate what happens across turns when CC markers shift.

    Turn 1: system(CC) + user1(CC)
    Turn 2: system(CC) + user1(NO CC) + asst1 + user2(CC)
    Turn 3: system(CC) + user1(NO CC) + asst1 + user2(NO CC) + asst2 + user3(CC)

    Q: Does turn 2 hit cache for system+user1 prefix even though user1
       lost its CC marker?

    Then test with CC kept on all user msgs (proxy approach):
    Turn 2': system(CC) + user1(CC) + asst1 + user2(CC)
    """
    print("\n" + "=" * 70)
    print("  EXP 6: Progressive turns — CC marker removal between turns")
    print("=" * 70)

    sys_cc = {"type": "text", "text": SYSTEM_FILLER, "cache_control": {"type": "ephemeral"}}
    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    # Turn 1
    t1_msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    print("\n  --- Turn 1: sys(CC) + user1(CC) ---")
    call(client, "T1", {**common, "system": [sys_cc], "messages": t1_msgs})
    pause()

    # Turn 2 CC-style: CC removed from user1 (how CC normally works)
    t2_cc_style = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A},  # NO CC (CC removed by CC)
        ]},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_B, "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    print("\n  --- Turn 2 (CC-style): user1 LOSES CC ---")
    r_cc = call(client, "T2 (CC-style, u1 no CC)", {**common, "system": [sys_cc], "messages": t2_cc_style})
    pause()

    # Turn 2 proxy-style: CC kept on user1
    t2_proxy_style = [
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_A, "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": [
            {"type": "text", "text": MSG_FILLER_B, "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    print("\n  --- Turn 2 (proxy-style): user1 KEEPS CC ---")
    r_proxy = call(client, "T2 (proxy, u1 keeps CC)", {**common, "system": [sys_cc], "messages": t2_proxy_style})
    pause()

    print("\n  VERDICT:")
    if r_cc and r_proxy:
        print(f"  CC-style (u1 loses CC):  read={r_cc['cr']:,}")
        print(f"  Proxy-style (u1 keeps CC): read={r_proxy['cr']:,}")
        if r_cc["cr"] > 100 and r_proxy["cr"] > 100:
            print("  → BOTH hit cache → CC metadata not in prefix hash")
        elif r_proxy["cr"] > 100 and r_cc["cr"] < 100:
            print("  → Only proxy hits → CC IS in hash, keeping CC prevents miss")
        elif r_cc["cr"] > 100 and r_proxy["cr"] < 100:
            print("  → Only CC-style hits → adding CC breaks hash??")
        else:
            print("  → Neither hits → cache expired or other issue")


# ── Experiment 7: Maximum breakpoints and the 4-block limit ─────────────

def exp7_max_breakpoints(client):
    """Test the 4-block limit explicitly.

    3 blocks: should work
    4 blocks: should work (max)
    5 blocks: should fail with the error the user saw
    """
    print("\n" + "=" * 70)
    print("  EXP 7: Maximum cache_control blocks (verify 4-block limit)")
    print("=" * 70)

    common = {"model": "claude-haiku-4-5", "max_tokens": 10}

    def make_body(n_system_cc=1, n_user_cc=0):
        system = [
            {"type": "text", "text": SYSTEM_FILLER + f" block {i}",
             **({"cache_control": {"type": "ephemeral"}} if i < n_system_cc else {})}
            for i in range(max(1, n_system_cc))
        ]
        # If n_system_cc is 0, no CC on system
        if n_system_cc == 0:
            system = [{"type": "text", "text": SYSTEM_FILLER}]

        msgs = []
        for j in range(max(n_user_cc, 1)):
            user_block = {"type": "text", "text": f"User message {j}: " + FILLER[:200]}
            if j < n_user_cc:
                user_block["cache_control"] = {"type": "ephemeral"}
            msgs.append({"role": "user", "content": [user_block]})
            if j < max(n_user_cc, 1) - 1:
                msgs.append({"role": "assistant", "content": f"Ack {j}."})

        return {**common, "system": system, "messages": msgs}

    # 1 system + 2 user = 3 total
    print("\n  --- 3 CC blocks (1 sys + 2 user) ---")
    call(client, "3 blocks", make_body(n_system_cc=1, n_user_cc=2))
    pause(2)

    # 1 system + 3 user = 4 total
    print("\n  --- 4 CC blocks (1 sys + 3 user) ---")
    call(client, "4 blocks", make_body(n_system_cc=1, n_user_cc=3))
    pause(2)

    # 1 system + 4 user = 5 total → should error
    print("\n  --- 5 CC blocks (1 sys + 4 user) → expect error ---")
    call(client, "5 blocks", make_body(n_system_cc=1, n_user_cc=4))
    pause(2)

    # 2 system + 2 user = 4 total
    print("\n  --- 4 CC blocks (2 sys + 2 user) ---")
    call(client, "4 blocks (2+2)", make_body(n_system_cc=2, n_user_cc=2))
    pause(2)

    # 0 system + 4 user = 4 total
    print("\n  --- 4 CC blocks (0 sys + 4 user) ---")
    call(client, "4 blocks (0+4)", make_body(n_system_cc=0, n_user_cc=4))


def main():
    client = make_client()

    exp1_cache_control_in_hash(client)
    exp2_breakpoint_count(client)
    exp3_breakpoint_position(client)
    exp4_cross_request_breakpoints(client)
    exp5_fork_with_breakpoints(client)
    exp6_progressive_turns(client)
    exp7_max_breakpoints(client)

    print("\n\n" + "=" * 70)
    print("  ALL EXPERIMENTS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
