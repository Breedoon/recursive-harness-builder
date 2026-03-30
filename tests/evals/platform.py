"""Platform protocol and CLIPlatform for eval testing.

CLIPlatform drives the real OBS Agent CLI via pexpect, providing
send/read/close operations for the eval judge.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Protocol

import pexpect

# Unique prompt used in eval mode to avoid collisions with markdown "> " in agent output.
# Set via OBS_EVAL_PROMPT env var. The CLI reads this in SimpleChannel.
EVAL_PROMPT = "OBS_EVAL> "


class Platform(Protocol):
    """Protocol for eval platforms that interact with the agent."""

    async def send(self, text: str) -> str: ...
    async def send_nowait(self, text: str) -> None: ...
    async def read(self) -> str: ...
    async def wait_for_prompt(self, timeout: int = 120) -> str: ...
    async def close(self) -> None: ...


class CLIPlatform:
    """pexpect-based platform for driving the OBS Agent CLI.

    Spawns the CLI with OBS_SIMPLE_INPUT=1 (SimpleChannel) and
    the given vault path / daemon port. Uses a unique prompt string
    (OBS_EVAL>) to avoid matching markdown blockquotes in agent output.
    """

    def __init__(
        self,
        vault_path: Path,
        daemon_port: int = 7833,
        timeout: int = 120,
    ) -> None:
        self._timeout = timeout
        self._last_output = ""
        self._prompt_pattern = re.escape(EVAL_PROMPT)

        # Inherit parent PATH so the claude CLI binary can be found by the SDK.
        # Override only the vault/input/port settings.
        env = os.environ.copy()
        env["OBS_VAULT_PATH"] = str(vault_path)
        env["OBS_SIMPLE_INPUT"] = "1"
        env["OBS_DAEMON_PORT"] = str(daemon_port)
        env["OBS_EVAL_PROMPT"] = EVAL_PROMPT
        env["OBS_PROFILE"] = "test"

        python = str(Path(sys.executable))
        self._child = pexpect.spawn(
            python,
            ["-m", "obs_agent.cli"],
            env=env,
            timeout=timeout,
            encoding="utf-8",
            maxread=65536,
        )

        # Wait for the CLI to be ready, then consume the initial prompt.
        # Without consuming the prompt, the first expect(OBS_EVAL>) would match
        # the initial prompt instead of the post-response prompt.
        self._child.expect("Type your message", timeout=60)
        self._child.expect(self._prompt_pattern, timeout=30)

    async def send(self, text: str) -> str:
        """Send a message and wait for the next prompt, returning output."""
        self._child.sendline(text)
        self._child.expect(self._prompt_pattern, timeout=self._timeout)
        output = self._child.before or ""
        self._last_output = output.strip()
        return self._last_output

    async def send_nowait(self, text: str) -> None:
        """Send text without waiting for a response. Used for concurrent testing."""
        self._child.sendline(text)

    async def read(self) -> str:
        """Return the most recent output."""
        return self._last_output

    async def wait_for_prompt(self, timeout: int = 120) -> str:
        """Wait for the next prompt and return all output before it."""
        self._child.expect(self._prompt_pattern, timeout=timeout)
        output = self._child.before or ""
        self._last_output = output.strip()
        return self._last_output

    async def close(self) -> None:
        """Send /quit and terminate the CLI process."""
        try:
            if self._child.isalive():
                self._child.sendline("/quit")
                self._child.expect(pexpect.EOF, timeout=10)
        except (pexpect.EOF, pexpect.TIMEOUT):
            pass
        finally:
            if self._child.isalive():
                self._child.terminate(force=True)
