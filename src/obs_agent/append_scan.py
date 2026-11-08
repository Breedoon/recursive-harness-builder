"""Incremental folds over append-only line files (session JSONLs).

obs-54: the Telegram daemon re-read whole multi-MB session JSONLs on its event
loop to find one value: the latest bootstrap block (lineage.py, on every
stream event of a run) or the newest event timestamp (tools.py search_team,
for every member of a team). Those files only grow (the CLI appends; OBS
appends overlays and only ever cuts a torn final record), so a scan keeps its
result together with the byte offset it reached and, on the next call, reads
only the bytes appended since.

The result equals a full scan of the file's current bytes:

- Only complete lines (ending in a newline) move the stored offset. A torn
  last line is folded into this call's result, never into the stored state.
- A file that was replaced (other device/inode), truncated below the stored
  offset, or rewritten is scanned again from the start. "Rewritten" means its
  first bytes or the bytes just before the stored offset changed, or it changed
  (new mtime or ctime) without changing length.
- A rewrite that also changes the length and keeps both byte ranges cannot be
  told from an append without reading the whole file; no OBS or CLI writer
  does it. Each stored state is trusted for ``revalidate_seconds`` (default
  300) after its last full scan, so even such an edit is seen within that time.
- Lines are split like Python's text mode with universal newlines: on "\\n",
  and on a lone "\\r" inside a line.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Generic, TypeVar

S = TypeVar("S")

# Bytes kept from the start of the file and from just before the stored
# offset. Both must be unchanged on the next call, or the file is scanned again.
_SIGNATURE_BYTES = 256
_BLOCK_BYTES = 4 << 20
_monotonic = time.monotonic


class AppendScanCache(Generic[S]):
    """Fold every line of a file into a state, re-reading only appended bytes.

    ``fold(state, line)`` gets each line decoded and stripped, empty lines
    included, and returns the new state. States must be immutable values: the
    stored state is reused by later calls.

    ``needle`` (ASCII): a line is passed to the fold only if it contains the
    needle or a JSON ``\\u`` escape, the one other way a JSON line can spell
    ASCII text; the fold must leave the state unchanged for every other line.
    Every line is still decoded, so undecodable bytes raise exactly as a full
    text-mode read would with ``errors="strict"``.
    """

    def __init__(
        self,
        fold: Callable[[S, str], S],
        initial: S,
        *,
        encoding: str = "utf-8",
        errors: str = "strict",
        needle: str | None = None,
        maxsize: int = 4096,
        revalidate_seconds: float = 300.0,
    ) -> None:
        if needle is not None and not needle.isascii():
            raise ValueError("needle must be ASCII")
        self._fold = fold
        self._initial = initial
        self._encoding = encoding
        self._errors = errors
        self._needle = needle
        self._maxsize = maxsize
        self._revalidate_seconds = revalidate_seconds
        # path -> (dev, ino, offset, head bytes, bytes before offset, state at offset,
        #          monotonic time of the last full scan, (size, mtime_ns, ctime_ns) when read)
        self._entries: "OrderedDict[str, tuple[int, int, int, bytes, bytes, S, float, tuple[int, int, int]]]" = (
            OrderedDict()
        )
        self._lock = threading.Lock()
        self.full_scans = 0
        self.incremental_scans = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _fold_bytes(self, state: S, data: bytes) -> S:
        fold = self._fold
        needle = self._needle
        for line in data.decode(self._encoding, self._errors).split("\n"):
            if needle is not None and needle not in line and "\\u" not in line:
                continue
            if "\r" in line:
                for piece in line.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                    state = fold(state, piece.strip())
            else:
                state = fold(state, line.strip())
        return state

    def scan(self, path: Path) -> S:
        """The fold over every line of ``path``. Raises OSError like ``open``."""
        key = str(path)
        now = _monotonic()
        with open(path, "rb") as handle:
            st = os.fstat(handle.fileno())
            with self._lock:
                cached = self._entries.get(key)
            offset = 0
            state = self._initial
            head = b""
            before = b""
            verified_at = now
            if cached is not None:
                (dev, ino, cached_offset, cached_head, cached_before, cached_state, cached_verified,
                 cached_stat) = cached
                # Same length but a new mtime or ctime: changed without growing, so
                # not an append (a utime that restores mtime still sets ctime).
                rewritten_in_place = (
                    st.st_size == cached_stat[0] and (st.st_mtime_ns, st.st_ctime_ns) != cached_stat[1:]
                )
                if (
                    (dev, ino) == (st.st_dev, st.st_ino)
                    and cached_offset <= st.st_size
                    and not rewritten_in_place
                    and now - cached_verified < self._revalidate_seconds
                    and handle.read(len(cached_head)) == cached_head
                ):
                    handle.seek(cached_offset - len(cached_before))
                    if handle.read(len(cached_before)) == cached_before:
                        offset = cached_offset
                        state = cached_state
                        head = cached_head
                        before = cached_before
                        verified_at = cached_verified
            reused = offset
            if reused:
                self.incremental_scans += 1
            else:
                handle.seek(0)
                self.full_scans += 1
            # Blocks keep a first scan of a large file from holding it all in memory.
            carry = b""
            while True:
                block = handle.read(_BLOCK_BYTES)
                if not block:
                    break
                block = carry + block
                cut = block.rfind(b"\n") + 1
                if not cut:
                    carry = block
                    continue
                complete = block[:cut]
                carry = block[cut:]
                # The piece after the last newline is empty, not a line: drop it.
                state = self._fold_bytes(state, complete[:-1])
                if not offset:
                    head = complete[:_SIGNATURE_BYTES]
                offset += len(complete)
                before = (before + complete)[-_SIGNATURE_BYTES:]
        stat = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with self._lock:
            if offset and (not reused or offset != reused or cached[7] != stat):
                self._entries[key] = (st.st_dev, st.st_ino, offset, head, before, state, verified_at, stat)
            elif not offset:
                self._entries.pop(key, None)
            if key in self._entries:
                self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)
        if carry:
            # A torn last line counts for this answer only.
            return self._fold_bytes(state, carry)
        return state
