"""CLI client for OBS Agent.

Simple REPL that sends messages to the daemon HTTP API and displays responses.
Auto-starts daemon if not running.
"""

from __future__ import annotations

import sys

import httpx

from obs_agent.config import OBSConfig


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


def main():
    """Entry point for obs-agent CLI."""
    config = OBSConfig.from_env()
    base_url = config.base_url

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: obs-agent [--help]")
        print(f"Interactive CLI for OBS Agent (daemon at {base_url})")
        sys.exit(0)

    print(f"OBS Agent CLI (daemon: {base_url})")
    print("Type your message, or 'quit' to exit.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break

        response = send_message(user_input, base_url=base_url)
        print(f"\n{response}\n")


if __name__ == "__main__":
    main()
