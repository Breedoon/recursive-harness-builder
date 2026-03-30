"""
Inflate a session by making each existing user message longer.
Instead of adding turns (which get ignored), we fatten existing ones.

The SDK walks the parent chain backwards from the head pointer,
so it sees all original messages — just with more content each.
"""
import json
import uuid
from pathlib import Path
from copy import deepcopy

SOURCE_SESSION = "d79e308b-9c64-4614-9a09-1bbe4557fdc1"
PROJ_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
SOURCE_FILE = PROJ_DIR / f"{SOURCE_SESSION}.jsonl"

# Target ~850K tokens from 79 turns. Currently ~476K.
# Need to roughly double the padding in each user message.

_EXTRA_PADDING = (
    "The history of computing is a fascinating journey that spans several decades. "
    "From the earliest mechanical calculators to modern quantum processors, each era "
    "brought innovations that transformed how humans interact with information. Charles "
    "Babbage conceived the Analytical Engine in the 1830s, establishing principles that "
    "would guide computer design for over a century. Ada Lovelace, working alongside "
    "Babbage, wrote what many consider the first computer program. The twentieth century "
    "saw the development of electronic computers, starting with machines like ENIAC and "
    "UNIVAC. These room-sized devices used vacuum tubes and consumed enormous amounts of "
    "power. The invention of the transistor at Bell Labs revolutionized the field, making "
    "computers smaller, faster, and more reliable. The integrated circuit further "
    "accelerated this trend, packing thousands and eventually billions of transistors onto "
    "tiny silicon chips. Personal computers emerged in the 1970s and 1980s, bringing "
    "computational power to homes and offices worldwide. "
) * 60  # ~48K chars = ~12K tokens extra per message. Total per msg: ~18K. 79 * 18K = ~1.4M... too much

# Actually: original is ~476K / 79 = ~6K per turn. Need 850K / 79 = ~10.8K per turn.
# So add ~4.8K tokens = ~19.2K chars per message.
_EXTRA = (
    "The history of computing is a fascinating journey that spans several decades. "
    "From the earliest mechanical calculators to modern quantum processors, each era "
    "brought innovations that transformed how humans interact with information. "
) * 60  # ~18K chars = ~4.5K tokens


def inflate():
    with open(SOURCE_FILE) as f:
        lines = [json.loads(l) for l in f]

    new_session_id = str(uuid.uuid4())
    new_file = PROJ_DIR / f"{new_session_id}.jsonl"

    user_count = 0
    new_lines = []

    for l in lines:
        l2 = deepcopy(l)

        # Update session IDs
        if 'sessionId' in l2:
            l2['sessionId'] = new_session_id
        if 'session_id' in l2:
            l2['session_id'] = new_session_id

        # Fatten user messages
        if l2.get('type') == 'user' and 'message' in l2:
            original_content = l2['message']['content']
            l2['message']['content'] = original_content + f"\n\nAdditional reference material:\n{_EXTRA}"
            user_count += 1

        new_lines.append(json.dumps(l2))

    new_file.write_text("\n".join(new_lines) + "\n")

    orig_size = SOURCE_FILE.stat().st_size
    new_size = new_file.stat().st_size
    est_tokens_per_turn = (new_size / orig_size) * (476_000 / 79)
    est_total = est_tokens_per_turn * 79

    print(f"Source: {SOURCE_SESSION}")
    print(f"  {len(lines)} lines, {user_count} user messages fattened")
    print(f"  Original size: {orig_size / 1024 / 1024:.1f} MB")
    print(f"\nInflated: {new_session_id}")
    print(f"  New size: {new_size / 1024 / 1024:.1f} MB ({new_size/orig_size:.1f}x)")
    print(f"  Estimated tokens: {est_total:,.0f}")
    print(f"\nTo test: python3 spikes/resume_inflated.py {new_session_id}")

    return new_session_id


if __name__ == "__main__":
    inflate()
