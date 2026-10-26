"""Lane-local event-driven active-latch SIGTERM watcher; never a daemon patch."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from tests.e2e.harness.events import FileTail
from tests.e2e.harness.evc_fixture import _process_record, record


def latch_match(line: str, chat: int, topic: int) -> bool:
    return f"[task_stop] latched route=TelegramRoute(chat_id={chat}, thread_id={topic}) active=True " in line


async def watch(root: Path, pid: int, start: int, chat: int, topic: int, offset: int) -> dict:
    initial = _process_record(Path("/proc") / str(pid), cli=True)
    assert initial["start"] == start and initial["state"] not in {"Z", "X"}, initial
    assert initial["binary_sha256"] == "7a4a653982b07e0a8157f8d3b2c2f8e442520ab07b2fa2e692ba054dbba210c9", initial
    assert "--input-format" in initial["cli_launch"], initial
    descriptor = os.pidfd_open(pid)
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def on_line(line, read_at):
        if future.done() or not latch_match(line, chat, topic):
            return
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            current = {"pid": pid, "start": int(fields[19]), "state": fields[0],
                       "ppid": int(fields[1]), "pgid": int(fields[2]), "sid": int(fields[3])}
            assert current["start"] == start and current["state"] not in {"Z", "X"}, current
            signal_before = time.time()
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            signal_after = time.time()
            latch_epoch = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc).timestamp()
            receipt = {"latch_line": line, "latch_log_epoch": latch_epoch, "latch_read_epoch": read_at,
                       "target_pid": pid, "target_start": start, "pre_signal_identity": current,
                       "pre_signal_identity_scope": "direct stat only; binary hash and safe flags captured before watcher readiness",
                       "prewatch_binary_sha256": initial["binary_sha256"],
                       "signal": "SIGTERM", "signal_method": "pidfd_send_signal",
                       "signal_before_epoch": signal_before, "signal_after_epoch": signal_after,
                       "log_read_to_signal_seconds": signal_before - read_at,
                       "signal_syscall_seconds": signal_after - signal_before,
                       "signal_syscall_returned": True}
            record(root, "active_latch_sigterm", receipt=receipt)
            future.set_result(receipt)
        except Exception as exc:
            record(root, "active_latch_signal_failure", error=type(exc).__name__ + ": " + str(exc))
            future.set_exception(exc)

    tail = FileTail(Path("/workspace/runtime/logs/telegram-prod.log"), on_line)
    tail.offset = offset
    try:
        tail.start()
        assert tail.mode == "inotify", "this race requires event-driven native inotify, not a poll"
        record(root, "latch_watcher_ready", target_pid=pid, target_start=start, mode=tail.mode, log_offset=offset)
        return await asyncio.wait_for(future, 45)
    finally:
        tail.close()
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    for name in ("pid", "start", "chat", "topic", "offset"):
        parser.add_argument("--" + name, required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(watch(args.root, args.pid, args.start, args.chat, args.topic, args.offset))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
