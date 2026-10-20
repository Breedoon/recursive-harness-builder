"""Unit tests must never write team state into the real home (obs-15 re-check 2, R-3/M6-2)."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _REAL_HOME, _REAL_TEAMS, _pytest_made_team_dirs


def test_unit_tests_get_an_isolated_teams_dir():
    teams = Path.home() / ".claude" / "teams"
    assert teams.is_dir()
    assert teams.resolve() != _REAL_TEAMS.resolve()


def test_other_claude_state_still_points_at_the_real_home():
    projects = Path.home() / ".claude" / "projects"
    real_projects = _REAL_HOME / ".claude" / "projects"
    if real_projects.exists():
        assert projects.resolve() == real_projects.resolve()


def test_test_set_home_env_is_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path.home() == tmp_path


def test_team_dir_written_via_path_home_stays_out_of_real_home(tmp_path):
    team = Path.home() / ".claude" / "teams" / "isolation-probe"
    team.mkdir(parents=True)
    (team / "config.json").write_text(f'{{"cwd": "{tmp_path}"}}', encoding="utf-8")
    assert not (_REAL_TEAMS / "isolation-probe").exists()


def test_detector_recognises_pytest_made_dirs(tmp_path):
    root = tmp_path / "teams"
    (root / "real").mkdir(parents=True)
    (root / "real" / "config.json").write_text('{"cwd": "/workspace/runtime/git/obs-vault-active"}')
    (root / "made").mkdir()
    (root / "made" / "config.json").write_text('{"cwd": "/tmp/pytest-of-agent/pytest-1/x0"}')
    assert _pytest_made_team_dirs(root) == {"made"}
