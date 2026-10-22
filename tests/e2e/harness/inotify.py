"""Minimal Linux inotify binding (ctypes, no dependency) for the E2E wait layer."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_Q_OVERFLOW = 0x00004000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

_EVENT = struct.Struct("iIII")
_libc = None


def _lib():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return _libc


class Inotify:
    """One inotify instance; ``fileno()`` is pollable (asyncio ``add_reader``)."""

    def __init__(self) -> None:
        fd = _lib().inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self.fd = fd
        self.watches: dict[int, str] = {}

    def fileno(self) -> int:
        return self.fd

    def add_watch(self, path: str, mask: int = IN_MODIFY | IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE) -> int:
        wd = _lib().inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_add_watch({path}): {os.strerror(err)}")
        self.watches[wd] = path
        return wd

    def read(self) -> list[tuple[int, int, str]]:
        """Return pending ``(wd, mask, name)`` events without blocking."""
        out: list[tuple[int, int, str]] = []
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return out
                raise
            if not data:
                return out
            offset = 0
            while offset + _EVENT.size <= len(data):
                wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                name = data[offset + _EVENT.size: offset + _EVENT.size + length].rstrip(b"\0").decode("utf-8", "replace")
                out.append((wd, mask, name))
                offset += _EVENT.size + length

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
