"""Index-observation hygiene; acceptance also requires recorded bytes and live gates."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def repro(monkeypatch):
    source = Path(__file__).with_name("prefix_repro.py")
    monkeypatch.syspath_prepend(str(source.parent))
    monkeypatch.delitem(sys.modules, "prefix_diff", raising=False)
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        # These tests exercise recorded index/evaluation only, never the SDK driver.
        sdk = ModuleType("claude_agent_sdk")
        for name in ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
                     "HookMatcher", "ResultMessage", "ToolUseBlock"):
            setattr(sdk, name, type(name, (), {}))
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    spec = importlib.util.spec_from_file_location("f4_prefix_repro", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.pd.__file__).resolve() == source.with_name("prefix_diff.py").resolve()
    return module


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.publish = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.publish()


def recording(tmp_path, *, mode="late", provider="codex", bad_prefix=False):
    day = tmp_path / "2026-10-23"
    day.mkdir()
    index = day / "index.jsonl"
    rows = []
    messages = []
    model = "gpt-6.1-sol" if provider == "codex" else "claude-haiku-4-5-20251001"
    route = "cli-proxy" if provider == "codex" else "anthropic"
    for i in range(4):
        messages += [{"role": "user", "content": [{"type": "text", "text": f"turn {i}"}]}]
        wire = {"model": model, "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
                "system": "public recorded-index test", "messages": json.loads(json.dumps(messages))}
        wire["messages"][-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        if bad_prefix and i == 3:
            wire["messages"][0]["content"][0]["text"] = "genuine changed history"
        req_id = f"20261023T010000.000000Z-1-{i + 1:06d}-own"
        with gzip.open(day / f"{req_id}.wire.json.gz", "wt") as f:
            json.dump(wire, f)
        rows.append({"req_id": req_id, "session_id": "own", "ts": 1000 + i / 10,
                     "path": "/v1/messages", "http_status": 200, "route": route,
                     "model": model, "sizes": {"pre": 30000},
                     "usage": {"cache_read": 0 if i == 0 else 99 + i,
                               "input_tokens": 100 if i == 0 else 1}})
    utility = {"req_id": "20261023T010000.000000Z-1-000005-own", "session_id": "own",
               "ts": 1000.15, "path": "/v1/messages", "http_status": 200,
               "route": route, "model": model, "usage": None, "sizes": {"pre": 100}}
    if mode == "wrong_route":
        utility["route"] = "anthropic" if provider == "codex" else "cli-proxy"
    if mode == "wrong_model":
        utility["model"] = "gpt-wrong-model"
    if mode == "other_session":
        utility["session_id"] = "other-scenario"
    if mode == "stale":
        utility["ts"] = 998
    if mode == "count_tokens":
        utility["path"] = "/v1/messages/count_tokens"
    if mode == "incomplete":
        utility["http_status"] = None
    if mode == "http_error":
        utility["http_status"] = 400
    with gzip.open(day / f"{utility['req_id']}.wire.json.gz", "wt") as f:
        json.dump({"model": model, "system": "Extract any file paths from this Bash result"}, f)
    index.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return index, utility, model


def run_recording(repro, monkeypatch, tmp_path, *, mode="late", provider="codex", bad_prefix=False):
    index, utility, model = recording(tmp_path, mode=mode, provider=provider, bad_prefix=bad_prefix)
    clock = Clock()
    published = False

    def publish():
        nonlocal published
        if not published and mode != "missing" and (mode != "late" or clock.now >= 4.2):
            with index.open("a") as f:
                f.write(json.dumps(utility) + "\n")
            published = True

    clock.publish = publish
    publish()
    monkeypatch.setattr(repro, "time", SimpleNamespace(time=lambda: 1000, monotonic=clock.monotonic,
                                                      sleep=clock.sleep))
    async def scenario(args):
        return ["own"], None
    monkeypatch.setitem(repro.SCENARIOS, "utility_route", (scenario, "recorded utility observation"))
    args = SimpleNamespace(log_dir=str(tmp_path), provider=provider, model=model,
                           threshold=0.95, scenario_timeout=30)
    result = repro.run(args, "utility_route")
    return result, clock


def test_late_own_row_is_observed_before_evaluation(repro, monkeypatch, tmp_path):
    result, clock = run_recording(repro, monkeypatch, tmp_path)
    assert result["status"] == "PASS", result
    assert all(p["ok"] for p in result["pairs"])
    assert result["utility_requests"] == ["20261023T010000.000000Z-1-000005-own"]
    assert 4.2 <= result["utility_observation"]["elapsed_seconds"] < 4.4
    assert clock.now < 8


@pytest.mark.parametrize("mode", ["missing", "other_session", "stale", "count_tokens", "incomplete"])
def test_missing_own_completed_row_times_out(repro, monkeypatch, tmp_path, mode):
    result, clock = run_recording(repro, monkeypatch, tmp_path, mode=mode)
    assert result["status"] == "FAIL", result
    assert all(p["ok"] for p in result["pairs"])
    assert result["utility_observation"]["status"] == "timed_out"
    assert result["utility_observation"]["elapsed_seconds"] == 30.0
    assert clock.now == 33.0  # unchanged evaluator settle follows bounded observation


@pytest.mark.parametrize("provider", ["codex", "anthropic"])
def test_wrong_route_fails_without_waiting_for_another_row(repro, monkeypatch, tmp_path, provider):
    result, clock = run_recording(repro, monkeypatch, tmp_path, mode="wrong_route", provider=provider)
    assert result["status"] == "FAIL", result
    assert not result["route_ok"]
    assert result["utility_observation"]["elapsed_seconds"] == 0
    assert clock.sleeps == [3.0]


@pytest.mark.parametrize("mode", ["early", "wrong_model", "http_error"])
def test_early_completed_row_never_adds_a_settling_delay(repro, monkeypatch, tmp_path, mode):
    result, clock = run_recording(repro, monkeypatch, tmp_path, mode=mode)
    assert result["status"] == ("PASS" if mode == "early" else "FAIL"), result
    assert result["utility_observation"]["checks"] == 1
    assert result["utility_observation"]["elapsed_seconds"] == 0
    assert clock.sleeps == [3.0]


def test_bad_prefix_still_fails_at_unchanged_threshold(repro, monkeypatch, tmp_path):
    result, _ = run_recording(repro, monkeypatch, tmp_path, mode="early", bad_prefix=True)
    assert result["status"] == "FAIL", result
    assert result["route_ok"]
    assert any(p["class"] == "history-changed" and not p["ok"] for p in result["pairs"])


def test_missing_index_is_a_bounded_failure(repro, monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(repro, "time", SimpleNamespace(time=lambda: 1000, monotonic=clock.monotonic,
                                                      sleep=clock.sleep))
    result = repro._wait_utility_index(str(tmp_path / "unavailable"), ["own"], 999, 0.3)
    assert result["status"] == "timed_out"
    assert 0.3 <= clock.now < 0.4
    assert not list(tmp_path.iterdir())


def test_help_exits_before_any_real_work(tmp_path):
    python = "/workspace/obs/.venv/bin/python"
    if not Path(python).exists():
        pytest.skip("runtime venv unavailable")
    source = Path(__file__).with_name("prefix_repro.py")
    root = tmp_path / "must-not-exist"
    env = dict(os.environ, PREFIX_REPRO_ROOT=str(root), HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([python, str(source), "--help"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not root.exists()


def test_dependency_unavailable_starts_no_work(tmp_path):
    (tmp_path / "claude_agent_sdk.py").write_text("raise ImportError('F4 deliberately unavailable SDK')\n")
    source = Path(__file__).with_name("prefix_repro.py")
    root = tmp_path / "must-not-exist"
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PREFIX_REPRO_ROOT=str(root),
               HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, str(source), "utility_route"], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "F4 deliberately unavailable SDK" in result.stderr
    assert not root.exists()
