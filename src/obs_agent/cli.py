"""CLI client for OBS Agent.

REPL that sends messages to the daemon HTTP API and displays responses.
Auto-starts daemon if not running. Supports slash commands, concurrent input,
and pluggable input channels (prompt_toolkit for TTY, SimpleChannel for pipes/CI).

See implementation-plan.md Step 10.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import httpx

from obs_agent.config import OBSConfig

if TYPE_CHECKING:
    from obs_agent.input import InputChannel


def parse_slash_command(text: str) -> tuple[str | None, str | None]:
    """Parse input for slash commands.

    Returns (command, None) for slash commands like "/stop", "/quit".
    Returns (None, text) for regular messages.
    Returns (None, None) for empty input.
    """
    text = text.strip()
    if not text:
        return (None, None)
    if text.startswith("/"):
        return (text, None)
    return (None, text)


def check_daemon(base_url: str) -> bool:
    """Check if the daemon is running by hitting /health."""
    try:
        resp = httpx.get(f"{base_url}/health", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def start_daemon(config: OBSConfig) -> subprocess.Popen | None:
    """Start the daemon as a background subprocess.

    Returns the Popen object if started, None if daemon was already running.
    """
    if check_daemon(config.base_url):
        return None

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "obs_agent.daemon:create_default_app",
            "--factory",
            "--host",
            config.daemon_host,
            "--port",
            str(config.daemon_port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(config.vault_path),
    )

    # Wait for daemon to be ready (up to 5 seconds)
    for _ in range(50):
        if check_daemon(config.base_url):
            return proc
        time.sleep(0.1)

    return proc


def send_message(message: str, *, base_url: str) -> str:
    """Send a message to the daemon and return the response."""
    try:
        response = httpx.post(
            f"{base_url}/chat",
            json={"message": message},
            timeout=120.0,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("response", "")
        return f"Error: daemon returned status {response.status_code}"
    except Exception as e:
        return f"Error: connection failed - {e}"


def _render_status(data_lines: list[str], channel: InputChannel) -> None:
    """Parse a status event's data lines and render through the channel."""
    raw = "\n".join(data_lines)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    # queue_delivered with messages: show actual message content
    if payload.get("type") == "queue_delivered" and payload.get("messages"):
        channel.print_queued(payload["messages"])
        return

    summary = payload.get("summary", "")
    if summary:
        channel.print_status(summary)


async def stream_message(message: str, *, base_url: str, channel: InputChannel) -> None:
    """Stream an SSE response from the daemon, printing tokens as they arrive."""
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/stream",
                json={"message": message},
                timeout=120.0,
            ) as response:
                if response.status_code != 200:
                    channel.print_output(f"Error: daemon returned status {response.status_code}\n")
                    return

                event_type: str | None = None
                event_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            if event_lines:
                                if event_type == "status":
                                    _render_status(event_lines, channel)
                                else:
                                    channel.print_output("\n".join(event_lines))
                            break
                        event_lines.append(payload)
                    elif line == "" and event_lines:
                        if event_type == "status":
                            _render_status(event_lines, channel)
                        else:
                            channel.print_output("\n".join(event_lines))
                        event_lines = []
                        event_type = None
    except Exception as e:
        channel.print_output(f"Error: connection failed - {e}\n")


async def _handle_input_during_stream(
    base_url: str, stop_event: asyncio.Event, channel: InputChannel
) -> str | None:
    """Read stdin non-blockingly, checking stop_event periodically.

    Uses select() with a short timeout so the thread can check stop_event
    between polls, preventing orphaned blocking readline threads from
    consuming stdin after the SSE stream finishes.

    Returns "/quit" if quit was requested, None otherwise.
    """
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        # Check if stdin has data (with 0.5s timeout)
        ready = await loop.run_in_executor(
            None, lambda: select.select([sys.stdin], [], [], 0.5)[0]
        )
        if not ready:
            continue  # No input, check stop_event and loop

        line = sys.stdin.readline().strip()
        if not line:
            continue

        command, text = parse_slash_command(line)

        if command == "/stop":
            channel.print_status("interrupting...")
            try:
                httpx.post(f"{base_url}/chat/interrupt", timeout=5.0)
            except Exception:
                pass
        elif command == "/quit":
            channel.print_status("quitting...")
            try:
                httpx.post(f"{base_url}/chat/interrupt", timeout=5.0)
            except Exception:
                pass
            return "/quit"
        elif text:
            try:
                httpx.post(
                    f"{base_url}/chat/enqueue",
                    json={"message": text},
                    timeout=5.0,
                )
                print("(queued)", flush=True)
            except Exception:
                print("(failed to queue message)", flush=True)

    return None


async def stream_with_input(message: str, *, base_url: str, channel: InputChannel) -> str | None:
    """Stream SSE response while accepting concurrent input.

    Uses a stop_event to cleanly signal the input handler to exit when
    the SSE stream finishes. No termios manipulation needed — prompt_toolkit's
    patch_stdout handles display for PromptToolkitChannel, and SimpleChannel
    doesn't need it.

    Returns "/quit" if the user typed /quit during streaming, None otherwise.
    """
    stop_event = asyncio.Event()

    try:
        async with httpx.AsyncClient() as client:
            sse_task = asyncio.create_task(
                _consume_sse(client, base_url, message, channel)
            )
            input_task = asyncio.create_task(
                _handle_input_during_stream(base_url, stop_event, channel)
            )

            # Wait for SSE to finish (or input to signal quit)
            done, pending = await asyncio.wait(
                [sse_task, input_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Signal input handler to stop cleanly
            stop_event.set()

            # Cancel whichever is still running
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Check if input task signalled quit
            if input_task in done:
                result = input_task.result()
                if result == "/quit":
                    return "/quit"

            return None
    except Exception as e:
        channel.print_output(f"Error: connection failed - {e}\n")
        return None


async def _consume_sse(
    client: httpx.AsyncClient, base_url: str, message: str, channel: InputChannel
) -> None:
    """Consume SSE stream from daemon, rendering through channel."""
    async with client.stream(
        "POST",
        f"{base_url}/chat/stream",
        json={"message": message},
        timeout=120.0,
    ) as response:
        if response.status_code != 200:
            channel.print_output(f"Error: daemon returned status {response.status_code}\n")
            return

        event_type: str | None = None
        event_lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    if event_lines:
                        if event_type == "status":
                            _render_status(event_lines, channel)
                        else:
                            channel.print_output("\n".join(event_lines))
                    break
                event_lines.append(payload)
            elif line == "" and event_lines:
                if event_type == "status":
                    _render_status(event_lines, channel)
                else:
                    channel.print_output("\n".join(event_lines))
                event_lines = []
                event_type = None


async def async_main() -> None:
    """Async entry point for obs-agent CLI."""
    config = OBSConfig.from_env()
    base_url = config.base_url

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: obs-agent [--help]")
        print(f"Interactive CLI for OBS Agent (daemon at {base_url})")
        sys.exit(0)

    # Channel selection: PromptToolkitChannel for TTY, SimpleChannel otherwise.
    # OBS_SIMPLE_INPUT=1 forces SimpleChannel (for pexpect tests, etc.)
    channel: InputChannel
    force_simple = os.environ.get("OBS_SIMPLE_INPUT", "").strip() == "1"
    if sys.stdin.isatty() and not force_simple:
        try:
            from obs_agent.input import PromptToolkitChannel
            channel = PromptToolkitChannel()
        except ImportError:
            from obs_agent.input import SimpleChannel
            channel = SimpleChannel()
    else:
        from obs_agent.input import SimpleChannel
        channel = SimpleChannel()

    # Auto-start daemon if not running
    daemon_proc = None
    if not check_daemon(base_url):
        print(f"Starting daemon at {base_url}...")
        daemon_proc = start_daemon(config)
        if daemon_proc and check_daemon(base_url):
            print("Daemon started.")
        else:
            print("Warning: Could not start daemon. Continuing anyway.")

    # Allow custom prompt for eval testing (avoids "> " colliding with markdown blockquotes)
    prompt = os.environ.get("OBS_EVAL_PROMPT", "> ")

    print(f"OBS Agent CLI (daemon: {base_url})")
    print("Type your message, or /quit to exit. (Esc+Enter for newline)\n")

    try:
        while True:
            user_input = await channel.read_input(prompt)
            if user_input is None:
                print("\nGoodbye.")
                break

            if not user_input:
                continue

            # Backward-compat: bare quit/exit/q still work
            if user_input.lower() in ("quit", "exit", "q"):
                print("Goodbye.")
                break

            # Parse slash commands at prompt level
            command, text = parse_slash_command(user_input)

            if command == "/quit":
                print("Goodbye.")
                break
            elif command == "/stop":
                print("(nothing to interrupt)")
                continue
            elif command is not None:
                print(f"Unknown command: {command}")
                continue

            # Regular message: stream with concurrent input
            try:
                result = await stream_with_input(text, base_url=base_url, channel=channel)
                if result == "/quit":
                    print("Goodbye.")
                    break
            except KeyboardInterrupt:
                print("\n(interrupting...)")
                try:
                    httpx.post(f"{base_url}/chat/interrupt", timeout=2.0)
                except Exception:
                    pass
            print()
    finally:
        channel.close()
        # Cleanup: terminate daemon if we started it
        if daemon_proc is not None:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)


def main():
    """Entry point for obs-agent CLI."""
    # Handle --help synchronously before starting the event loop
    if "--help" in sys.argv or "-h" in sys.argv:
        config = OBSConfig.from_env()
        print("Usage: obs-agent [--help]")
        print(f"Interactive CLI for OBS Agent (daemon at {config.base_url})")
        sys.exit(0)

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
