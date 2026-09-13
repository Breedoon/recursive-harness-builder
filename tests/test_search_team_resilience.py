"""Search regressions for incomplete trees and malformed persisted metadata.

These tests replace the transport and identity-storage boundaries, not the
search implementation. They never launch an agent or contact a model provider.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from obs_agent import tools


TEAM_NAME = "2026-09-13-12-00-root"
ROOT_LINEAGE = ("Root",)
CHILD_LINEAGE = ("Root", "Worker")
GRANDCHILD_LINEAGE = ("Root", "Worker", "Leaf")
CHILD_NAME = "aaaaaaaaaa-worker"
GRANDCHILD_NAME = "bbbbbbbbbb-leaf"


def _known_agent_name(lineage, *, team_key):
    known_names = {
        ROOT_LINEAGE: team_key,
        CHILD_LINEAGE: CHILD_NAME,
        GRANDCHILD_LINEAGE: GRANDCHILD_NAME,
    }
    return known_names[tuple(lineage)]


def _known_lineage_fingerprint(lineage):
    return {
        ROOT_LINEAGE: "aaaaaaaaaa",
        CHILD_LINEAGE: "bbbbbbbbbb",
        GRANDCHILD_LINEAGE: "cccccccccc",
    }[tuple(lineage)]


@pytest.fixture
def search_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide independent caller, projection, and live-provider records."""
    bootstrap = SimpleNamespace(
        root_team_key=TEAM_NAME, agent_name=TEAM_NAME, lineage=ROOT_LINEAGE
    )
    projection = {
        TEAM_NAME: {
            "agent_name": TEAM_NAME,
            "team_name": TEAM_NAME,
            "lineage": list(ROOT_LINEAGE),
            "last_activity": 100.0,
        },
        CHILD_NAME: {
            "agent_name": CHILD_NAME,
            "team_name": TEAM_NAME,
            "lineage": list(CHILD_LINEAGE),
            "last_activity": 50.0,
        },
    }
    runtime: dict[str, dict[str, Any]] = {}
    hook_state = SimpleNamespace(
        pending_obs_bootstrap_xml=None,
        session_id="caller-session",
        team_status_provider=lambda **kwargs: runtime,
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(tools, "_cursor_store", OrderedDict())
    monkeypatch.setattr(tools, "find_latest_obs_bootstrap_for_session", lambda **kwargs: bootstrap)
    monkeypatch.setattr(tools, "_load_team_projection_metadata", lambda team_name: projection)
    monkeypatch.setattr(tools, "find_session_jsonl_index", lambda **kwargs: {})
    monkeypatch.setattr(tools, "agent_name_for_lineage", _known_agent_name)
    monkeypatch.setattr(tools, "lineage_fingerprint", _known_lineage_fingerprint)
    monkeypatch.setattr(tools, "normalize_lineage_name", lambda name: name)
    monkeypatch.setattr(tools, "create_sdk_mcp_server", lambda name, tools: tools)
    registered_tools = tools.create_obs_tools(
        SimpleNamespace(vault_path=tmp_path), lambda: "caller-session", hook_state
    )
    handler = next(tool.handler for tool in registered_tools if tool.name == "search_team")
    return SimpleNamespace(
        call=lambda arguments: asyncio.run(handler(arguments)),
        bootstrap=bootstrap,
        projection=projection,
        runtime=runtime,
        hook_state=hook_state,
    )


INVALID_TIMESTAMPS = [
    pytest.param(True, id="boolean"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(10**400, id="integer-overflow"),
    pytest.param(1e100, id="finite-positive-out-of-range"),
    pytest.param(-1e100, id="finite-negative-out-of-range"),
    pytest.param(253_402_300_800.0, id="year-10000"),
    pytest.param(-62_135_596_801.0, id="year-zero"),
    pytest.param("0001-01-01T00:00:00+01:00", id="offset-before-year-one"),
    pytest.param("9999-12-31T23:59:59-01:00", id="offset-after-year-9999"),
]


@pytest.mark.parametrize("value", INVALID_TIMESTAMPS)
def test_event_timestamp_must_be_representable_in_utc(value: object) -> None:
    assert tools._parse_jsonl_event_timestamp(value) is None


@pytest.mark.parametrize("value", INVALID_TIMESTAMPS)
def test_invalid_activity_does_not_hide_a_valid_fallback(value: object) -> None:
    assert tools._activity_fallback({"last_activity": value, "created_at": 42.0}) == (
        42.0, "projection_timestamp"
    )


@pytest.mark.parametrize("value", INVALID_TIMESTAMPS)
def test_invalid_activity_sorts_after_known_activity(value: object) -> None:
    malformed = {"team_name": TEAM_NAME, "last_activity_epoch": value}
    valid = {"team_name": TEAM_NAME, "last_activity_epoch": 0.0}
    assert tools._member_activity_sort_key(valid, "z") < tools._member_activity_sort_key(malformed, "a")


@pytest.mark.parametrize("value", [0, -1, 42.125, 1_800_000_000.5])
def test_epoch_activity_preserves_seconds_and_fraction(value: float) -> None:
    assert tools._parse_jsonl_event_timestamp(value) == float(value)


def test_activity_accepts_timezone_aware_strings_and_legacy_milliseconds() -> None:
    epoch = datetime(2026, 9, 13, 12, tzinfo=timezone.utc).timestamp()
    assert tools._parse_jsonl_event_timestamp("2026-09-13T14:00:00+02:00") == epoch
    assert tools._activity_fallback({"updated_at": epoch * 1000}) == (
        epoch, "projection_timestamp"
    )


def test_invalid_newer_event_does_not_hide_latest_valid_event(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        '\n'.join(json.dumps(record) for record in [
            {"timestamp": 100.0}, {"timestamp": 1e100}, {"timestamp": 50.0},
        ]) + '\n',
        encoding="utf-8",
    )
    assert tools._jsonl_event_activity(session_file) == 100.0


@pytest.mark.parametrize("payload", [None, [], 123, "not-an-object"])
def test_wrong_shape_team_configuration_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / ".claude" / "teams" / TEAM_NAME / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    assert tools._load_team_projection_metadata(TEAM_NAME) == {}


@pytest.mark.parametrize("value", INVALID_TIMESTAMPS)
def test_search_survives_bad_runtime_activity(search_fixture, value: object) -> None:
    search_fixture.projection[CHILD_NAME]["last_activity"] = value
    search_fixture.projection[CHILD_NAME]["created_at"] = 42.0
    response = search_fixture.call({"mode": "tree"})
    assert not response.get("is_error"), response
    member = next(row for row in response["tool_use_result"]["members"] if row["agent_name"] == CHILD_NAME)
    assert member["last_activity_epoch"] == 42.0


@pytest.mark.parametrize("field", ["session_id", "runtime_status"])
@pytest.mark.parametrize("value", [["malformed"], {"malformed": True}])
def test_search_survives_unhashable_provider_fields(search_fixture, field: str, value: object) -> None:
    search_fixture.runtime[CHILD_NAME] = {
        "agent_name": CHILD_NAME,
        "team_name": TEAM_NAME,
        field: value,
    }
    response = search_fixture.call({"mode": "tree"})
    assert not response.get("is_error"), response
    member = next(row for row in response["tool_use_result"]["members"] if row["agent_name"] == CHILD_NAME)
    if field == "session_id":
        assert "session_id" not in member
    else:
        assert member["runtime_status"] == "unknown"


def test_ancestor_search_skips_missing_intermediate_records(search_fixture) -> None:
    del search_fixture.projection[CHILD_NAME]
    search_fixture.bootstrap.agent_name = GRANDCHILD_NAME
    search_fixture.bootstrap.lineage = GRANDCHILD_LINEAGE
    response = search_fixture.call({"mode": "ancestors"})
    assert not response.get("is_error"), response
    assert response["tool_use_result"]["ancestors"] == [TEAM_NAME]


def test_ancestor_search_with_no_retained_ancestors_is_empty(search_fixture) -> None:
    search_fixture.projection.clear()
    search_fixture.bootstrap.agent_name = GRANDCHILD_NAME
    search_fixture.bootstrap.lineage = GRANDCHILD_LINEAGE
    response = search_fixture.call({"mode": "ancestors"})
    assert not response.get("is_error"), response
    assert response["tool_use_result"]["ancestors"] == []


def test_on_behalf_search_preserves_caller_identity_across_pages(search_fixture) -> None:
    first_page = search_fixture.call({"mode": "tree", "agent_name": CHILD_NAME, "limit": 1})
    first = first_page["tool_use_result"]
    assert first["current_agent"] == TEAM_NAME
    assert first["target_agent"] == CHILD_NAME
    second = search_fixture.call({"cursor": first["next_cursor"]})["tool_use_result"]
    assert second["current_agent"] == TEAM_NAME
    assert second["target_agent"] == CHILD_NAME


def test_cursor_freezes_nested_provider_metadata(search_fixture) -> None:
    live_lineage = list(CHILD_LINEAGE)
    search_fixture.runtime[CHILD_NAME] = {
        "agent_name": CHILD_NAME,
        "team_name": TEAM_NAME,
        "lineage": live_lineage,
    }
    first = search_fixture.call({"mode": "tree", "limit": 1})["tool_use_result"]
    live_lineage.append("Later mutation")
    second = search_fixture.call({"cursor": first["next_cursor"]})["tool_use_result"]
    assert second["members"][0]["lineage"] == list(CHILD_LINEAGE)
    assert second["members"][0]["lineage_length"] == len(CHILD_LINEAGE)


@pytest.mark.parametrize("arguments", [{"limit": 1.5}, {"offset": 0.5}, {"offset": -0.5}])
def test_fractional_pagination_is_rejected(search_fixture, arguments: dict) -> None:
    response = search_fixture.call({"mode": "tree", **arguments})
    assert response.get("is_error") is True
    assert "integer" in response["content"][0]["text"]


def test_jsonl_event_activity_wins_over_mtime(search_fixture, tmp_path: Path, monkeypatch) -> None:
    import os

    session_file = tmp_path / "activity.jsonl"
    session_file.write_text('{"timestamp": 30.125}\n{"timestamp": 1e100}\n', encoding="utf-8")
    os.utime(session_file, (10_000, 10_000))
    search_fixture.projection[CHILD_NAME]["session_id"] = "child-session"
    monkeypatch.setattr(tools, "find_session_jsonl_index", lambda **kwargs: {"child-session": session_file})
    response = search_fixture.call({"mode": "tree"})["tool_use_result"]
    child = next(row for row in response["members"] if row["agent_name"] == CHILD_NAME)
    assert child["last_activity_epoch"] == 30.125
    assert child["last_activity_source"] == "jsonl_event_timestamp"


def test_provider_exception_is_not_retried_as_a_signature_mismatch(search_fixture) -> None:
    calls = []

    def broken_provider(team_name):
        calls.append(team_name)
        raise TypeError("internal provider bug")

    search_fixture.hook_state.team_status_provider = broken_provider
    response = search_fixture.call({"mode": "tree"})
    assert not response.get("is_error"), response
    assert calls == [TEAM_NAME]


def test_positional_only_provider_remains_supported(search_fixture) -> None:
    def positional_provider(team_name, /):
        return {CHILD_NAME: {"team_name": team_name, "agent_name": CHILD_NAME, "running": True}}

    search_fixture.hook_state.team_status_provider = positional_provider
    response = search_fixture.call({"mode": "tree", "running_only": True})["tool_use_result"]
    assert [member["agent_name"] for member in response["members"]] == [CHILD_NAME]


@pytest.mark.parametrize("raw", ["[" * 2000 + "]" * 2000, '{"timestamp":' + "9" * 5000 + "}"])
def test_unparseable_json_record_does_not_abort_activity_scan(tmp_path: Path, raw: str) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(raw + '\n{"timestamp": 77.5}\n', encoding="utf-8")
    assert tools._jsonl_event_activity(session_file) == 77.5


@pytest.mark.parametrize("modified_at", [float("nan"), float("inf"), 1e100])
def test_invalid_mtime_uses_runtime_fallback(search_fixture, monkeypatch, modified_at: float) -> None:
    class EmptyTranscript:
        def open(self, *args, **kwargs):
            from io import StringIO
            return StringIO("")

        def stat(self):
            return SimpleNamespace(st_mtime=modified_at)

    search_fixture.projection[CHILD_NAME]["session_id"] = "child-session"
    monkeypatch.setattr(
        tools, "find_session_jsonl_index", lambda **kwargs: {"child-session": EmptyTranscript()}
    )
    response = search_fixture.call({"mode": "tree"})["tool_use_result"]
    child = next(row for row in response["members"] if row["agent_name"] == CHILD_NAME)
    assert child["last_activity_epoch"] == 50.0
    assert child["last_activity_source"] == "runtime_last_activity"


def test_whitespace_session_identity_resolves_its_transcript(search_fixture, tmp_path: Path, monkeypatch) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text('{"timestamp": 77.5}\n', encoding="utf-8")
    search_fixture.projection[CHILD_NAME]["session_id"] = "  child-session  "

    def find_paths(*, session_ids, cwd):
        assert session_ids == {"child-session"}
        return {"child-session": session_file}

    monkeypatch.setattr(tools, "find_session_jsonl_index", find_paths)
    response = search_fixture.call({"mode": "tree"})["tool_use_result"]
    child = next(row for row in response["members"] if row["agent_name"] == CHILD_NAME)
    assert child["session_id"] == "child-session"
    assert child["last_activity_epoch"] == 77.5


@pytest.mark.parametrize("limit", [1, "1", 1.0])
def test_integer_compatible_page_sizes_still_work(search_fixture, limit: object) -> None:
    response = search_fixture.call({"mode": "tree", "limit": limit})["tool_use_result"]
    assert response["returned"] == 1
    assert response["has_more"] is True


def test_activity_filter_keeps_explicit_bounds_and_running_requirement(search_fixture) -> None:
    search_fixture.runtime[CHILD_NAME] = {
        "agent_name": CHILD_NAME, "team_name": TEAM_NAME, "running": True,
    }
    response = search_fixture.call({
        "mode": "tree", "activity_after": 49.0, "activity_before": 51.0,
        "running_only": True,
    })["tool_use_result"]
    assert [row["agent_name"] for row in response["members"]] == [CHILD_NAME]
    excluded = search_fixture.call({"mode": "tree", "activity_after": 50.0, "activity_before": 51.0})
    assert excluded["tool_use_result"]["members"] == []


def test_no_activity_is_unknown_and_filtered_out(search_fixture) -> None:
    search_fixture.projection[CHILD_NAME].pop("last_activity")
    response = search_fixture.call({"mode": "tree"})["tool_use_result"]
    child = next(row for row in response["members"] if row["agent_name"] == CHILD_NAME)
    assert child["last_activity_source"] == "unknown"
    assert "last_activity_epoch" not in child
    filtered = search_fixture.call({"mode": "tree", "activity_after": -1})["tool_use_result"]
    assert [row["agent_name"] for row in filtered["members"]] == [TEAM_NAME]


def test_cursor_continuation_does_not_read_live_metadata_again(search_fixture, monkeypatch) -> None:
    first = search_fixture.call({"mode": "tree", "limit": 1})["tool_use_result"]

    def unavailable_projection(team_name):
        raise AssertionError("continuation must use its frozen snapshot")

    monkeypatch.setattr(tools, "_load_team_projection_metadata", unavailable_projection)
    second = search_fixture.call({"cursor": first["next_cursor"]})["tool_use_result"]
    assert second["returned"] == 1
    assert second["has_more"] is False
    assert second["members"][0]["agent_name"] == CHILD_NAME


def test_json_decoder_recursion_error_does_not_abort_activity_scan(tmp_path: Path, monkeypatch) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text('too-deep\n{"timestamp": 77.5}\n', encoding="utf-8")
    real_loads = json.loads

    def bounded_decoder(raw):
        if raw == "too-deep":
            raise RecursionError("nested JSON exceeds decoder recursion limit")
        return real_loads(raw)

    monkeypatch.setattr(tools.json, "loads", bounded_decoder)
    assert tools._jsonl_event_activity(session_file) == 77.5
