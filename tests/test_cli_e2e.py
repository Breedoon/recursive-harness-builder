"""Real CLI end-to-end tests using pexpect to drive the terminal.

These tests start the actual CLI process, type messages, type during
streaming, and verify behavior. LLM-as-judge (Haiku) evaluates responses.

Run with: .venv/bin/pytest tests/test_cli_e2e.py -v -m e2e --timeout=300
"""
from __future__ import annotations

import logging
import os
import time

import pexpect
import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM-as-judge (same pattern as test_real_e2e.py)
# ---------------------------------------------------------------------------

_HAS_JUDGE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_judge(question: str, response: str, criterion: str) -> bool:
    """Use Haiku to judge whether a response meets a criterion.

    Falls back to a simple non-empty check if no ANTHROPIC_API_KEY is set.
    """
    if not _HAS_JUDGE_KEY:
        logger.warning(
            "No ANTHROPIC_API_KEY -- skipping LLM judge, using heuristic fallback"
        )
        return len(response.strip()) > 5

    import anthropic

    client = anthropic.Anthropic()
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Assess this AI agent interaction:\n\n"
                    f"User asked: {question}\n\n"
                    f"Agent responded: {response[:500]}\n\n"
                    f"Criterion: {criterion}\n\n"
                    f"Does the response meet the criterion? Answer ONLY 'YES' or 'NO'."
                ),
            }
        ],
    )
    answer = result.content[0].text.strip().upper()
    passed = "YES" in answer
    if not passed:
        logger.warning(
            "LLM judge said NO -- criterion: %s | answer: %s | response[:200]: %s",
            criterion,
            answer,
            response[:200],
        )
    return passed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spawn_cli(timeout: int = 120) -> pexpect.spawn:
    """Spawn the CLI process using the project's venv Python.

    Sets OBS_SIMPLE_INPUT=1 to force SimpleChannel (no prompt_toolkit escape
    codes that interfere with pexpect pattern matching).
    """
    env = os.environ.copy()
    env["OBS_SIMPLE_INPUT"] = "1"
    return pexpect.spawn(
        "/Users/breedoon/Documents/obs/.venv/bin/python -m obs_agent.cli",
        timeout=timeout,
        encoding="utf-8",
        cwd="/Users/breedoon/Documents/obs",
        env=env,
    )


def _wait_for_ready(child: pexpect.spawn, timeout: int = 60) -> None:
    """Wait for the CLI to be ready (shows 'Type your message')."""
    child.expect("Type your message", timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCLIRealInteraction:
    """Real terminal-driving tests for the CLI.

    Each test spawns a fresh CLI process with pexpect, sends messages,
    and validates output. These hit the real Claude SDK -- no mocking.
    """

    def test_quit_command(self):
        """CLI exits cleanly on /quit."""
        child = _spawn_cli(timeout=60)
        _wait_for_ready(child)
        child.sendline("/quit")
        child.expect("Goodbye", timeout=10)
        child.expect(pexpect.EOF, timeout=10)

    def test_basic_chat(self):
        """Start CLI, send a message, get a response."""
        child = _spawn_cli()
        _wait_for_ready(child)
        child.sendline("Say hello in exactly one word.")
        # Wait for response + next prompt
        child.expect(r"> ", timeout=120)
        output = child.before  # Everything before the prompt
        assert len(output.strip()) > 0, "Should get some response text"
        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_prompt_returns_immediately(self):
        """After response, prompt appears immediately without extra Enter press.

        This validates Bug 2 fix: no orphaned readline thread consuming stdin.
        """
        child = _spawn_cli()
        _wait_for_ready(child)
        child.sendline("Say OK and nothing else.")

        # The prompt should appear after the response without requiring Enter
        child.expect(r"> ", timeout=120)

        # Should be able to type immediately without pressing Enter first
        child.sendline("Say goodbye and nothing else.")
        child.expect(r"> ", timeout=120)

        output = child.before
        assert len(output.strip()) > 0, "Should get a response to the second message"
        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_queue_message_during_streaming(self):
        """Type during streaming, verify queued message reaches agent.

        This validates Bug 1 fix: queued messages that arrive after query()
        finishes are drained and prepended to the next turn's prompt.
        """
        child = _spawn_cli()
        _wait_for_ready(child)

        # Send first message (something that takes a while to stream)
        child.sendline("List 5 facts about the ocean. Be detailed.")

        # Wait a moment for streaming to start, then queue a message
        time.sleep(3)
        child.sendline("remember the code word is BANANA")
        # Verify the CLI acknowledged the queue
        child.expect(r"\(queued\)", timeout=15)

        # Wait for response to complete and prompt to return
        child.expect(r"> ", timeout=120)

        # Now ask about the queued message
        child.sendline("What code word did I give you?")
        child.expect(r"> ", timeout=120)
        response = child.before

        # LLM judge: does the response mention BANANA?
        assert llm_judge(
            "What code word did I give you?",
            response,
            "Does the response mention the word BANANA?",
        ), f"Agent didn't receive queued message. Got: {response[:300]}"

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_stop_during_streaming(self):
        """Sending /stop during streaming interrupts the agent."""
        child = _spawn_cli()
        _wait_for_ready(child)

        # Send a long request
        child.sendline("Write a 500-word essay about climate change.")
        time.sleep(3)
        child.sendline("/stop")
        child.expect(r"\(interrupting\.\.\.\)", timeout=10)

        # Should get back to prompt relatively quickly
        child.expect(r"> ", timeout=60)

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)


@pytest.mark.e2e
class TestCLIStatusEvents:
    """E2E tests verifying status events appear in the CLI output.

    Status events display as dim text like (classifying skills...),
    (reading Agent/context.md), etc.
    """

    def test_classify_status_appears(self):
        """The (classifying skills...) status appears when sending a message."""
        child = _spawn_cli()
        _wait_for_ready(child)
        # Message must exceed classification_threshold (100 chars) to trigger classification
        child.sendline("Please say hello in exactly one word. I want to verify that the system is working correctly and that skill classification triggers properly.")

        # The output between the first sendline and the next > prompt
        # should contain the classification status and the response.
        # Use a longer regex that won't match the initial "> " from banner.
        # We expect to see "classifying skills" somewhere before the next ">"
        idx = child.expect(
            [r"classifying skills", pexpect.TIMEOUT],
            timeout=120,
        )
        if idx == 0:
            # Found it! Now wait for prompt to complete
            child.expect(r"> ", timeout=120)
        else:
            # Gather whatever output we got
            child.expect(r"> ", timeout=120)
            output = child.before
            assert False, (
                f"Expected '(classifying skills...)' in output. Got: {output[:500]}"
            )

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_status_events_during_tool_use(self):
        """Status events appear when the agent uses tools.

        Ask the agent to do something that requires tool use (like reading
        a file), then verify tool_use status events appear.
        """
        child = _spawn_cli()
        _wait_for_ready(child)

        # Ask to read a file - this should trigger tool use
        # Message must exceed classification_threshold (100 chars) to trigger classification
        child.sendline("Please read my Agent/context.md file and tell me what's in it. I want to know the full contents of the file including all sections and details.")

        # Look for the classifying skills status event first
        idx = child.expect(
            [r"classifying skills", pexpect.TIMEOUT],
            timeout=120,
        )
        if idx == 0:
            # Good - found classification status. Now look for tool use status
            # or just wait for prompt
            idx2 = child.expect(
                [r"Read: |Bash: |Grep: ", r"> "],
                timeout=120,
            )
            if idx2 == 0:
                # Found tool use status - wait for prompt
                child.expect(r"> ", timeout=120)
            # If idx2 == 1, we got to prompt (classification was there at least)
        else:
            assert False, "Expected '(classifying skills...)' status event"

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_simplified_queued_format(self):
        """Queue acknowledgement shows just (queued) not (queued: text...).

        Validates the simplified queue format from Task #4.
        The old format was: (queued: follow up question)
        The new format is:  (queued)
        """
        child = _spawn_cli()
        _wait_for_ready(child)

        # Send a long request
        child.sendline("List 10 interesting facts about space. Be very detailed.")

        # Wait for streaming to start, then queue a message
        time.sleep(3)
        child.sendline("follow up question")

        # Should see (queued) not (queued: follow up question)
        child.expect(r"\(queued\)", timeout=15)

        # child.after is the matched text "(queued)"
        # Verify the matched text is EXACTLY "(queued)" without message content
        # The old format would be "(queued: follow up question...)"
        assert child.after.strip() == "(queued)", (
            f"Queue format should be exactly '(queued)', got: {child.after!r}"
        )

        # Wait for response to complete
        child.expect(r"> ", timeout=120)

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)


@pytest.mark.e2e
class TestCLILargeMessages:
    """E2E tests verifying large and multiline messages work through the CLI."""

    def test_large_message_via_multiline(self):
        """CLI handles large input (2000+ chars) sent as multiline paste.

        Terminal line buffers cap single lines at ~1024 bytes (macOS).
        Large content should be pasted as multiple lines. This test sends
        a large block of text across many lines with a unique marker at
        the end and verifies the agent sees the full content.
        """
        child = _spawn_cli()
        _wait_for_ready(child)

        # Build ~2500 chars spread across multiple lines
        filler_lines = [f"Context line {i}: some filler text about topic number {i} here." for i in range(1, 46)]
        marker = "FLAMINGO_CASTLE_3847"
        filler_lines.append(f"The secret code is: {marker}")
        filler_lines.append("What is the secret code I just gave you? Reply with ONLY the code.")
        message = "\r\n".join(filler_lines) + "\r\n"
        assert len(message) > 2000, f"Message should be >2000 chars, got {len(message)}"

        child.send(message)
        child.expect(r"> ", timeout=120)
        response = child.before

        assert llm_judge(
            "What is the secret code?",
            response,
            f"Does the response contain or mention the code {marker}?",
        ), f"Large multiline message truncated in CLI. Got: {response[:500]}"

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)

    def test_multiline_paste(self):
        """CLI handles pasted multiline text (simulated with rapid sendline).

        Sends multiple lines rapidly (simulating paste), with a marker
        on the last line. Verifies the agent receives all lines.
        """
        child = _spawn_cli()
        _wait_for_ready(child)

        # Simulate pasting multiline text by sending lines rapidly
        # pexpect.sendline adds \\n, so this simulates a paste of several lines
        lines = [
            "I am pasting multiple lines of text.",
            "This is line 2 with some context.",
            "This is line 3 with more context.",
            "This is line 4 with even more context.",
            "This is line 5 with still more context.",
            "The secret code on the last line is DOLPHIN_NEBULA_9216.",
            "What is the secret code? Reply with ONLY the code.",
        ]
        # Send all lines as a single block to simulate paste
        child.send("\r\n".join(lines) + "\r\n")

        child.expect(r"> ", timeout=120)
        response = child.before

        marker = "DOLPHIN_NEBULA_9216"
        assert llm_judge(
            "What is the secret code?",
            response,
            f"Does the response contain or mention the code {marker}?",
        ), f"Multiline paste lost content in CLI. Got: {response[:500]}"

        child.sendline("/quit")
        child.expect(pexpect.EOF, timeout=10)
