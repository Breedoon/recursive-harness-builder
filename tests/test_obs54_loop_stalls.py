"""obs-54: event-loop stalls from O(records x routes) pruning and whole-file JSONL scans.

Each fix keeps the old result and removes the repeated work:
- ``_collect_chain_unsafe`` gives the same set as the old per-row walk;
- the idle sweep and team status take one pass over the routes;
- the bootstrap and event-activity scans read only appended bytes;
- the inbox wake poller reuses a parsed, settled inbox;
- team-config member upserts are not rewritten on every stream event;
- a launch whose notices time out goes on instead of leaving an empty topic.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, TimedOut

from obs_agent import jsonl_health, lineage, tools
from obs_agent.append_scan import AppendScanCache
from obs_agent.loop_monitor import LoopStallMonitor
from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord

_TEST_GAP = 0.05


# --- _collect_chain_unsafe ---------------------------------------------------


def _old_collect_chain_unsafe(*, by_uuid, poison_uuids):
    """The pre-obs-54 implementation, kept as the oracle."""
    unsafe = set(poison_uuids)
    for uuid_value in by_uuid:
        seen = set()
        cursor = uuid_value
        while cursor:
            if cursor in unsafe:
                unsafe.add(uuid_value)
                break
            if cursor in seen:
                unsafe.add(uuid_value)
                break
            seen.add(cursor)
            entry = by_uuid.get(cursor)
            if entry is None:
                break
            cursor = entry.parent_uuid
    return frozenset(unsafe)


@dataclass
class _Row:
    parent_uuid: str | None


def _random_graph(rng: random.Random, n: int) -> dict[str, _Row]:
    ids = [f"u{i}" for i in range(n)]
    rows: dict[str, _Row] = {}
    for i, uid in enumerate(ids):
        roll = rng.random()
        if i == 0 or roll < 0.05:
            parent = None
        elif roll < 0.08:
            parent = f"missing-{i}"  # parent row not in the file
        elif roll < 0.11:
            parent = rng.choice(ids)  # anywhere: may close a cycle or self-loop
        else:
            parent = ids[i - 1] if rng.random() < 0.8 else rng.choice(ids[:i])
        rows[uid] = _Row(parent_uuid=parent)
    order = list(rows.items())
    rng.shuffle(order)  # the CLI's row order is not the chain order
    return dict(order)


def test_collect_chain_unsafe_matches_the_old_walk():
    rng = random.Random(54)
    for _ in range(300):
        rows = _random_graph(rng, rng.randint(1, 120))
        ids = list(rows)
        for poison in (
            set(),
            set(rng.sample(ids, min(len(ids), rng.randint(1, 4)))),
            {"not-in-file"},
            set(ids),
        ):
            assert jsonl_health._collect_chain_unsafe(
                by_uuid=rows, poison_uuids=set(poison),
            ) == _old_collect_chain_unsafe(by_uuid=rows, poison_uuids=set(poison))


def test_collect_chain_unsafe_marks_cycles_and_their_descendants():
    rows = {
        "a": _Row("b"), "b": _Row("a"),  # two-row cycle
        "c": _Row("a"),  # leads into it
        "s": _Row("s"),  # self loop
        "r": _Row(None), "k": _Row("r"),  # clean chain
    }
    assert jsonl_health._collect_chain_unsafe(by_uuid=rows, poison_uuids=set()) == {"a", "b", "c", "s"}


def test_collect_chain_unsafe_is_linear_on_a_long_chain():
    n = 20_000
    rows = {f"u{i}": _Row(f"u{i - 1}" if i else None) for i in range(n)}
    started = time.perf_counter()
    unsafe = jsonl_health._collect_chain_unsafe(by_uuid=rows, poison_uuids={"u10000"})
    elapsed = time.perf_counter() - started
    assert unsafe == {f"u{i}" for i in range(10_000, n)}
    # The old walk needs ~n^2/2 = 2e8 steps here (minutes); the new one ~n.
    assert elapsed < 1.0


# --- AppendScanCache ----------------------------------------------------------


def _full_fold(path: Path, fold, initial):
    state = initial
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            state = fold(state, line.strip())
    return state


def _collect(lines: tuple, line: str) -> tuple:
    return lines + (line,) if line else lines


def test_append_scan_reads_only_appended_bytes(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"one\ntwo\n")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    assert cache.scan(path) == ("one", "two")
    with path.open("ab") as handle:
        handle.write(b"three\n")
    assert cache.scan(path) == ("one", "two", "three")
    assert (cache.full_scans, cache.incremental_scans) == (1, 1)
    assert cache.scan(path) == ("one", "two", "three")
    assert cache.incremental_scans == 2


def test_append_scan_torn_last_line_counts_now_but_is_not_stored(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"one\ntw")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    assert cache.scan(path) == ("one", "tw")
    with path.open("ab") as handle:
        handle.write(b"o\n")
    assert cache.scan(path) == ("one", "two")


def test_append_scan_rescans_rewritten_truncated_and_replaced_files(tmp_path):
    path = tmp_path / "s.jsonl"
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    path.write_bytes(b"alpha\nbeta\n")
    assert cache.scan(path) == ("alpha", "beta")
    path.write_bytes(b"ALPHA\nbeta\ngamma\n")  # in place, longer, first bytes changed
    assert cache.scan(path) == ("ALPHA", "beta", "gamma")
    path.write_bytes(b"x\n")  # truncated
    assert cache.scan(path) == ("x",)
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(b"x\ny\n")
    os.replace(replacement, path)  # another inode
    assert cache.scan(path) == ("x", "y")
    assert cache.full_scans == 4


def test_append_scan_universal_newlines_and_needle(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"a\r\nb\rc\n\nkeep-me\n")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    assert cache.scan(path) == _full_fold(path, _collect, ()) == ("a", "b", "c", "keep-me")
    needle_cache: AppendScanCache[tuple] = AppendScanCache(_collect, (), needle="keep")
    assert needle_cache.scan(path) == ("keep-me",)


def test_append_scan_still_raises_on_undecodable_bytes_like_a_text_read(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"ok\n\xff\xfe\n")
    with pytest.raises(UnicodeDecodeError):
        AppendScanCache(_collect, (), needle="never-present").scan(path)
    assert AppendScanCache(_collect, (), errors="replace").scan(path) == ("ok", "\ufffd\ufffd")


def test_append_scan_sees_a_same_size_middle_rewrite_at_once(tmp_path):
    path = tmp_path / "s.jsonl"
    head, tail = "H" * 300, "T" * 300
    path.write_text(f"{head}\nmiddle-one\n{tail}\n", encoding="utf-8")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    assert cache.scan(path) == (head, "middle-one", tail)
    stat = path.stat()
    with path.open("r+b") as handle:  # same inode, same size, same first and last 256 bytes
        handle.seek(len(head) + 1)
        handle.write(b"middle-two")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # even with the old mtime back
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns
    assert cache.scan(path) == (head, "middle-two", tail)
    assert cache.full_scans == 2


def test_append_scan_rescans_in_full_after_the_revalidation_interval(tmp_path, monkeypatch):
    from obs_agent import append_scan

    clock = {"now": 1000.0}
    monkeypatch.setattr(append_scan, "_monotonic", lambda: clock["now"])
    path = tmp_path / "s.jsonl"
    path.write_text("one\ntwo\n", encoding="utf-8")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, (), revalidate_seconds=300)
    assert cache.scan(path) == ("one", "two")
    # Stand-in for a rewrite no cheap check can see: corrupt the stored fold.
    key = str(path)
    entry = cache._entries[key]
    cache._entries[key] = (*entry[:5], ("stale",), *entry[6:])
    clock["now"] += 299
    assert cache.scan(path) == ("stale",)  # still trusted inside the interval
    clock["now"] += 2
    assert cache.scan(path) == ("one", "two")
    assert cache.full_scans == 2


def test_append_scan_matches_a_full_scan_under_random_appends(tmp_path, monkeypatch):
    from obs_agent import append_scan

    monkeypatch.setattr(append_scan, "_BLOCK_BYTES", 7)  # many block boundaries
    rng = random.Random(7)
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"")
    cache: AppendScanCache[tuple] = AppendScanCache(_collect, ())
    for step in range(200):
        chunk = "".join(rng.choice("ab\n\r ") for _ in range(rng.randint(0, 30)))
        with path.open("ab") as handle:
            handle.write(chunk.encode())
        assert cache.scan(path) == _full_fold(path, _collect, ()), step


# --- lineage bootstrap scan ---------------------------------------------------


def _old_find_latest_bootstrap(path: Path):
    latest_xml = None
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            for text in lineage._entry_candidate_texts(entry):
                found = lineage.extract_obs_bootstrap_xml(text)
                if found:
                    latest_xml = found
    return None if latest_xml is None else lineage.parse_obs_bootstrap_xml(latest_xml)


def _bootstrap_row(agent: str) -> str:
    xml = lineage.build_obs_bootstrap_xml(
        lineage=("Trunk", agent),
        origin="agent_task_fresh",
        is_fork=False,
        session_id=None,
        root_team_key="2026-10-25-12-00-trunk",
        agent_name=f"0123456789-{agent.lower()}",
    )
    return json.dumps({"type": "user", "message": {"role": "user", "content": f"hi {xml} there"}})


def test_bootstrap_scan_follows_appends_like_the_full_scan(tmp_path):
    path = tmp_path / "s.jsonl"
    filler = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}})
    path.write_text(_bootstrap_row("First") + "\n" + filler + "\n", encoding="utf-8")
    first = lineage.find_latest_obs_bootstrap_in_jsonl(path)
    assert first == _old_find_latest_bootstrap(path)
    assert first is not None and first.lineage[-1] == "First"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(filler + "\n" + _bootstrap_row("Second") + "\n")
    second = lineage.find_latest_obs_bootstrap_in_jsonl(path)
    assert second == _old_find_latest_bootstrap(path)
    assert second is not None and second.lineage[-1] == "Second"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(filler + "\n")
    assert lineage.find_latest_obs_bootstrap_in_jsonl(path) == second


def test_bootstrap_scan_finds_a_block_spelled_with_json_unicode_escapes(tmp_path):
    path = tmp_path / "s.jsonl"
    row = _bootstrap_row("Escaped")
    # Valid JSON for the same text, with the tag name's letters escaped.
    escaped = row.replace("obs-bootstrap", "\\u006fbs-bootstrap")
    assert json.loads(escaped) == json.loads(row)
    path.write_text(escaped + "\n", encoding="utf-8")
    found = lineage.find_latest_obs_bootstrap_in_jsonl(path)
    assert found == _old_find_latest_bootstrap(path)
    assert found is not None and found.lineage[-1] == "Escaped"


def test_bootstrap_scan_without_any_block(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"user","message":{"role":"user","content":"no block"}}\n', encoding="utf-8")
    assert lineage.find_latest_obs_bootstrap_in_jsonl(path) is None


def test_parse_obs_bootstrap_xml_still_raises_on_bad_xml():
    with pytest.raises(Exception):
        lineage.parse_obs_bootstrap_xml("<obs-bootstrap>")
    with pytest.raises(Exception):
        lineage.parse_obs_bootstrap_xml("<obs-bootstrap>")


# --- search_team activity -----------------------------------------------------


def test_event_activity_is_incremental_and_exact(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps({"timestamp": "2026-10-25T10:00:00Z"}) + "\n" + json.dumps({"timestamp": 50.0}) + "\n",
        encoding="utf-8",
    )
    first = tools._jsonl_event_activity(path)
    assert first == datetime(2026, 10, 25, 10, tzinfo=timezone.utc).timestamp()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": "2026-10-25T09:00:00Z"}) + "\n")  # older: max stays
    assert tools._jsonl_event_activity(path) == first
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": "2026-10-25T11:00:00Z"}) + "\n")
    assert tools._jsonl_event_activity(path) == datetime(2026, 10, 25, 11, tzinfo=timezone.utc).timestamp()
    assert tools._jsonl_event_activity(tmp_path / "missing.jsonl") is None


# --- idle sweep and team status -----------------------------------------------


class _CountingRoutes(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def values(self):
        self.values_calls += 1
        return super().values()


def _record(task_id: str, child_route: TelegramRoute, *, idle_ready: bool = True) -> _ForkTaskRecord:
    return _ForkTaskRecord(
        task_id=task_id,
        tool_use_id=None,
        parent_route=TelegramRoute(chat_id=-100, thread_id=None),
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_route=child_route,
        child_session_id=f"sid-{task_id}",
        prompt="p",
        description="d",
        timeout_ms=None,
        max_turns=None,
        launch_parent_message_id=None,
        launch_child_message_id=None,
        is_fork=False,
        team_name="2026-10-25-12-00-trunk",
        agent_name=f"0123456789-{task_id}",
        idle_ready=idle_ready,
        status="completed",
    )


def test_idle_sweep_takes_one_pass_over_the_routes(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    for i in range(200):
        route = TelegramRoute(chat_id=-100, thread_id=1000 + i)
        state = bot._get_state(route)
        if i % 7 == 0:
            state.active_fork_task_ids.add(f"t{i}")
        bot._fork_tasks_by_id[f"t{i}"] = _record(f"t{i}", route)
    counting = _CountingRoutes(bot._states_by_route)
    bot._states_by_route = counting
    skips: dict[str, int] = {}
    bot._idle_claude_process_candidates(skip_reasons=skips)
    # Old code: one values() per idle-ready record (200); now one for the set.
    assert counting.values_calls == 1
    assert skips["running"] == len([i for i in range(200) if i % 7 == 0])


def test_running_check_with_and_without_the_precomputed_set_agree(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent = bot._get_state(TelegramRoute(chat_id=-100, thread_id=1))
    parent.active_fork_task_ids.update({"a", "b"})
    records = [_record(task_id, TelegramRoute(chat_id=-100, thread_id=2)) for task_id in ("a", "b", "c")]
    active = bot._active_fork_task_ids()
    for record in records:
        for completing in (None, "a", "c"):
            assert bot._is_fork_task_running(record, completing_task_id=completing) == bot._is_fork_task_running(
                record, completing_task_id=completing, active_task_ids=active,
            )


def test_team_status_takes_one_pass_over_the_routes(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    for i in range(50):
        route = TelegramRoute(chat_id=-100, thread_id=2000 + i)
        bot._get_state(route)
        bot._fork_tasks_by_id[f"s{i}"] = _record(f"s{i}", route)
    bot._get_state(TelegramRoute(chat_id=-100, thread_id=1999)).active_fork_task_ids.add("s3")
    counting = _CountingRoutes(bot._states_by_route)
    bot._states_by_route = counting
    status = bot._make_team_status_provider(TelegramRoute(chat_id=-100, thread_id=None))
    records = status(team_name="2026-10-25-12-00-trunk")
    # one pass for the active set and one for the route projections
    assert counting.values_calls == 2
    assert records[("2026-10-25-12-00-trunk", "0123456789-s3")]["running"] is True
    assert records[("2026-10-25-12-00-trunk", "0123456789-s4")]["running"] is False


# --- inbox wake poller cache --------------------------------------------------


_SETTLE = 0.3  # the tests shorten _INBOX_CACHE_SETTLE_SECONDS (2 s) to this


def _write_inbox(path: Path, items: list[dict], *, age_seconds: float) -> None:
    """Write an inbox with an old mtime; its ctime is now, so wait for it to settle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    if age_seconds:
        time.sleep(_SETTLE + 0.1)


def test_inbox_poll_reuses_a_settled_unchanged_inbox(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("obs_agent.telegram._INBOX_CACHE_SETTLE_SECONDS", _SETTLE)
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._daemon_started_at = time.time() - 3600
    inbox = bot._team_inbox_path("team", "agent")
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    _write_inbox(inbox, [{"from": "a", "text": "hello", "timestamp": fresh, "read": False}], age_seconds=60)
    reads = {"n": 0}
    original = bot._latest_unread_team_inbox_message

    def counting(**kwargs):
        reads["n"] += 1
        return original(**kwargs)

    monkeypatch.setattr(bot, "_latest_unread_team_inbox_message", counting)
    expected = ("a", None, "hello")
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == expected
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == expected
    assert reads["n"] == 1
    # Marking it read changes the file: re-read, now nothing to wake for.
    _write_inbox(inbox, [{"from": "a", "text": "hello", "timestamp": fresh, "read": True}], age_seconds=30)
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") is None
    assert reads["n"] == 2
    inbox.unlink()
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") is None


def test_inbox_poll_does_not_store_a_just_written_inbox(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("obs_agent.telegram._INBOX_CACHE_SETTLE_SECONDS", _SETTLE)
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._daemon_started_at = time.time() - 3600
    inbox = bot._team_inbox_path("team", "agent")
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_inbox(inbox, [{"from": "a", "text": "x", "timestamp": fresh, "read": False}], age_seconds=0)
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == ("a", None, "x")
    assert inbox not in bot._inbox_latest_cache
    # A same-size rewrite within the coarse mtime tick is still seen.
    stat = inbox.stat()
    inbox.write_text(json.dumps([{"from": "b", "text": "y", "timestamp": fresh, "read": False}]), encoding="utf-8")
    os.utime(inbox, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == ("b", None, "y")


def test_inbox_poll_sees_a_same_size_rewrite_that_restores_the_old_mtime(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("obs_agent.telegram._INBOX_CACHE_SETTLE_SECONDS", _SETTLE)
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._daemon_started_at = time.time() - 3600
    inbox = bot._team_inbox_path("team", "agent")
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    _write_inbox(inbox, [{"from": "a", "text": "old", "timestamp": fresh, "read": False}], age_seconds=60)
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == ("a", None, "old")
    assert inbox in bot._inbox_latest_cache
    stat = inbox.stat()
    inbox.write_text(json.dumps([{"from": "b", "text": "new", "timestamp": fresh, "read": False}]), encoding="utf-8")
    os.utime(inbox, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size, same mtime as before
    assert inbox.stat().st_size == stat.st_size and inbox.stat().st_mtime_ns == stat.st_mtime_ns
    assert bot._cached_latest_unread_team_inbox_message(team_name="team", agent_name="agent") == ("b", None, "new")


# --- team projection config ---------------------------------------------------


def test_team_projection_upsert_is_not_rewritten_every_event(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    config_path = tmp_path / ".claude" / "teams" / "team" / "config.json"
    meta = {"display_name": "Agent", "lineage": ["Trunk", "Agent"], "updated_at": 1}
    bot._upsert_team_projection_config(team_name="team", agent_name="agent", child_session_id="sid", obs_metadata=meta)
    first = config_path.read_text(encoding="utf-8")
    writes = {"n": 0}
    real_write = Path.write_text

    def counting_write(self, *args, **kwargs):
        if self == config_path:
            writes["n"] += 1
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", counting_write)
    for stamp in range(2, 6):  # only the volatile stamp changes
        bot._upsert_team_projection_config(
            team_name="team", agent_name="agent", child_session_id="sid", obs_metadata={**meta, "updated_at": stamp},
        )
    assert writes["n"] == 0
    assert config_path.read_text(encoding="utf-8") == first
    bot._upsert_team_projection_config(
        team_name="team", agent_name="agent", child_session_id="sid2", obs_metadata=meta,
    )
    assert writes["n"] == 1  # real change: written
    config_path.unlink()
    bot._upsert_team_projection_config(
        team_name="team", agent_name="agent", child_session_id="sid2", obs_metadata=meta,
    )
    assert writes["n"] == 2 and config_path.exists()  # file gone: written again
    fingerprint, _written_at, size = bot._team_projection_written[("team", "agent")]
    bot._team_projection_written[("team", "agent")] = (fingerprint, time.monotonic() - 31, size)
    bot._upsert_team_projection_config(
        team_name="team", agent_name="agent", child_session_id="sid2", obs_metadata=meta,
    )
    assert writes["n"] == 3  # refresh interval passed
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    names = [member["name"] for member in payload["members"]]
    assert names == ["team-lead", "agent"]


def test_team_projection_member_removed_outside_obs_is_written_back_at_once(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    config_path = tmp_path / ".claude" / "teams" / "team" / "config.json"
    meta = {"display_name": "Agent", "lineage": ["Trunk", "Agent"], "updated_at": 1}
    bot._upsert_team_projection_config(team_name="team", agent_name="agent", child_session_id="sid", obs_metadata=meta)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["members"] = [m for m in payload["members"] if m["name"] != "agent"]
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")  # smaller now
    bot._upsert_team_projection_config(team_name="team", agent_name="agent", child_session_id="sid", obs_metadata=meta)
    names = [m["name"] for m in json.loads(config_path.read_text(encoding="utf-8"))["members"]]
    assert names == ["team-lead", "agent"]


def test_team_projection_other_members_upserts_do_not_force_rewrites(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    config_path = tmp_path / ".claude" / "teams" / "team" / "config.json"
    meta = {"display_name": "A", "lineage": ["Trunk", "A"], "updated_at": 1}
    bot._upsert_team_projection_config(team_name="team", agent_name="a", child_session_id="sa", obs_metadata=meta)
    bot._upsert_team_projection_config(team_name="team", agent_name="b", child_session_id="sb", obs_metadata=meta)
    before = config_path.read_text(encoding="utf-8")
    bot._upsert_team_projection_config(team_name="team", agent_name="a", child_session_id="sa", obs_metadata=meta)
    assert config_path.read_text(encoding="utf-8") == before  # a's entry is still current
    names = [m["name"] for m in json.loads(before)["members"]]
    assert names == ["team-lead", "a", "b"]


# --- launch notices ---------------------------------------------------------


async def test_launch_survives_child_notice_timeouts_past_the_old_limit(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    state = bot._get_state(route)
    state.last_bot = MagicMock()
    state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=444))
    # Three timeouts were the old abort point (after the topic existed).
    state.last_bot.send_message = AsyncMock(
        side_effect=[TimedOut(), TimedOut(), TimedOut(),
                     MagicMock(message_id=950), MagicMock(message_id=951), MagicMock(message_id=952)]
    )
    state.session_manager.set_session_id("sid-root")
    with patch("obs_agent.telegram.asyncio.sleep", new=AsyncMock()), patch.object(
        bot, "_execute_fork_task", new_callable=AsyncMock,
    ):
        launched = await bot._launch_fork_task(
            route=route,
            args={"prompt": "work", "description": "Stall survivor", "fork": False, "task_tool_name": "AgentTask"},
        )
    task_id = launched["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
    record = bot._fork_tasks_by_id[task_id]
    assert record.child_route == TelegramRoute(chat_id=-10067890, thread_id=444)
    assert record.launch_child_message_id == 950
    assert task_id in state.active_fork_task_ids
    await bot.shutdown()


async def test_launch_goes_on_when_every_notice_times_out(config, caplog):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    state = bot._get_state(route)
    state.last_bot = MagicMock()
    state.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=445))
    # Telegram unreachable for the whole launch: every send times out.
    state.last_bot.send_message = AsyncMock(side_effect=TimedOut())
    state.session_manager.set_session_id("sid-root")
    with caplog.at_level(logging.WARNING, logger="obs_agent.telegram"), patch(
        "obs_agent.telegram.asyncio.sleep", new=AsyncMock(),
    ), patch.object(bot, "_execute_fork_task", new_callable=AsyncMock):
        launched = await bot._launch_fork_task(
            route=route,
            args={"prompt": "work", "description": "Offline launch", "fork": False, "task_tool_name": "AgentTask"},
        )
    task_id = launched["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
    record = bot._fork_tasks_by_id[task_id]
    assert record.child_route.thread_id == 445
    assert record.launch_child_message_id is None
    # No parent message: a synthetic run generation keeps control stops exact.
    assert isinstance(record.launch_parent_message_id, int) and record.launch_parent_message_id < 0
    assert task_id in state.active_fork_task_ids
    # child notice, session marker, parent confirmation: 6 bounded attempts each
    assert state.last_bot.send_message.await_count == 18
    notices = [r.getMessage() for r in caplog.records if "[launch_notice]" in r.getMessage()]
    assert any("child service message" in text for text in notices)
    assert any("parent launch confirmation" in text for text in notices)
    await bot.shutdown()


async def test_launch_notice_still_raises_on_a_bad_request(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    state = bot._get_state(route)
    state.last_bot = MagicMock()
    state.last_bot.send_message = AsyncMock(side_effect=BadRequest("chat_write_forbidden"))
    with pytest.raises(BadRequest):
        await bot._send_task_launch_notice(route=route, bot=state.last_bot, html_text="x", purpose="test")
    await bot.shutdown()


# --- stall monitor summary ----------------------------------------------------


def test_stall_monitor_summary_reports_window_and_totals(caplog):
    loop = MagicMock()
    monitor = LoopStallMonitor(loop, threshold=2.0, interval=0.5, summary=600.0)
    start = monitor._window_started_at
    monitor._last_beat = start
    assert monitor.check_once(now=start + 2.5) == "stall"
    monitor._last_beat = start + 4.0
    assert monitor.check_once(now=start + 4.1) == "recovered"
    assert monitor.summary_once(now=start + 100) is None
    with caplog.at_level(logging.INFO, logger="obs_agent.loop_monitor"):
        line = monitor.summary_once(now=start + 600)
    assert line is not None and "stalls=1" in line and "blocked=4.0s" in line and "max=4.0s" in line
    assert monitor.blocked_seconds == pytest.approx(4.0)
    assert monitor.summary_once(now=start + 1200).startswith("[loop_stall_summary] last 600s: stalls=0 blocked=0.0s")


def test_stall_monitor_splits_a_stall_across_the_windows_it_spans():
    monitor = LoopStallMonitor(MagicMock(), threshold=2.0, interval=0.5, summary=600.0)
    start = monitor._window_started_at
    monitor._last_beat = start + 590  # the loop stops beating here
    assert monitor.check_once(now=start + 595) == "stall"
    first = monitor.summary_once(now=start + 600)
    assert "blocked=10.0s (1.7%)" in first and "(a stall is in progress)" in first
    monitor._last_beat = start + 1300  # recovered: a 710 s stall
    assert monitor.check_once(now=start + 1300.1) == "recovered"
    second = monitor.summary_once(now=start + 1301)
    assert "last 701s: stalls=0 blocked=700.0s (99.9%) max=710.0s" in second
    assert monitor.blocked_seconds == pytest.approx(710.0)
    third = monitor.summary_once(now=start + 1901)
    assert "blocked=0.0s (0.0%)" in third


def test_stall_monitor_does_not_charge_a_closed_window_for_a_late_detected_stall():
    monitor = LoopStallMonitor(MagicMock(), threshold=2.0, interval=0.5, summary=600.0)
    start = monitor._window_started_at
    monitor._last_beat = start + 599.5  # stall starts just before the window closes
    assert monitor.summary_once(now=start + 600) is not None  # not detected yet
    assert monitor.check_once(now=start + 601.6) == "stall"
    monitor._last_beat = start + 604
    assert monitor.check_once(now=start + 604.1) == "recovered"
    line = monitor.summary_once(now=start + 1200)
    assert "blocked=4.0s" in line and "max=4.5s" in line
