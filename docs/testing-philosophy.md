# Testing Philosophy (SUPERSEDED)

> **This document is superseded by the Evaluations section in CLAUDE.md.**
> The eval infrastructure in `tests/evals/` replaces the test layers described below.
> Kept for historical reference only.

## Core Principle

**Assume code is broken until proven otherwise with real requests.**

"Tests passing" with mocked SDK calls proves nothing about the system working. Every feature must be verified end-to-end: real daemon, real HTTP, real SDK, real responses.

## Test Layers

All new features require tests at every layer:

### Layer 1: Unit Tests
- Fast, mocked, test individual functions in isolation
- Mock objects MUST match real SDK types: `mock_msg.content = [TextBlock(text="...")]` not `"string"`
- Good for: pure logic, data transformations, parsing
- Files: `tests/test_hooks.py`, `tests/test_session.py`, `tests/test_commands.py`

### Layer 2: Integration Tests (TestClient)
- FastAPI TestClient — in-process HTTP, no real server
- SDK `query()` is mocked, everything else runs for real
- Good for: endpoint routing, request validation, response format, shared state wiring
- Files: `tests/test_daemon.py`, `tests/test_queue_integration.py`

### Layer 3: Integration Tests (Live HTTP)
- Real uvicorn server on random port, real TCP connections
- SDK `query()` is mocked, but HTTP stack is fully real
- Good for: catching bugs that TestClient masks (missing routes, middleware issues)
- Files: `tests/test_http_integration.py`

### Layer 4: Real E2E (LLM-as-Judge)
- Real uvicorn + real SDK + real HTTP — zero mocking
- Response quality evaluated by Haiku as LLM judge, not brittle string matching
- Good for: proving the system actually works as a human would use it
- Files: `tests/test_real_e2e.py`

## LLM-as-Judge Pattern

Instead of `assert "hello" in response` (brittle, breaks when model behavior changes), use an LLM to evaluate:

```python
def llm_judge(question: str, response: str, criterion: str) -> bool:
    client = anthropic.Anthropic()
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": (
                f"Assess this AI agent interaction:\n\n"
                f"User asked: {question}\n\n"
                f"Agent responded: {response[:500]}\n\n"
                f"Criterion: {criterion}\n\n"
                f"Does the response meet the criterion? Answer ONLY 'YES' or 'NO'."
            ),
        }],
    )
    return "YES" in result.content[0].text.upper()
```

Benefits:
- Robust to model output variations
- Tests semantic correctness, not exact wording
- Criteria are human-readable and self-documenting
- Cheap (Haiku is fast and inexpensive)

## Auth

The Claude Agent SDK uses subscription auth — no `ANTHROPIC_API_KEY` env var needed for SDK calls. E2E tests should NOT gate on API key presence.

The LLM judge (direct Anthropic API via `anthropic` package) does need `ANTHROPIC_API_KEY`. When unavailable, fall back to heuristic checks (non-empty response > N chars).

## Running Tests

```bash
# Unit + integration (fast, no API calls)
.venv/bin/pytest tests/ -q --tb=short -m "not e2e"

# Real E2E (hits Claude API, ~2 min)
.venv/bin/pytest tests/test_real_e2e.py -v -m e2e --timeout=300

# Everything
.venv/bin/pytest tests/ -v --timeout=300
```

## Layer 5: Terminal E2E (pexpect + LLM-as-Judge)

The highest-confidence layer. Uses `pexpect` to spawn the actual CLI process and interact with it like a human — typing messages, typing during streaming, verifying prompts return immediately.

```python
import pexpect

child = pexpect.spawn('.venv/bin/python -m obs_agent.cli', timeout=120, encoding='utf-8')
child.expect('Type your message', timeout=30)
child.sendline('List 5 facts about the ocean.')
time.sleep(3)  # let streaming start
child.sendline('remember the code word is BANANA')
child.expect(r'\(queued\)', timeout=10)
child.expect('> ', timeout=120)
child.sendline('What code word did I give you?')
child.expect('> ', timeout=120)
response = child.before
assert llm_judge("What code word?", response, "Does the response mention BANANA?")
```

Why this matters:
- **Catches bugs mocked tests miss**: stdin threading issues, orphaned readline threads eating input, queued messages never reaching the agent — all caught by pexpect, all invisible to mocked tests.
- **Tests the full vertical**: CLI process → daemon startup → HTTP → SDK → hooks → SSE → terminal output. Every seam is exercised.
- **Race conditions**: Typing during streaming exposes timing bugs (queue injection, interrupt at tool boundaries, stdin contention) that unit tests cannot reproduce.
- Files: `tests/test_cli_e2e.py`

### Lessons Learned

1. **Mocked tests create false confidence.** The team wrote 263 passing tests across 5 layers of mocked/integration tests. Two critical bugs shipped anyway: queued messages never reached the agent (queue only drained at hook boundaries, not after query()), and stdin was consumed by orphaned readline threads. Both were immediately obvious in manual testing.

2. **If a human would test it by typing in the terminal, write a pexpect test.** CLI interaction has timing, concurrency, and I/O concerns that no amount of mocking can verify. The pexpect tests caught both bugs in seconds.

3. **LLM-as-judge + pexpect is the gold standard for agent E2E.** Pexpect drives the real process, LLM judge evaluates semantic correctness. Together they test both the plumbing and the intelligence.

4. **Don't trust "tests passing" — run the real thing.** Every feature should be manually tested at least once. If it breaks when you try it, your tests are insufficient regardless of count or coverage.

## Known Limitations

- **Interrupt only fires at tool boundaries**: The interrupt hook checks `interrupt_flag` during PreToolUse/PostToolUse callbacks. Pure text generation without tool calls never hits these boundaries. Future work: switch from `query()` to `ClaudeSDKClient` for true mid-token interrupt.
- **Enqueue injection timing**: Queued messages are injected as `additionalContext` at hook boundaries. If the agent doesn't use tools, the queued message stays in the queue until the next turn's first tool call. Fixed: daemon now drains remaining queue after query() and prepends to next turn.
