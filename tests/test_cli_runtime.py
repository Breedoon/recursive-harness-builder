"""Per-route CLI runtime selection and the orphan reaper (obs-22 increment 12).

Fake binaries and a fake /proc only: no CLI is started and no process is signalled.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from obs_agent import cli_runtime
from obs_agent import maintenance_restart as maint
from obs_agent.session import SessionManager

BUNDLED = cli_runtime.BUNDLED_RUNTIME


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv(cli_runtime.CANARY_PATH_ENV, raising=False)
    monkeypatch.delenv(cli_runtime.RUNTIME_DB_ENV, raising=False)
    monkeypatch.delenv("OBS_CLI_PATH", raising=False)
    cli_runtime._seen_paths.clear()
    cli_runtime._warned.clear()
    # The sessions below go through the cache proxy, as production Claude does.
    monkeypatch.setattr("obs_agent.cache_proxy_lifecycle.should_use_proxy", lambda **kw: True)
    yield
    cli_runtime._seen_paths.clear()
    cli_runtime._warned.clear()


def _binary(tmp_path: Path, name: str = "obs-cli", body: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def _write(config, **fields) -> Path:
    path = cli_runtime.canary_path(config.telegram_state_db_path)
    path.write_text(json.dumps(fields))
    return path


def _manager(config, *, agent="a1", team="t1", model="claude-opus-4-6") -> SessionManager:
    manager = SessionManager(config=config)
    manager.model_override = model
    manager.set_sdk_env_overrides({"CLAUDE_CODE_AGENT_NAME": agent, "CLAUDE_CODE_TEAM_NAME": team})
    return manager


def _db(config) -> Path:
    return cli_runtime.runtime_db_path(config.telegram_state_db_path)


def _selected(config, tmp_path, **extra):
    binary = _binary(tmp_path)
    fields = {"cli_path": str(binary), "agents": ["a1"], "teams": [], "new_sessions_only": True, **extra}
    _write(config, **fields)
    return binary, cli_runtime.runtime_id_for_path(str(binary))


# --- file absent / bad ------------------------------------------------------------------------


def test_absent_file_changes_nothing(config, caplog):
    caplog.set_level(logging.DEBUG, logger="obs_agent.session")
    manager = _manager(config)
    options = manager.create_options()
    assert options.cli_path is None
    assert manager.cli_runtime is None
    manager.set_session_id("s-new")
    assert not _db(config).exists()
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "CLI runtime" in r.getMessage()]


@pytest.mark.parametrize("content", [b"{not json", b"[]", b"", b'{"agents": ["a1"]}'])
def test_bad_file_falls_back_to_bundled_and_warns_once_per_change(config, caplog, content):
    caplog.set_level(logging.WARNING, logger="obs_agent.cli_runtime")
    path = cli_runtime.canary_path(config.telegram_state_db_path)
    path.write_bytes(content)
    manager = _manager(config)
    for _ in range(3):
        assert manager.create_options().cli_path is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not _db(config).exists()


def test_a_changed_bad_file_warns_again_and_a_fixed_one_rearms(config, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="obs_agent.cli_runtime")
    path = cli_runtime.canary_path(config.telegram_state_db_path)
    path.write_bytes(b"{nope")
    manager = _manager(config)
    manager.create_options()
    path.write_bytes(b"{nope2")
    manager.create_options()
    assert len(caplog.records) == 2
    binary = _binary(tmp_path)
    _write(config, cli_path=str(binary), agents=["a1"])
    assert manager.create_options().cli_path == str(binary)
    path.write_bytes(b"{nope")  # the same breakage after a good spell warns again
    manager.create_options()
    assert len(caplog.records) == 3


def test_non_executable_or_missing_cli_path_is_invalid(config, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="obs_agent.cli_runtime")
    plain = tmp_path / "plain"
    plain.write_text("x")
    for bad in (str(plain), str(tmp_path / "missing"), "relative/obs-cli", None, 5):
        _write(config, cli_path=bad, agents=["a1"])
        assert _manager(config).create_options().cli_path is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 5


def test_unreadable_file(config, monkeypatch):
    path = _write(config, cli_path="/x", agents=["a1"])
    real = Path.read_bytes

    def boom(self):
        if self == path:
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert cli_runtime.load_canary(path).state == "invalid"
    assert _manager(config).create_options().cli_path is None


# --- selection ------------------------------------------------------------------------------


def test_match_by_agent(config, tmp_path):
    binary, runtime = _selected(config, tmp_path)
    manager = _manager(config)
    options = manager.create_options()
    assert options.cli_path == str(binary)
    assert manager.cli_runtime == runtime and runtime.startswith("obs-cli:") and len(runtime) == len("obs-cli:") + 12
    assert _manager(config, agent="other").create_options().cli_path is None


def test_match_by_team(config, tmp_path):
    binary = _binary(tmp_path)
    _write(config, cli_path=str(binary), teams=["t1"])
    assert _manager(config, agent="zzz", team="t1").create_options().cli_path == str(binary)
    assert _manager(config, agent="zzz", team="t2").create_options().cli_path is None


def test_unnamed_session_never_matches(config, tmp_path):
    _selected(config, tmp_path)
    manager = SessionManager(config=config)
    manager.model_override = "claude-opus-4-6"
    assert manager.create_options().cli_path is None


@pytest.mark.parametrize("model", ["gpt", "local-qwen3.8-27b", "local-sparks-glm-5.3-flash-uncensored"])
def test_non_claude_route_ignored(config, tmp_path, model):
    _selected(config, tmp_path)
    manager = _manager(config, model=model)
    assert manager.create_options().cli_path is None
    assert manager.cli_runtime == BUNDLED
    manager.set_session_id("s")
    assert not _db(config).exists()


def test_claude_route_not_behind_cache_proxy_ignored(config, tmp_path, monkeypatch):
    _selected(config, tmp_path)
    monkeypatch.setattr("obs_agent.cache_proxy_lifecycle.should_use_proxy", lambda **kw: False)
    assert _manager(config).create_options().cli_path is None
    explicit = _manager(config)
    explicit.set_sdk_env_overrides({"CLAUDE_CODE_AGENT_NAME": "a1", "ANTHROPIC_BASE_URL": "http://elsewhere:1"})
    monkeypatch.setattr("obs_agent.cache_proxy_lifecycle.should_use_proxy", lambda **kw: True)
    assert explicit.create_options().cli_path is None


def test_file_is_reread_at_every_build(config, tmp_path):
    binary, _ = _selected(config, tmp_path)
    manager = _manager(config)
    assert manager.create_options().cli_path == str(binary)
    _write(config, cli_path=str(binary), agents=[])  # rollback: no restart needed
    assert manager.create_options().cli_path is None
    cli_runtime.canary_path(config.telegram_state_db_path).unlink()
    assert manager.create_options().cli_path is None


# --- resume and records -----------------------------------------------------------------------


def test_new_session_is_recorded_when_its_id_is_learned(config, tmp_path):
    binary, runtime = _selected(config, tmp_path)
    manager = _manager(config)
    manager.create_options()
    assert not _db(config).exists()  # id unknown yet
    manager.set_session_id("s-new")
    record = cli_runtime.get_record(_db(config), "s-new")
    assert record == cli_runtime.RuntimeRecord(runtime, str(binary))
    manager.set_session_id("s-new")  # a later call does not rewrite or fail
    assert cli_runtime.known_cli_paths(config.telegram_state_db_path) == {str(binary)}


def test_unselected_session_is_recorded_as_bundled(config, tmp_path):
    _selected(config, tmp_path)
    manager = _manager(config, agent="other")
    manager.create_options()
    manager.set_session_id("s-other")
    assert cli_runtime.get_record(_db(config), "s-other") == cli_runtime.RuntimeRecord(BUNDLED, None)


def _resumable(manager, session_id):
    manager.set_session_id(session_id)
    manager.last_activity = __import__("time").time()


def test_resume_stays_on_recorded_runtime_then_follows_rollback(config, tmp_path, monkeypatch):
    binary, runtime = _selected(config, tmp_path)
    monkeypatch.setattr(SessionManager, "_session_is_unborn", lambda self: False)
    first = _manager(config)
    first.create_options()
    _resumable(first, "s-1")
    again = first.create_options()
    assert again.resume == "s-1" and again.cli_path == str(binary)
    # Canary release moves on: a resumed session stays on the binary that wrote it.
    newer = _binary(tmp_path, "obs-cli-2", b"#!/bin/sh\nexit 1\n")
    _write(config, cli_path=str(newer), agents=["a1"], new_sessions_only=True)
    assert first.create_options().cli_path == str(binary)
    # new_sessions_only false: the conversation moves, the record follows.
    _write(config, cli_path=str(newer), agents=["a1"], new_sessions_only=False)
    moved = first.create_options()
    assert moved.cli_path == str(newer)
    assert cli_runtime.get_record(_db(config), "s-1").cli_path == str(newer)
    # Rollback: the agent leaves the file; its next spawn is bundled even on resume.
    _write(config, cli_path=str(newer), agents=[])
    options = first.create_options()
    assert options.resume == "s-1" and options.cli_path is None
    assert cli_runtime.get_record(_db(config), "s-1").runtime == BUNDLED


def test_resume_of_unrecorded_session_stays_bundled(config, tmp_path, monkeypatch):
    # A session written before the canary existed has no record: 2.1.59 wrote it.
    _selected(config, tmp_path)
    monkeypatch.setattr(SessionManager, "_session_is_unborn", lambda self: False)
    manager = _manager(config)
    _resumable(manager, "s-old")
    options = manager.create_options()
    assert options.resume == "s-old" and options.cli_path is None
    assert cli_runtime.get_record(_db(config), "s-old").runtime == BUNDLED
    # and it keeps doing so on every later build
    assert manager.create_options().cli_path is None


def test_resume_with_vanished_recorded_binary_uses_canary_binary(config, tmp_path, monkeypatch):
    binary, _ = _selected(config, tmp_path)
    monkeypatch.setattr(SessionManager, "_session_is_unborn", lambda self: False)
    cli_runtime.put_record(_db(config), "s-gone", "obs-cli:deadbeef0000", str(tmp_path / "gone"))
    manager = _manager(config)
    _resumable(manager, "s-gone")
    assert manager.create_options().cli_path == str(binary)


def test_fork_inherits_runtime(config, tmp_path, monkeypatch):
    binary, runtime = _selected(config, tmp_path)
    monkeypatch.setenv("OBS_TELEGRAM_STATE_DB_PATH", str(config.telegram_state_db_path))
    cli_runtime.put_record(_db(config), "parent", runtime, str(binary))
    cli_runtime.inherit_record("parent", "child")
    assert cli_runtime.get_record(_db(config), "child") == cli_runtime.RuntimeRecord(runtime, str(binary))
    cli_runtime.inherit_record("no-record", "child-2")  # silent no-op
    assert cli_runtime.get_record(_db(config), "child-2") is None


def test_inherit_without_db_creates_nothing(config, monkeypatch):
    monkeypatch.setenv("OBS_TELEGRAM_STATE_DB_PATH", str(config.telegram_state_db_path))
    cli_runtime.inherit_record("a", "b")
    assert not _db(config).exists()


def test_global_cli_path_stays_when_canary_does_not_select(config, tmp_path):
    # The E2E lane's OBS_CLI_PATH is applied by the harness branch's own code
    # after this one in a merged tree; a pre-set options.cli_path must survive.
    binary, _ = _selected(config, tmp_path, agents=[])
    manager = _manager(config)
    options = manager.create_options()
    assert options.cli_path is None
    other = _binary(tmp_path, "lane-cli", b"#!/bin/sh\n")
    options.cli_path = str(other)
    manager._apply_cli_runtime(options)
    assert options.cli_path == str(other)
    assert manager.cli_runtime == cli_runtime.runtime_id_for_path(str(other))


def test_hash_is_cached_per_path_mtime_and_size(tmp_path):
    binary = _binary(tmp_path)
    first = cli_runtime.runtime_id_for_path(str(binary))
    assert cli_runtime.runtime_id_for_path(str(binary)) == first
    binary.write_bytes(b"#!/bin/sh\nexit 2\n")
    assert cli_runtime.runtime_id_for_path(str(binary)) != first


# --- reaper -----------------------------------------------------------------------------------

CLI = "/workspace/obs/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"


def _proc(root: Path, pid: int, argv: list[str], *, ppid: int, pgid: int) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x) R {ppid} {pgid} {pgid} " + "0 " * 15 + f"{pid * 10}\n")


def test_reaper_recognizes_canary_binary_and_leaves_live_clis_alone(tmp_path):
    canary = "/opt/obs-cli/release/obs-cli"
    _proc(tmp_path, 500, ["/bin/bash", "wrapper.sh"], ppid=1, pgid=500)  # live wrapper
    _proc(tmp_path, 501, [canary, "--output-format"], ppid=1, pgid=400)  # orphan obs-cli
    _proc(tmp_path, 502, [CLI], ppid=1, pgid=400)  # orphan bundled
    _proc(tmp_path, 503, [canary], ppid=600, pgid=500)  # live CLI of the current daemon
    _proc(tmp_path, 504, [canary], ppid=1, pgid=700)  # live CLI of another OBS instance
    _proc(tmp_path, 700, ["/bin/bash", "other.sh"], ppid=1, pgid=700)
    _proc(tmp_path, 505, ["/opt/obs-cli/other/obs-cli"], ppid=1, pgid=400)  # path never named
    _proc(tmp_path, 506, [canary + "x"], ppid=1, pgid=400)  # exact match only
    found = maint.find_orphaned_agent_clis(own_pgid=500, proc_root=tmp_path, canary_paths=frozenset({canary}))
    assert found == [501, 502]
    assert maint.find_orphaned_agent_clis(own_pgid=500, proc_root=tmp_path, canary_paths=frozenset()) == [502]


def test_reaper_learns_paths_from_file_memory_and_persisted_table(config, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_TELEGRAM_STATE_DB_PATH", str(config.telegram_state_db_path))
    canary = _binary(tmp_path, "obs-cli-a")
    old = _binary(tmp_path, "obs-cli-old")
    _write(config, cli_path=str(canary), agents=["a1"])
    cli_runtime.put_record(_db(config), "s", "obs-cli:x", str(old))
    assert cli_runtime.known_cli_paths() == {str(canary), str(old)}
    # A restarted OBS has empty memory; the canary file is removed (full rollback):
    cli_runtime._seen_paths.clear()
    cli_runtime.canary_path().unlink()
    assert cli_runtime.known_cli_paths() == {str(old)}


def test_reap_orphans_end_to_end_with_fake_proc(config, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_TELEGRAM_STATE_DB_PATH", str(config.telegram_state_db_path))
    old = _binary(tmp_path, "obs-cli-old")
    cli_runtime.put_record(_db(config), "s", "obs-cli:x", str(old))
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 501, [str(old)], ppid=1, pgid=400)
    sent = []
    pids = maint.reap_orphaned_agent_clis(
        own_pgid=500, proc_root=proc, kill=lambda p, s: sent.append((p, s)), sleep=lambda _s: None
    )
    assert pids == [501]
    assert [p for p, _ in sent][0] == 501


def test_kill_and_capture_accept_canary_clients_and_still_refuse_others(tmp_path):
    canary = "/opt/obs-cli/release/obs-cli"
    proc = tmp_path
    _proc(proc, 600, ["python"], ppid=1, pgid=500)
    _proc(proc, 601, [canary], ppid=600, pgid=500)
    _proc(proc, 602, ["bash"], ppid=601, pgid=500)
    _proc(proc, 603, ["/usr/bin/claude"], ppid=600, pgid=500)
    cli_runtime._seen_paths.add(canary)
    assert maint.capture_process_tree(601, proc_root=proc) == {601: 6010, 602: 6020}
    sent = []
    assert maint.kill_process_tree(601, proc_root=proc, kill=lambda p, s: sent.append(p)) == [601, 602]
    assert sent == [601, 602]
    with pytest.raises(ValueError):
        maint.kill_process_tree(603, proc_root=proc, kill=lambda p, s: None)
    with pytest.raises(ValueError):
        maint.capture_process_tree(603, proc_root=proc)


def test_corrupt_or_missing_runtime_db_is_empty_and_warns_once(config, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="obs_agent.cli_runtime")
    binary, _ = _selected(config, tmp_path)
    monkeypatch.setattr(SessionManager, "_session_is_unborn", lambda self: False)
    _db(config).write_bytes(b"this is not a sqlite database" * 20)
    manager = _manager(config)
    _resumable(manager, "s-x")
    for _ in range(3):  # resumed but unrecorded => bundled; never fails the spawn
        assert manager.create_options().cli_path is None
    assert cli_runtime.known_cli_paths(config.telegram_state_db_path) >= {str(binary)}
    assert len([r for r in caplog.records if r.name == "obs_agent.cli_runtime"]) == 1
    _db(config).unlink()  # losing the file only sends resumed canary sessions back to bundled
    assert manager.create_options().cli_path is None
