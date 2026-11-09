"""prefix_repro --cli-path: the CLI is selectable and its argv[0] is recorded and asserted (obs-22 inc12b).

No live model call and no cache proxy: scenario bodies and CLI processes are faked.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def driver():
    spec = importlib.util.spec_from_file_location("prefix_repro_cli_path_test", ROOT / "scripts/prefix_repro.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.CLI_ARGV0S.clear()
    return module


def _args(**kw):
    base = dict(base_url="http://127.0.0.1:1", provider="anthropic", model="claude-haiku-5-5", cli_path=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_options_carry_the_cli_path_only_when_given(driver, tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "WORK_ROOT", tmp_path)
    assert driver._options(_args()).cli_path is None  # default unchanged: the SDK's bundled CLI
    assert driver._options(_args(cli_path="/opt/obs-cli")).cli_path == "/opt/obs-cli"


def test_the_spawned_process_argv0_is_read_from_proc(driver):
    proc = subprocess.Popen(["bash", "-c", "exec -a /opt/fake/obs-cli sleep 30"])
    try:
        for _ in range(100):  # the exec happens right after fork
            if Path(f"/proc/{proc.pid}/cmdline").read_bytes().startswith(b"/opt/fake/obs-cli"):
                break
        client = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=proc.pid)))
        driver._record_cli_argv0(client)
    finally:
        proc.kill()
        proc.wait()
    assert driver.CLI_ARGV0S == ["/opt/fake/obs-cli"]


def test_client_subclass_records_after_connect(driver, monkeypatch):
    seen = []

    async def base_connect(self, *a, **k):
        self._transport = SimpleNamespace(_process=SimpleNamespace(pid=1))
        return "connected"

    monkeypatch.setattr(driver._SDKClient, "connect", base_connect)
    monkeypatch.setattr(driver, "_record_cli_argv0", lambda client: seen.append(client))
    client = driver.ClaudeSDKClient.__new__(driver.ClaudeSDKClient)
    assert asyncio.run(client.connect()) == "connected"
    assert seen == [client]


def _fake_run(driver, monkeypatch, argv0s, ok=True):
    def fake(args, name):
        driver.CLI_ARGV0S.extend(argv0s)
        return {"scenario": name, "ok": ok, "status": "PASS" if ok else "FAIL", "pairs": []}

    monkeypatch.setattr(driver, "_run", fake)


def test_run_without_cli_path_records_but_asserts_nothing(driver, monkeypatch):
    _fake_run(driver, monkeypatch, ["/bundled/claude"])
    res = driver.run(_args(), "baseline")
    assert res["ok"] and res["status"] == "PASS" and res["cli_argv0s"] == ["/bundled/claude"]
    assert "cli_path_expected" not in res


def test_run_with_cli_path_passes_only_when_every_process_was_that_binary(driver, monkeypatch):
    _fake_run(driver, monkeypatch, ["/opt/obs-cli", "/opt/obs-cli"])
    res = driver.run(_args(cli_path="/opt/obs-cli"), "baseline")
    assert res["ok"] and res["cli_argv0s"] == ["/opt/obs-cli"] and res["cli_path_expected"] == "/opt/obs-cli"


@pytest.mark.parametrize("seen", [["/bundled/claude"], ["/opt/obs-cli", "/bundled/claude"], []])
def test_run_with_cli_path_fails_on_another_cli_or_none(driver, monkeypatch, seen):
    _fake_run(driver, monkeypatch, seen)
    res = driver.run(_args(cli_path="/opt/obs-cli"), "baseline")
    assert not res["ok"] and res["status"] == "FAIL" and "argv[0] check" in res["error"]


def test_a_failing_scenario_stays_failing(driver, monkeypatch):
    _fake_run(driver, monkeypatch, ["/opt/obs-cli"], ok=False)
    res = driver.run(_args(cli_path="/opt/obs-cli"), "baseline")
    assert not res["ok"] and res["status"] == "FAIL"


def test_cli_path_must_be_an_executable_file(driver, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.write_text("x")
    with pytest.raises(SystemExit):
        driver.main(["baseline", "--cli-path", str(plain)])
    assert "executable" in capsys.readouterr().err


def test_cache_regression_passes_cli_path_to_the_anthropic_route_only(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("cache_regression_cli_path_test", ROOT / "scripts/cache_regression.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cmds = []

    class Done:
        stdout, stderr, returncode = "", "", 0

    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: cmds.append(cmd) or Done())
    args = SimpleNamespace(base_url="u", log_dir="l", threshold=0.95, scenario_timeout=5, model=None,
                           cli_path="/opt/obs-cli")
    module.run_one(args, "anthropic", "baseline", tmp_path)
    module.run_one(args, "codex", "baseline", tmp_path)
    assert cmds[0][cmds[0].index("--cli-path") + 1] == "/opt/obs-cli"
    assert "--cli-path" not in cmds[1]
    args.cli_path = None
    module.run_one(args, "anthropic", "baseline", tmp_path)
    assert "--cli-path" not in cmds[2]


def test_a_canary_run_never_writes_the_admission_report(tmp_path):
    spec = importlib.util.spec_from_file_location("cache_regression_canary_name_test", ROOT / "scripts/cache_regression.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    results = [{"provider": "anthropic", "scenario": "a", "status": "PASS", "cli_argv0s": ["/opt/obs-cli"]}]
    canary = SimpleNamespace(scenarios=[], base_url="u", cli_path="/opt/obs-cli")
    module._write_gate_report(tmp_path, args=canary, ident={}, live="x", results=results, overall="PASS")
    assert (tmp_path / "gate-canary.json").exists() and not (tmp_path / "gate.json").exists()
    default = SimpleNamespace(scenarios=[], base_url="u", cli_path=None)
    module._write_gate_report(tmp_path, args=default, ident={}, live="x", results=results, overall="PASS")
    assert (tmp_path / "gate.json").exists()


def _runner(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/cache_regression.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_model_reaches_every_codex_route_run_only(monkeypatch, tmp_path):
    module = _runner("cache_regression_codex_model_test")
    cmds = []

    class Done:
        stdout, stderr, returncode = "", "", 0

    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: cmds.append(cmd) or Done())
    args = SimpleNamespace(base_url="u", log_dir="l", threshold=0.95, scenario_timeout=5, model=None,
                           cli_path=None, codex_model="terra")
    module.run_one(args, "codex", "baseline", tmp_path)
    module.run_one(args, "anthropic", "baseline", tmp_path)
    assert cmds[0][cmds[0].index("--model") + 1] == "terra"
    assert "--model" not in cmds[1]  # the Claude route keeps the harness Haiku
    args.codex_model = None  # default unchanged: no --model, so prefix_repro.py resolves its own default
    module.run_one(args, "codex", "baseline", tmp_path)
    assert "--model" not in cmds[2]
    del args.codex_model  # callers that never heard of the option (older tests) still work
    module.run_one(args, "codex", "baseline", tmp_path)
    assert "--model" not in cmds[3]


def test_run_one_reads_the_model_prefix_repro_used(monkeypatch, tmp_path):
    module = _runner("cache_regression_model_read_test")

    class Done:
        stdout, stderr, returncode = "", "", 0

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--json-out") + 1])
        out.write_text('[{"scenario": "baseline", "status": "PASS", "ok": true, "model": "gpt-6.1-terra"}]')
        return Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    args = SimpleNamespace(base_url="u", log_dir="l", threshold=0.95, scenario_timeout=5, model=None,
                           cli_path=None, codex_model="terra")
    assert module.run_one(args, "codex", "baseline", tmp_path)["model"] == "gpt-6.1-terra"


def test_report_records_the_model_of_each_route_and_admission_fields_are_unchanged(tmp_path):
    module = _runner("cache_regression_models_report_test")
    results = [
        {"provider": "anthropic", "scenario": "a", "status": "PASS", "model": "claude-haiku-4-5-20251001"},
        {"provider": "codex", "scenario": "a", "status": "PASS", "model": "gpt-6.1-terra"},
        {"provider": "codex", "scenario": "b", "status": "SKIPPED"},
    ]
    args = SimpleNamespace(scenarios=[], base_url="u", cli_path=None)
    module._write_gate_report(tmp_path, args=args, ident={"code": {"head": "x"}}, live="x", results=results,
                              overall="PASS")
    import json
    report = json.loads((tmp_path / "gate.json").read_text())
    assert report["models"] == {"anthropic": "claude-haiku-4-5-20251001", "codex": "gpt-6.1-terra"}
    assert report["providers"] == ["anthropic", "codex"] and report["scenarios"] == "all"
    mixed = [{"provider": "codex", "scenario": "a", "status": "PASS", "model": "gpt-6.1-sol"},
             {"provider": "codex", "scenario": "b", "status": "PASS", "model": "gpt-6.1-terra"}]
    module._write_gate_report(tmp_path, args=args, ident={"code": {}}, live="x", results=mixed, overall="PASS")
    assert json.loads((tmp_path / "gate.json").read_text())["models"] == {"codex": "gpt-6.1-sol+gpt-6.1-terra"}
