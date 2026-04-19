"""Spike: Does fork cache work WITHOUT touching cache_control?

Tests whether CC's native cache_control placement is sufficient for fork
cache hits, using raw anthropic SDK to simulate exactly what CC does.

Key questions:
1. Fork from end of turn (where CC placed cache_control) → cache hit?
2. Fork from middle of conversation (no cache_control at that point) → cache hit?
3. Fork of a fork → cache hit?
4. What's the adjusted cache hit rate (shared prefix read / shared prefix total)?

Uses ~16K tokens per message block to create clear differentiation between
system cache and conversation cache. Total conversation ~80K tokens across 4 turns.

IMPORTANT: No cache_control manipulation. We place CC exactly where CC would:
- 1 on system prompt (last system block)
- 1 on last user message (last content block)
That's it. Same 2 breakpoints CC uses natively.

Usage:
    cd ~/Documents/obs && source .venv/bin/activate
    python spikes/cache_control_passthrough_spike.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import anthropic

# ~16K tokens per block (big enough for clear differentiation)
# "The quick brown fox..." is ~10 tokens per repetition
CHUNK = "The quick brown fox jumps over the lazy dog. " * 1600  # ~16K tokens

# Each piece has unique prefix so content differs between messages
SYSTEM_TEXT = "You are a research assistant analyzing documents. " + CHUNK
DOC_A = "DOCUMENT-A: Analysis of market trends in renewable energy. " + CHUNK
DOC_B = "DOCUMENT-B: Historical overview of semiconductor fabrication. " + CHUNK
DOC_C = "DOCUMENT-C: Comparative study of distributed systems architectures. " + CHUNK
DOC_D = "DOCUMENT-D: Survey of natural language processing techniques. " + CHUNK


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


def call(client, label, system, messages, expect_read=None):
    """Make an API call and report cache metrics.

    Returns dict with cr, cc, fresh, total, and adjusted_rate.
    adjusted_rate = cache_read / (cache_read + cache_creation), excluding fresh input tokens.
    This measures: of the prefix content, how much was from cache?
    """
    try:
        r = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=5,
            system=system,
            messages=messages,
        )
    except anthropic.APIStatusError as e:
        print(f"  [{label}] ERROR {e.status_code}: {e.message[:300]}")
        return None
    u = r.usage
    cr = u.cache_read_input_tokens
    cc = u.cache_creation_input_tokens
    inp = u.input_tokens
    total = cr + cc + inp
    # Adjusted rate: of the cacheable prefix (cr + cc), how much was read?
    # This excludes fresh input tokens (typically just the final user prompt overhead)
    prefix_total = cr + cc
    adjusted = cr / prefix_total * 100 if prefix_total > 0 else 0
    naive = cr / total * 100 if total > 0 else 0

    status = ""
    if expect_read is not None:
        if abs(cr - expect_read) < 500:  # within 500 tokens tolerance
            status = " ✓"
        else:
            status = f" ✗ (expected ~{expect_read:,} read)"

    print(f"  [{label}] total={total:>7,}  read={cr:>7,}  create={cc:>7,}  fresh={inp:>5}"
          f"  adjusted={adjusted:.1f}%  naive={naive:.1f}%{status}")
    return {"cr": cr, "cc": cc, "fresh": inp, "total": total, "adjusted": adjusted, "naive": naive}


def pause(sec=4):
    """Wait for cache propagation."""
    time.sleep(sec)


def main():
    client = make_client()

    # CC-style cache_control: only on last system block + last user message block
    # This is exactly what CC does natively.
    def sys_blocks():
        """System with CC on last block (CC's native behavior)."""
        return [
            {"type": "text", "text": SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}},
        ]

    def user_msg(text, is_last=False):
        """User message. cache_control only if this is the last user message."""
        block = {"type": "text", "text": text}
        if is_last:
            block["cache_control"] = {"type": "ephemeral"}
        return {"role": "user", "content": [block]}

    def asst_msg(text):
        return {"role": "assistant", "content": text}

    # ================================================================
    # BUILD THE PARENT CONVERSATION (4 turns, ~80K tokens)
    # ================================================================
    # Turn 1: system + user1(DOC_A)
    # Turn 2: + asst1 + user2(DOC_B)
    # Turn 3: + asst2 + user3(DOC_C)
    # Turn 4: + asst3 + user4(DOC_D)
    #
    # CC places cache_control on the LAST user message only.
    # On each turn, previous user messages lose their cache_control.

    print("=" * 75)
    print("  PARENT CONVERSATION BUILD (4 turns, CC-native cache_control)")
    print("=" * 75)

    # Turn 1: system + user1
    t1_msgs = [user_msg(DOC_A, is_last=True)]
    print("\n  --- Turn 1: sys + user1 (~32K tokens) ---")
    r1 = call(client, "T1", sys_blocks(), t1_msgs)
    pause()

    # Turn 2: user1 loses CC, user2 gets CC
    t2_msgs = [
        user_msg(DOC_A, is_last=False),  # CC removed (CC's behavior)
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=True),    # CC added (last user msg)
    ]
    print("\n  --- Turn 2: +asst1+user2 (~48K tokens) ---")
    r2 = call(client, "T2", sys_blocks(), t2_msgs)
    pause()

    # Turn 3: user2 loses CC, user3 gets CC
    t3_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=False),   # CC removed
        asst_msg("Document B acknowledged."),
        user_msg(DOC_C, is_last=True),    # CC added
    ]
    print("\n  --- Turn 3: +asst2+user3 (~64K tokens) ---")
    r3 = call(client, "T3", sys_blocks(), t3_msgs)
    pause()

    # Turn 4: user3 loses CC, user4 gets CC
    t4_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=False),
        asst_msg("Document B acknowledged."),
        user_msg(DOC_C, is_last=False),   # CC removed
        asst_msg("Document C acknowledged."),
        user_msg(DOC_D, is_last=True),    # CC added
    ]
    print("\n  --- Turn 4: +asst3+user4 (~80K tokens) ---")
    r4 = call(client, "T4", sys_blocks(), t4_msgs)
    pause()

    if not r4:
        print("ABORT — turn 4 failed")
        return

    # ================================================================
    # FORK TESTS
    # ================================================================
    print("\n" + "=" * 75)
    print("  FORK TESTS (no cache_control changes — CC-native only)")
    print("=" * 75)

    # Sizes for expected values (approximate)
    sys_size = r1["cr"] + r1["cc"] if r1 else 16000  # system prefix
    t1_size = r1["total"] if r1 else 32000            # system + user1
    t2_size = r2["total"] if r2 else 48000            # through turn 2
    t3_size = r3["total"] if r3 else 64000            # through turn 3
    t4_size = r4["total"] if r4 else 80000            # through turn 4

    # --- Fork 1: From END of turn 4 (where CC placed cache_control) ---
    # This is the normal fork case. The parent's last request had CC on user4.
    # The fork replaces user4 with a new message.
    fork1_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=False),
        asst_msg("Document B acknowledged."),
        user_msg(DOC_C, is_last=False),
        asst_msg("Document C acknowledged."),
        user_msg("Summarize all three documents briefly.", is_last=True),  # NEW
    ]
    print("\n  --- Fork 1: from END of turn 4 (last user msg replaced) ---")
    print("  Expected: read ≈ turn 3 prefix (~64K), create ≈ new msg")
    f1 = call(client, "FORK-END", sys_blocks(), fork1_msgs, expect_read=t3_size)
    pause()

    # --- Fork 2: From END of turn 2 (mid-conversation fork) ---
    # Shared prefix: system + user1 + asst1 + user2
    # The fork's user2 is replaced with new content.
    fork2_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg("What are the key findings from document A?", is_last=True),  # NEW
    ]
    print("\n  --- Fork 2: from END of turn 2 (after user2) ---")
    print("  Expected: read ≈ turn 1 prefix (~32K), create ≈ new msg")
    f2 = call(client, "FORK-T2", sys_blocks(), fork2_msgs, expect_read=t1_size)
    pause()

    # --- Fork 3: From MIDDLE of turn 3 (after asst2, before user3) ---
    # This is the tricky case: no cache_control was ever placed here by CC.
    # Shared prefix: system + user1 + asst1 + user2 + asst2
    # New user message replaces user3.
    fork3_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=False),
        asst_msg("Document B acknowledged."),
        user_msg("Compare documents A and B.", is_last=True),  # NEW
    ]
    print("\n  --- Fork 3: from MIDDLE (after asst2, replacing user3) ---")
    print("  Expected: read ≈ turn 2 prefix (~48K) IF lookback reaches it")
    print("  This position never had cache_control in the parent!")
    f3 = call(client, "FORK-MID", sys_blocks(), fork3_msgs, expect_read=t2_size)
    pause()

    # --- Fork 4: Fork of Fork 1 (fork of a fork) ---
    # Fork1 created cache entries. Can a fork of fork1 read them?
    # Shared prefix with fork1: system + user1 + asst1 + user2 + asst2 + user3 + asst3
    # New message replaces fork1's "summarize" message.
    fork4_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg(DOC_B, is_last=False),
        asst_msg("Document B acknowledged."),
        user_msg(DOC_C, is_last=False),
        asst_msg("Document C acknowledged."),
        user_msg("What is the most surprising finding across all documents?", is_last=True),  # NEW
    ]
    print("\n  --- Fork 4: fork of fork1 (fork-of-fork) ---")
    print("  Expected: read ≈ turn 3 prefix (~64K)")
    f4 = call(client, "FORK-OF-FORK", sys_blocks(), fork4_msgs, expect_read=t3_size)
    pause()

    # --- Fork 5: From turn 1 (earliest possible fork) ---
    # Shared prefix: system + user1 only (no assistant response)
    # Actually: shared prefix is system only, because user1 is replaced.
    # Wait no — if we keep user1 and add a new user2, the shared prefix is system + user1 + asst1.
    # But if we replace user1's content, shared prefix is just system.
    # Let's keep user1 (DOC_A) and replace the rest.
    fork5_msgs = [
        user_msg(DOC_A, is_last=False),
        asst_msg("Document A acknowledged."),
        user_msg("Tell me more about the energy trends.", is_last=True),  # NEW
    ]
    print("\n  --- Fork 5: from turn 1 (keep user1+asst1, new user2) ---")
    print("  Expected: read ≈ turn 1 prefix (~32K)")
    f5 = call(client, "FORK-T1", sys_blocks(), fork5_msgs, expect_read=t1_size)

    # ================================================================
    # ANALYSIS
    # ================================================================
    print("\n\n" + "=" * 75)
    print("  ANALYSIS")
    print("=" * 75)

    results = [
        ("Parent T1 (build)", r1, "N/A (first turn)"),
        ("Parent T2 (build)", r2, "N/A (continuation)"),
        ("Parent T3 (build)", r3, "N/A (continuation)"),
        ("Parent T4 (build)", r4, "N/A (continuation)"),
        ("Fork from END t4", f1, f"shared≈{t3_size:,}"),
        ("Fork from END t2", f2, f"shared≈{t1_size:,}"),
        ("Fork from MID (no CC there)", f3, f"shared≈{t2_size:,}"),
        ("Fork of fork", f4, f"shared≈{t3_size:,}"),
        ("Fork from t1", f5, f"shared≈{t1_size:,}"),
    ]

    print(f"\n  {'Label':<30} {'Total':>7} {'Read':>7} {'Create':>7} {'Fresh':>5} {'Adj%':>6} {'Note'}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*5} {'-'*6} {'-'*20}")
    for label, r, note in results:
        if r:
            print(f"  {label:<30} {r['total']:>7,} {r['cr']:>7,} {r['cc']:>7,} {r['fresh']:>5} {r['adjusted']:>5.1f}% {note}")
        else:
            print(f"  {label:<30} {'FAILED':>7}")

    # Verdict
    print("\n  VERDICTS:")
    all_forks = [("END t4", f1), ("END t2", f2), ("MID (no CC)", f3), ("fork-of-fork", f4), ("t1", f5)]
    all_pass = True
    for label, f in all_forks:
        if f and f["adjusted"] > 95:
            print(f"  ✓ Fork {label}: {f['adjusted']:.1f}% adjusted → CC-native cache_control works")
        elif f:
            print(f"  ? Fork {label}: {f['adjusted']:.1f}% adjusted → needs investigation")
            all_pass = False
        else:
            print(f"  ✗ Fork {label}: FAILED")
            all_pass = False

    if all_pass:
        print("\n  CONCLUSION: CC-native cache_control is sufficient.")
        print("  The proxy does NOT need to manage cache_control (Rule 5 can be removed).")
    else:
        print("\n  CONCLUSION: Some forks didn't hit cache — investigate further.")


if __name__ == "__main__":
    main()
