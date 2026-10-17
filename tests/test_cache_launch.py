"""Crash-start selection uses admitted immutable bytes, not the mutable checkout."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from obs_agent import cache_gate, cache_launch
from tests.test_cache_gate import repo, _commit, _report  # noqa: F401

pytestmark = pytest.mark.real_cache_gate


@pytest.fixture
def state(repo, tmp_path, monkeypatch):
    (repo / "src/obs_agent/telegram_main.py").write_text("# fake daemon\n")
    monkeypatch.setenv("OBS_CACHE_GATE_LOG", str(tmp_path / "bypass.jsonl"))
    return tmp_path / "releases"


def select(repo, state, tmp_path, **kwargs):
    return cache_launch.select(repo, state, report_root=tmp_path / "reports", **kwargs)


def test_gated_snapshot_survives_dirty_crash_restart(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    assert first["decision"] == "admitted"
    release = Path(first["selected"])
    assert release != repo
    original = (release / "src/cache_proxy.py").read_bytes()
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nBROKEN = True\n")
    second = select(repo, state, tmp_path)
    assert second["decision"] == "fallback"
    assert second["selected"] == str(release)
    assert (release / "src/cache_proxy.py").read_bytes() == original
    assert "different code identity" in second["reason"]


def test_ungated_committed_change_falls_back(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 99\n")
    assert select(repo, state, tmp_path)["selected"] == first["selected"]


def test_non_sensitive_change_inherits_approval(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    select(repo, state, tmp_path)
    _commit(repo, "src/obs_agent/plain.py", "W = 42\n")
    result = select(repo, state, tmp_path)
    assert result["decision"] == "admitted"
    assert (Path(result["selected"]) / "src/obs_agent/plain.py").read_text() == "W = 42\n"


def test_removed_marker_does_not_hide_change(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    _commit(repo, "src/obs_agent/session.py", "Y = 404\n")
    assert select(repo, state, tmp_path)["selected"] == first["selected"]


def test_dirty_admitted_bytes_are_preserved(repo, state, tmp_path):
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nDIRTY = 1\n")
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    assert first["decision"] == "admitted"
    assert cache_gate.code_identity(Path(first["selected"])) == cache_gate.code_identity(repo)
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nDIRTY = 2\n")
    assert select(repo, state, tmp_path)["decision"] == "fallback"


def test_no_fallback_fails_toward_running_and_logs(repo, state, tmp_path):
    result = select(repo, state, tmp_path)
    assert result["decision"] == "emergency-current"
    assert result["selected"] == str(repo)
    assert not (state / "current.json").exists()
    log = json.loads((tmp_path / "bypass.jsonl").read_text().splitlines()[-1])
    assert log["source"] == "supervisor launch"
    assert "no usable admitted release" in log["reason"]


def test_corrupt_snapshot_cannot_be_trusted(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    (Path(first["selected"]) / "src/cache_proxy.py").write_text("CORRUPT = 1\n")
    _commit(repo, "src/cache_proxy.py", "# CACHE-PREFIX WARNING\nX = 999\n")
    result = select(repo, state, tmp_path)
    assert result["decision"] == "emergency-current"
    assert result["selected"] == str(repo)


def test_gate_exception_still_uses_existing_fallback(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    with patch.object(cache_gate, "find_passing_report", side_effect=OSError("unavailable")):
        result = select(repo, state, tmp_path)
    assert result["decision"] == "fallback"
    assert result["selected"] == first["selected"]


def test_external_cliproxy_mismatch_is_not_hidden(repo, state, tmp_path, monkeypatch):
    external = tmp_path / "cliproxy"
    external.mkdir()
    (external / "config.yaml").write_text("old")
    monkeypatch.setattr(cache_gate, "CLIPROXY_DIR", external)
    _report(tmp_path / "reports", repo)
    select(repo, state, tmp_path)
    (external / "config.yaml").write_text("new")
    result = select(repo, state, tmp_path)
    assert result["decision"] == "emergency-current"
    assert "CLIProxyAPI" in result["reason"]


def test_dry_run_never_updates_state(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    result = select(repo, state, tmp_path, dry_run=True)
    assert result["decision"] == "would-admit"
    assert not state.exists()


def test_snapshot_race_does_not_replace_previous_release(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    _commit(repo, "src/cache_proxy.py", "# CACHE-PREFIX WARNING\nX = 22\n")
    _report(tmp_path / "reports", repo, name="later")
    with patch.object(cache_launch, "snapshot", side_effect=RuntimeError("changed while copying")):
        result = select(repo, state, tmp_path)
    assert result["decision"] == "fallback"
    assert result["selected"] == first["selected"]
    assert json.loads((state / "current.json").read_text())["repo"] == first["selected"]


def test_real_wrapper_launches_verified_fallback_not_dirty_checkout(repo, state, tmp_path):
    from tests.test_prod_wrapper import _prepare, _start, _free_port

    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nUNGATED = 1\n")
    control = tmp_path / "control"
    cache_launch.install_control(control)
    (repo / "src/obs_agent/cache_gate.py").write_text("# CACHE-PREFIX\ninvalid python!\n")
    command = f"PYTHONPATH={control} /workspace/obs/.venv/bin/python -m obs_agent.cache_launch --repo {repo} --state {state}"
    script = _prepare(tmp_path, _free_port(), launch_command=command)
    proc = _start(tmp_path, script, "crash", CACHE_REGRESSION_OUT=str(tmp_path / "reports"),
                  OBS_CLIPROXY_DIR=str(tmp_path / "no-cliproxy"))
    assert proc.wait(timeout=20) == 1
    assert (tmp_path / "out/release").read_text() == first["selected"]
    assert (tmp_path / "out/pythonpath").read_text().startswith(first["selected"] + "/src")
    assert not (tmp_path / "sentinel").exists()


def test_explicit_restart_override_does_not_replace_gated_baseline(repo, state, tmp_path):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nOVERRIDE = 1\n")
    target = cache_gate.code_identity(repo)
    result = cache_gate.GateResult(False, target=target)
    cache_gate.log_bypass(reason="authorized incident", source="cli", result=result)
    answer = select(repo, state, tmp_path)
    assert answer["decision"] == "override"
    assert answer["selected"] != str(repo)
    assert json.loads((state / "current.json").read_text())["repo"] == first["selected"]
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nOVERRIDE = 2\n")
    assert select(repo, state, tmp_path)["decision"] == "fallback"


def test_release_restart_checks_candidate_not_saved_release(repo, state, tmp_path, monkeypatch):
    _report(tmp_path / "reports", repo)
    first = select(repo, state, tmp_path)
    release = Path(first["selected"])
    monkeypatch.setenv("OBS_RUNNING_CODE_FILE", str(tmp_path / "running.json"))
    cache_gate.write_running_record(release)
    monkeypatch.setenv("OBS_CACHE_GATE_RELEASE", str(release))
    monkeypatch.setenv("OBS_CACHE_GATE_CANDIDATE", str(repo))
    (repo / "src/cache_proxy.py").write_text("# CACHE-PREFIX WARNING\nNEXT = 1\n")
    checked = cache_gate.check(release, report_root=tmp_path / "reports")
    assert not checked.ok
    assert checked.target == cache_gate.code_identity(repo)


@pytest.mark.parametrize("source", ["git pre-push refs/heads/main", "cli --plain", "supervisor launch"])
def test_other_bypasses_do_not_authorize_deployment(repo, state, tmp_path, source):
    cache_gate.log_bypass(reason="not a maintenance override", source=source,
                         result=cache_gate.GateResult(False, target=cache_gate.code_identity(repo)))
    assert select(repo, state, tmp_path)["decision"] == "emergency-current"
