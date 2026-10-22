"""Byte-preserving TCP/Unix relays for the loopback-only CLIProxyAPI service."""

from __future__ import annotations

import argparse
import asyncio
import signal
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _address(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("address must be HOST:PORT")
    try:
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return host.removeprefix("[").removesuffix("]"), number


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counts: list[int], index: int) -> None:
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
            return
        writer.write(chunk)
        counts[index] += len(chunk)
        await writer.drain()


async def _connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    opened = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    counts = [0, 0]
    upstream = None
    pumps = []
    error = "none"
    try:
        if args.command == "unix-to-tcp":
            remote_reader, upstream = await asyncio.open_connection(*args.target)
        else:
            remote_reader, upstream = await asyncio.open_unix_connection(str(args.unix))
        pumps = [
            asyncio.create_task(_pipe(reader, upstream, counts, 0)),
            asyncio.create_task(_pipe(remote_reader, writer, counts, 1)),
        ]
        await asyncio.gather(*pumps)
    except asyncio.CancelledError:
        error = "cancelled"
        raise
    except Exception as exc:
        error = type(exc).__name__
    finally:
        for pump in pumps:
            if not pump.done():
                pump.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        writers = [writer] if upstream is None else [writer, upstream]
        for stream in writers:
            stream.close()
        await asyncio.gather(*(stream.wait_closed() for stream in writers), return_exceptions=True)
        # INVARIANT(auth): log metadata only; payload logging would expose provider requests.
        # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
        print(
            f"relay={args.command} opened={opened} closed={datetime.now(timezone.utc).isoformat()} "
            f"bytes_out={counts[0]} bytes_in={counts[1]} duration_s={time.monotonic() - started:.3f} error={error}",
            file=sys.stderr, flush=True,
        )


def _remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise ValueError(f"refusing to replace non-socket path {path}")
    path.unlink()


async def serve(args: argparse.Namespace) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_connection(reader, writer, args))
        active.add(task)
        task.add_done_callback(active.discard)

    unix_server = args.command == "unix-to-tcp"
    server = None
    try:
        if unix_server:
            _remove_stale_socket(args.unix)
            server = await asyncio.start_unix_server(connected, path=str(args.unix))
            args.unix.chmod(0o660)
        else:
            server = await asyncio.start_server(connected, *args.listen)
        await stopping.wait()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in tuple(active):
            task.cancel()
        if active:
            await asyncio.gather(*tuple(active), return_exceptions=True)
        if unix_server and server is not None:
            args.unix.unlink(missing_ok=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    outward = commands.add_parser("unix-to-tcp", help="listen on a Unix socket and connect to TCP")
    outward.add_argument("--unix", type=Path, required=True)
    outward.add_argument("--target", type=_address, required=True)
    inward = commands.add_parser("tcp-to-unix", help="listen on TCP and connect to a Unix socket")
    inward.add_argument("--listen", type=_address, required=True)
    inward.add_argument("--unix", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
