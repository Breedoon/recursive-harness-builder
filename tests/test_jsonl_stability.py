"""Unit tests for TelegramBot._await_jsonl_stability().

Tests the JSONL stability polling method that waits for the parent's JSONL
to stop receiving new entries before forking. Uses mocks to simulate different
JSONL growth patterns (stable, advancing, timeout, empty).
"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_bot(config) -> TelegramBot:
    """Create a TelegramBot with minimal config for testing."""
    return TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)


def _mock_persisted_uuids(uuid_sequence: list[list[str]]):
    """Create a side_effect function that returns UUIDs from a sequence.

    Each call to _persisted_session_uuids pops the next list from the sequence.
    Once exhausted, keeps returning the last list.
    """
    idx = {"i": 0}

    def side_effect(session_id: str) -> list[str]:
        i = min(idx["i"], len(uuid_sequence) - 1)
        idx["i"] += 1
        return uuid_sequence[i]

    return side_effect


def _write_jsonl(path: Path, entries: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            if isinstance(entry, str):
                handle.write(entry + "\n")
            else:
                handle.write(json.dumps(entry) + "\n")


# ── Tests ────────────────────────────────────────────────────────────────


class TestPersistedJsonlUuids:
    """Tests for direct JSONL-path UUID enumeration."""

    def test_reads_uuids_from_explicit_jsonl_path(self, config, tmp_path):
        bot = _make_bot(config)
        path = tmp_path / "explicit.jsonl"
        _write_jsonl(
            path,
            [
                {"type": "queue-operation", "operation": "dequeue"},
                {"uuid": "uuid-a", "sessionId": "source-session"},
                "{not-json",
                {"uuid": "uuid-b", "sessionId": "different-session"},
                {"uuid": "", "sessionId": "source-session"},
            ],
        )

        assert bot._persisted_jsonl_uuids(path) == ["uuid-a", "uuid-b"]

    def test_missing_explicit_jsonl_path_returns_empty(self, config, tmp_path):
        bot = _make_bot(config)

        assert bot._persisted_jsonl_uuids(tmp_path / "missing.jsonl") == []


class TestAwaitJsonlStability:
    """Tests for _await_jsonl_stability method."""

    @pytest.mark.asyncio
    async def test_already_stable_returns_quickly(self, config):
        """When JSONL has entries and doesn't change, returns the last UUID."""
        bot = _make_bot(config)
        # Always return the same list — JSONL is stable
        bot._persisted_session_uuids = MagicMock(return_value=["uuid-a", "uuid-b", "uuid-c"])

        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.3,
            timeout_seconds=5.0,
        )

        assert result == "uuid-c"
        # Should have polled at least twice (initial + one stable check)
        assert bot._persisted_session_uuids.call_count >= 2

    @pytest.mark.asyncio
    async def test_detects_advancing_jsonl(self, config):
        """When JSONL grows, waits for it to stop before returning."""
        bot = _make_bot(config)

        # Simulate JSONL growing: first 3 calls show growth, then stable
        uuid_lists = [
            ["uuid-a"],
            ["uuid-a", "uuid-b"],           # grew
            ["uuid-a", "uuid-b", "uuid-c"], # grew again
            ["uuid-a", "uuid-b", "uuid-c"], # stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
            ["uuid-a", "uuid-b", "uuid-c"], # still stable
        ]
        bot._persisted_session_uuids = MagicMock(side_effect=_mock_persisted_uuids(uuid_lists))

        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.3,
            timeout_seconds=5.0,
        )

        assert result == "uuid-c"
        # Must have been called more than 3 times (3 advancing + stability wait)
        assert bot._persisted_session_uuids.call_count > 3

    @pytest.mark.asyncio
    async def test_timeout_returns_last_uuid(self, config):
        """When JSONL never stops growing, returns last UUID after timeout."""
        bot = _make_bot(config)

        # Every call returns a new UUID — JSONL never stabilizes
        call_count = {"n": 0}

        def always_growing(session_id: str) -> list[str]:
            call_count["n"] += 1
            return [f"uuid-{i}" for i in range(call_count["n"])]

        bot._persisted_session_uuids = MagicMock(side_effect=always_growing)

        start = time.monotonic()
        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.5,
            timeout_seconds=1.0,
        )
        elapsed = time.monotonic() - start

        # Should return the last UUID seen, not None
        assert result is not None
        assert result.startswith("uuid-")
        # Should have taken roughly the timeout duration
        assert elapsed >= 0.9, f"Returned too quickly: {elapsed:.2f}s"
        assert elapsed < 2.0, f"Took too long: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_empty_jsonl_times_out(self, config):
        """When JSONL has no entries, times out and returns None."""
        bot = _make_bot(config)
        bot._persisted_session_uuids = MagicMock(return_value=[])

        start = time.monotonic()
        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.3,
            timeout_seconds=0.5,
        )
        elapsed = time.monotonic() - start

        # Returns None because last_uuid is never set
        assert result is None
        # Should have waited until timeout
        assert elapsed >= 0.4

    @pytest.mark.asyncio
    async def test_uuid_advancement_detected(self, config):
        """When JSONL advances past the initial UUID, the new UUID is returned.

        This tests the integration point: if the caller had source_uuid="uuid-a"
        but JSONL advances to "uuid-c" during the wait, the returned UUID
        is "uuid-c" — enabling the caller to update its fork source.
        """
        bot = _make_bot(config)

        uuid_lists = [
            ["uuid-a"],                     # initial state (what caller expected)
            ["uuid-a", "uuid-b"],           # advanced
            ["uuid-a", "uuid-b", "uuid-c"], # advanced more
            ["uuid-a", "uuid-b", "uuid-c"], # stable now
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
        ]
        bot._persisted_session_uuids = MagicMock(side_effect=_mock_persisted_uuids(uuid_lists))

        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.3,
            timeout_seconds=5.0,
        )

        # The returned UUID should be the latest, not the initial
        assert result == "uuid-c"
        # Caller can now compare result != "uuid-a" and update source_uuid

    @pytest.mark.asyncio
    async def test_short_stability_duration(self, config):
        """With a very short stable_duration, returns as soon as JSONL pauses."""
        bot = _make_bot(config)
        bot._persisted_session_uuids = MagicMock(return_value=["uuid-only"])

        start = time.monotonic()
        result = await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.01,  # Very short
            timeout_seconds=5.0,
        )
        elapsed = time.monotonic() - start

        assert result == "uuid-only"
        # Should be fast — just initial poll + one 150ms sleep + stability check
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_poll_interval_is_150ms(self, config):
        """Verify the method polls at approximately 150ms intervals."""
        bot = _make_bot(config)
        timestamps = []

        original_return = ["uuid-a"]

        def tracking_uuids(session_id: str) -> list[str]:
            timestamps.append(time.monotonic())
            return original_return

        bot._persisted_session_uuids = MagicMock(side_effect=tracking_uuids)

        await bot._await_jsonl_stability(
            session_id="test-session",
            stable_duration=0.5,
            timeout_seconds=2.0,
        )

        # Check intervals between calls are roughly 150ms
        if len(timestamps) >= 3:
            intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
            # Skip first interval (may be shorter — initial poll before first sleep)
            check_intervals = intervals[1:] if len(intervals) > 1 else intervals
            for interval in check_intervals:
                assert 0.1 <= interval <= 0.3, (
                    f"Poll interval {interval:.3f}s outside expected range [0.1, 0.3]"
                )

    @pytest.mark.asyncio
    async def test_session_id_passed_to_persisted_uuids(self, config):
        """The correct session_id is passed through to _persisted_session_uuids."""
        bot = _make_bot(config)
        bot._persisted_session_uuids = MagicMock(return_value=["uuid-x"])

        await bot._await_jsonl_stability(
            session_id="my-special-session-id",
            stable_duration=0.01,
            timeout_seconds=1.0,
        )

        # Every call should use the provided session_id
        for call in bot._persisted_session_uuids.call_args_list:
            assert call[0][0] == "my-special-session-id" or call[1].get("session_id") == "my-special-session-id" or call.args[0] == "my-special-session-id"

    @pytest.mark.asyncio
    async def test_jsonl_path_reads_explicit_path(self, config, tmp_path):
        """An explicit JSONL path is polled directly instead of via session lookup."""
        bot = _make_bot(config)
        path = tmp_path / "explicit-source.jsonl"
        _write_jsonl(path, [{"uuid": "uuid-a"}, {"uuid": "uuid-b"}])
        bot._persisted_session_uuids = MagicMock(side_effect=AssertionError("session lookup used"))

        result = await bot._await_jsonl_stability(
            jsonl_path=path,
            stable_duration=0.01,
            timeout_seconds=1.0,
        )

        assert result == "uuid-b"
        bot._persisted_session_uuids.assert_not_called()

    @pytest.mark.asyncio
    async def test_jsonl_path_detects_advancing_jsonl(self, config, tmp_path):
        """Path-mode stability waits for the supplied JSONL path to stop growing."""
        bot = _make_bot(config)
        path = tmp_path / "growing-source.jsonl"
        uuid_lists = [
            ["uuid-a"],
            ["uuid-a", "uuid-b"],
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
            ["uuid-a", "uuid-b", "uuid-c"],
        ]
        bot._persisted_jsonl_uuids = MagicMock(side_effect=_mock_persisted_uuids(uuid_lists))
        bot._persisted_session_uuids = MagicMock(side_effect=AssertionError("session lookup used"))

        result = await bot._await_jsonl_stability(
            jsonl_path=path,
            stable_duration=0.3,
            timeout_seconds=5.0,
        )

        assert result == "uuid-c"
        assert bot._persisted_jsonl_uuids.call_count > 3
        for call in bot._persisted_jsonl_uuids.call_args_list:
            assert call.args[0] == path
        bot._persisted_session_uuids.assert_not_called()

    @pytest.mark.asyncio
    async def test_jsonl_path_timeout_returns_last_uuid(self, config, tmp_path):
        """When an explicit path never stabilizes, return the latest UUID seen."""
        bot = _make_bot(config)
        path = tmp_path / "always-growing.jsonl"
        call_count = {"n": 0}

        def always_growing(jsonl_path: Path) -> list[str]:
            call_count["n"] += 1
            return [f"uuid-{i}" for i in range(call_count["n"])]

        bot._persisted_jsonl_uuids = MagicMock(side_effect=always_growing)

        result = await bot._await_jsonl_stability(
            jsonl_path=path,
            stable_duration=0.5,
            timeout_seconds=1.0,
        )

        assert result is not None
        assert result.startswith("uuid-")

    @pytest.mark.asyncio
    async def test_requires_exactly_one_source(self, config, tmp_path):
        """The generalized stability waiter requires one source mode."""
        bot = _make_bot(config)
        path = tmp_path / "source.jsonl"

        with pytest.raises(ValueError, match="Exactly one of session_id or jsonl_path is required"):
            await bot._await_jsonl_stability(timeout_seconds=0.01)

        with pytest.raises(ValueError, match="Exactly one of session_id or jsonl_path is required"):
            await bot._await_jsonl_stability(
                session_id="session-a",
                jsonl_path=path,
                timeout_seconds=0.01,
            )
