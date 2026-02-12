"""CLI client for OBS Agent.

Simple REPL that sends messages to the daemon HTTP API and displays responses.
Auto-starts daemon if not running.

See implementation-plan.md Step 10.
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

from obs_agent.config import OBSConfig


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
            "obs_agent.daemon:app",
            "--host",
            config.daemon_host,
            "--port",
            str(config.daemon_port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


def main():
    """Entry point for obs-agent CLI."""
    config = OBSConfig.from_env()
    base_url = config.base_url

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: obs-agent [--help]")
        print(f"Interactive CLI for OBS Agent (daemon at {base_url})")
        sys.exit(0)

    # Auto-start daemon if not running
    daemon_proc = None
    if not check_daemon(base_url):
        print(f"Starting daemon at {base_url}...")
        daemon_proc = start_daemon(config)
        if daemon_proc and check_daemon(base_url):
            print("Daemon started.")
        else:
            print("Warning: Could not start daemon. Continuing anyway.")

    print(f"OBS Agent CLI (daemon: {base_url})")
    print("Type your message, or 'quit' to exit.\n")

    try:
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
    finally:
        # Cleanup: terminate daemon if we started it
        if daemon_proc is not None:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)


if __name__ == "__main__":
    main()
