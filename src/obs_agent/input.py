"""Input channel abstraction for OBS Agent CLI.

Defines a Protocol for input channels and provides two implementations:
- PromptToolkitChannel: rich terminal input with async prompt, visible typing
  during streaming, multiline paste support, and no buffer limits.
- SimpleChannel: wraps builtin input() for CI, piped input, and pexpect tests.

Future channels (Telegram, etc.) implement the same protocol.
"""

from __future__ import annotations

import asyncio
import select
import sys
from typing import Protocol

MAX_INPUT_LENGTH = 100_000


class InputChannel(Protocol):
    """Protocol for input channels."""

    async def read_input(self, prompt: str = "> ") -> str | None: ...
    def print_output(self, text: str) -> None: ...
    def print_status(self, text: str) -> None: ...
    def print_queued(self, messages: list[str]) -> None: ...
    def close(self) -> None: ...


class SimpleChannel:
    """Simple channel using builtin input() with run_in_executor.

    Works in CI, piped input, and pexpect tests. No prompt_toolkit dependency.
    """

    async def read_input(self, prompt: str = "> ") -> str | None:
        """Read input using builtin input() with select() drain for multiline paste."""
        loop = asyncio.get_event_loop()
        try:
            line = await loop.run_in_executor(None, lambda: input(prompt))
        except (EOFError, KeyboardInterrupt):
            return None

        # Drain any remaining pasted lines (multiline paste support)
        lines = [line]
        if sys.stdin.isatty():
            while select.select([sys.stdin], [], [], 0.0)[0]:
                extra = sys.stdin.readline()
                if not extra:
                    break
                lines.append(extra.rstrip("\n"))

        result = "\n".join(lines).strip()
        if not result:
            return ""

        # Safety cap
        if len(result) > MAX_INPUT_LENGTH:
            result = result[:MAX_INPUT_LENGTH]

        return result

    def print_output(self, text: str) -> None:
        """Print assistant output text."""
        print(text, end="", flush=True)

    def print_status(self, text: str) -> None:
        """Print a dimmed status message."""
        print(f"\033[2m({text})\033[0m", flush=True)

    def print_queued(self, messages: list[str]) -> None:
        """Print dimmed queued message previews."""
        for msg in messages:
            preview = msg[:120]
            if len(msg) > 120:
                preview += "..."
            print(f"\033[2m(queued: {preview})\033[0m", flush=True)

    def close(self) -> None:
        """No-op for simple channel."""
        pass


class PromptToolkitChannel:
    """Rich terminal channel using prompt_toolkit.

    Features:
    - Native asyncio prompt (no blocking, no buffer limit)
    - patch_stdout() keeps typed characters visible during streaming
    - Enter sends, Esc+Enter inserts newline
    - Bracketed paste mode captures multiline paste atomically
    """

    def __init__(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.patch_stdout import patch_stdout

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _newline(event):
            """Esc+Enter inserts a newline."""
            event.current_buffer.insert_text("\n")

        @bindings.add("enter")
        def _submit(event):
            """Enter submits the input."""
            event.current_buffer.validate_and_handle()

        self._session = PromptSession(key_bindings=bindings)
        self._patch_ctx = patch_stdout(raw=True)
        self._patch_ctx.__enter__()

    async def read_input(self, prompt: str = "> ") -> str | None:
        """Read input using prompt_toolkit's async prompt."""
        try:
            result = await self._session.prompt_async(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

        if result is None:
            return None

        result = result.strip()
        if not result:
            return ""

        # Safety cap
        if len(result) > MAX_INPUT_LENGTH:
            result = result[:MAX_INPUT_LENGTH]

        return result

    def print_output(self, text: str) -> None:
        """Print assistant output text."""
        print(text, end="", flush=True)

    def print_status(self, text: str) -> None:
        """Print a dimmed status message."""
        print(f"\033[2m({text})\033[0m", flush=True)

    def print_queued(self, messages: list[str]) -> None:
        """Print dimmed queued message previews."""
        for msg in messages:
            preview = msg[:120]
            if len(msg) > 120:
                preview += "..."
            print(f"\033[2m(queued: {preview})\033[0m", flush=True)

    def close(self) -> None:
        """Exit patch_stdout context."""
        try:
            self._patch_ctx.__exit__(None, None, None)
        except Exception:
            pass
