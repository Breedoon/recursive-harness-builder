"""
Inflate an existing opus session JSONL to ~850K tokens by duplicating
user/assistant message pairs with correct parent UUID chain.

Then resume it with ONE message to verify opus[1m] handles it.
"""
import json
import uuid
import sys
import os
from pathlib import Path
from copy import deepcopy

SOURCE_SESSION = "d79e308b-9c64-4614-9a09-1bbe4557fdc1"
PROJ_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
SOURCE_FILE = PROJ_DIR / f"{SOURCE_SESSION}.jsonl"

# Target: ~850K tokens. Source is ~476K at 80 turns.
# Each turn adds ~5,900 tokens. Need ~(850K - 476K) / 5900 = ~63 more turns.
# We'll duplicate the first 65 user/assistant pairs.

TARGET_TOKENS = 850_000
TOKENS_PER_TURN = 5_900  # observed from source session


def inflate():
    with open(SOURCE_FILE) as f:
        lines = [json.loads(l) for l in f]

    # Separate conversation messages from queue-operations
    conv_msgs = [l for l in lines if l.get('type') in ('user',) or l.get('message', {}).get('role') in ('user', 'assistant')]
    queue_ops = [l for l in lines if l.get('type') == 'queue-operation']

    # Get user/assistant pairs
    pairs = []
    i = 0
    while i < len(conv_msgs) - 1:
        user_msg = conv_msgs[i]
        asst_msg = conv_msgs[i + 1]
        if (user_msg.get('type') == 'user' and
            asst_msg.get('message', {}).get('role') == 'assistant'):
            pairs.append((user_msg, asst_msg))
            i += 2
        else:
            i += 1

    print(f"Source session: {SOURCE_SESSION}")
    print(f"  {len(lines)} lines, {len(pairs)} conversation pairs")
    print(f"  ~{len(pairs) * TOKENS_PER_TURN:,} estimated tokens")

    current_tokens = len(pairs) * TOKENS_PER_TURN
    extra_turns_needed = (TARGET_TOKENS - current_tokens) // TOKENS_PER_TURN + 1
    print(f"\nNeed ~{extra_turns_needed} extra turns to reach {TARGET_TOKENS:,} tokens")

    # Template: use the first pair as a template for duplication
    template_user = pairs[0][0]
    template_asst = pairs[0][1]

    # Find the last message's UUID to continue the chain
    last_uuid = pairs[-1][1].get('uuid')
    print(f"Last existing UUID: {last_uuid[:20]}...")

    # Create new session ID
    new_session_id = str(uuid.uuid4())
    new_file = PROJ_DIR / f"{new_session_id}.jsonl"

    # Build the new JSONL: copy all existing lines, then append duplicated pairs
    new_lines = []

    # Copy existing lines, updating session ID
    for l in lines:
        l2 = deepcopy(l)
        if 'sessionId' in l2:
            l2['sessionId'] = new_session_id
        if 'session_id' in l2:
            l2['session_id'] = new_session_id
        new_lines.append(json.dumps(l2))

    # Append new pairs
    prev_uuid = last_uuid
    turn_base = len(pairs) + 1

    for i in range(extra_turns_needed):
        turn_num = turn_base + i

        # New user message
        new_user = deepcopy(template_user)
        new_user['sessionId'] = new_session_id
        new_user['uuid'] = str(uuid.uuid4())
        new_user['parentUuid'] = prev_uuid
        new_user['message']['content'] = (
            f"[{turn_num}] OK {turn_num}. Section {turn_num}:\n"
            + template_user['message']['content'].split('\n', 1)[1]  # keep the padding
        )

        # Queue op before user (dequeue)
        q1 = {"type": "queue-operation", "operation": "dequeue",
               "timestamp": new_user.get('timestamp', ''), "sessionId": new_session_id}
        new_lines.append(json.dumps(q1))

        new_lines.append(json.dumps(new_user))

        # New assistant message
        new_asst = deepcopy(template_asst)
        new_asst['sessionId'] = new_session_id
        new_asst['uuid'] = str(uuid.uuid4())
        new_asst['parentUuid'] = new_user['uuid']
        new_asst['message']['content'] = [{"type": "text", "text": f"OK {turn_num}"}]
        new_asst['message']['id'] = f"msg_{uuid.uuid4().hex[:24]}"

        new_lines.append(json.dumps(new_asst))

        # Queue op after assistant
        q2 = {"type": "queue-operation", "operation": "dequeue",
               "timestamp": new_asst.get('timestamp', ''), "sessionId": new_session_id}
        new_lines.append(json.dumps(q2))

        prev_uuid = new_asst['uuid']

    new_file.write_text("\n".join(new_lines) + "\n")
    total_turns = len(pairs) + extra_turns_needed
    est_tokens = total_turns * TOKENS_PER_TURN

    print(f"\nCreated inflated session:")
    print(f"  Session ID: {new_session_id}")
    print(f"  File: {new_file}")
    print(f"  Size: {new_file.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  Total turns: {total_turns}")
    print(f"  Estimated tokens: {est_tokens:,}")
    print(f"\nTo test (ONE API call):")
    print(f"  python3 spikes/resume_inflated.py {new_session_id}")

    return new_session_id


if __name__ == "__main__":
    inflate()
