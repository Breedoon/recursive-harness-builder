"""Cache-prefix gate (bead vault-3px5)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import cache_gate
from obs_agent import maintenance_restart as maint

pytestmark = pytest.mark.real_cache_gate


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "obs_agent").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src" / "cache_proxy.py").write_text("# CACHE-PREFIX WARNING proxy\nX = 1\n")
    (root / "src" / "obs_agent" / "session.py").write_text("# CACHE-PREFIX WARNING\nY = 1\n")
    (root / "src" / "obs_agent" / "tools.py").write_text("Z = 1\n")
    (root / "src" / "obs_agent" / "plain.py").write_text("W = 1\n")
    (root / "pyproject.toml").write_text("[x]\n")
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "base")
    monkeypatch.setenv("OBS_CLIPROXY_DIR", str(tmp_path / "no-cliproxy"))
    monkeypatch.setattr(cache_gate, "CLIPROXY_DIR", tmp_path / "no-cliproxy")
    return root


def _commit(repo, rel, text):
    (repo / rel).write_text(text)
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qam", f"edit {rel}")


def _report(root: Path, repo: Path, *, result="PASS", providers=("anthropic", "codex"), scenarios="all", name="20261015T190000Z"):
    out = root / name
    out.mkdir(parents=True)
    ident = cache_gate.code_identity(repo)
    (out / "gate.json").write_text(json.dumps({
        "result": result, "providers": list(providers), "scenarios": scenarios,
        "code": ident, "proxy_source": ident["proxy_source_sha256"],
    }))
    return out


def test_sensitive_files_follow_markers_plus_extras(repo):
    files = cache_gate.sensitive_files(repo)
    assert {"src/cache_proxy.py", "src/obs_agent/session.py", "src/obs_agent/tools.py", "pyproject.toml"} <= files
    assert "src/obs_agent/plain.py" not in files


def test_no_sensitive_change_passes_free(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/plain.py", "W = 2\n")
    result = cache_gate.check(repo, base=base, report_root=tmp_path / "reports")
    assert result.ok and result.touched == [] and result.report is None


def test_sensitive_change_without_report_refused(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 2\n")
    result = cache_gate.check(repo, base=base, report_root=tmp_path / "reports")
    assert not result.ok and result.touched == ["src/obs_agent/session.py"]
    assert "cache_regression.py" in result.message and "--skip-cache-gate" in result.message


def test_matching_passing_report_allows(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/tools.py", "Z = 2\n")
    reports = tmp_path / "reports"
    out = _report(reports, repo)
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert result.ok and result.report == out


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"result": "INCONCLUSIVE"}, "result"),
        ({"providers": ("anthropic",)}, "providers missing"),
        ({"scenarios": ["ws_resume"]}, "full scenario set"),
    ],
)
def test_non_matching_reports_rejected(repo, tmp_path, kwargs, why):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/cache_proxy.py", "# CACHE-PREFIX WARNING proxy\nX = 2\n")
    reports = tmp_path / "reports"
    _report(reports, repo, **kwargs)
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and why in result.message


def test_report_for_other_code_rejected(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 2\n")
    reports = tmp_path / "reports"
    _report(reports, repo)
    (repo / "src" / "obs_agent" / "session.py").write_text("# CACHE-PREFIX WARNING\nY = 3\n")  # uncommitted edit
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "different code identity" in result.message


def test_unknown_running_code_requires_report(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_RUNNING_CODE_FILE", str(tmp_path / "absent.json"))
    result = cache_gate.check(repo, report_root=tmp_path / "reports")
    assert not result.ok and "running code unknown" in result.message


def test_running_record_is_the_default_base(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_RUNNING_CODE_FILE", str(tmp_path / "running.json"))
    assert cache_gate.write_running_record(repo, pid=1)["head"]
    _commit(repo, "src/obs_agent/plain.py", "W = 3\n")
    assert cache_gate.check(repo, report_root=tmp_path / "reports").ok


def test_cli_refuses_and_override_is_logged(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_RUNNING_CODE_FILE", str(tmp_path / "absent.json"))
    log = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("OBS_CACHE_GATE_LOG", str(log))
    refused = cache_gate.GateResult(False, ["src/cache_proxy.py"], None, "cache gate REFUSED: test", {"head": "abc"})
    with patch.object(cache_gate, "check", return_value=refused), patch.object(
        maint, "find_daemon_pids", return_value=[4242]
    ), patch.object(maint.os, "kill") as kill:
        assert maint.main([]) == 3
        kill.assert_not_called()
        assert maint.main(["--skip-cache-gate", "  "]) == 2
        assert maint.main(["--skip-cache-gate", "crash loop fix"]) == 0
        kill.assert_called_once()
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["reason"] == "crash loop fix" and entry["touched"] == ["src/cache_proxy.py"]


def test_plain_restart_never_gated_but_logged(tmp_path, monkeypatch):
    log = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("OBS_CACHE_GATE_LOG", str(log))
    refused = cache_gate.GateResult(False, ["x"], None, "refused", {})
    with patch.object(cache_gate, "check", return_value=refused), patch.object(maint, "plain_restart", return_value=0) as plain:
        assert maint.main(["--plain"]) == 0
    plain.assert_called_once()
    assert "plain restart" in log.read_text()


def test_gate_crash_does_not_block_restart(monkeypatch):
    def boom(repo, **_):
        raise RuntimeError("git missing")

    with patch.object(cache_gate, "check", side_effect=boom), patch.object(
        maint, "find_daemon_pids", return_value=[4242]
    ), patch.object(maint.os, "kill") as kill:
        assert maint.main([]) == 0
    kill.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_restart_refused_then_bypassed(config, tmp_path, monkeypatch):
    from tests.test_maintenance_restart import _bot

    log = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("OBS_CACHE_GATE_LOG", str(log))
    bot = _bot(config)

    def _update(args):
        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_message.chat_id = 67890
        update.effective_message.message_thread_id = None
        context = MagicMock()
        context.args = args
        return update, context

    refused = cache_gate.GateResult(False, ["src/cache_proxy.py"], None, "cache gate REFUSED: test", {})
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        cache_gate, "check", return_value=refused
    ), patch.object(bot, "handle_maintenance_restart", AsyncMock()) as maintenance, patch.object(
        bot, "request_plain_restart", AsyncMock()
    ) as plain, patch.object(bot, "_send_system_message", AsyncMock()) as send:
        await bot.handle_restart(*_update([]))
        maintenance.assert_not_awaited()
        assert "REFUSED" in send.await_args.kwargs["text"]
        await bot.handle_restart(*_update(["skip-cache-gate"]))
        maintenance.assert_not_awaited()
        await bot.handle_restart(*_update(["skip-cache-gate", "Crash", "loop"]))
        maintenance.assert_awaited_once()
        await bot.handle_restart(*_update(["plain"]))
        plain.assert_awaited_once()
    reasons = [json.loads(line)["reason"] for line in log.read_text().splitlines()]
    assert reasons == ["Crash loop", "plain restart (kill switch, ungated)"]


def test_regression_runner_writes_matching_gate_report(tmp_path):
    from types import SimpleNamespace

    from scripts import cache_regression as runner

    results = [
        {"provider": "anthropic", "scenario": "a", "status": "PASS"},
        {"provider": "codex", "scenario": "a", "status": "PASS"},
        {"provider": "codex", "scenario": "b", "status": "PASS"},
    ]
    args = SimpleNamespace(scenarios=[], base_url="http://127.0.0.1:28942")
    runner._write_gate_report(tmp_path, args=args, ident={}, live="abc", results=results, overall="PASS")
    report = json.loads((tmp_path / "gate.json").read_text())
    assert report["result"] == "PASS" and report["providers"] == ["anthropic", "codex"]
    assert report["scenarios"] == "all" and report["code"]["head"] and report["proxy_source"] == "abc"
    results[2]["status"] = "PROVIDER-ERROR"
    runner._write_gate_report(tmp_path, args=args, ident={}, live="abc", results=results, overall="INCONCLUSIVE")
    report = json.loads((tmp_path / "gate.json").read_text())
    assert report["providers"] == ["anthropic", "codex"] and report["providers_all_pass"] == ["anthropic"]


def _report_with(root, repo, name, result, per, scenarios="all", failures=None, providers=None):
    out = root / name
    out.mkdir(parents=True)
    ident = cache_gate.code_identity(repo)
    report = {
        "result": result, "providers": sorted(per) if providers is None else list(providers),
        "scenarios": scenarios, "scenario_results": per,
        "code": ident, "proxy_source": ident["proxy_source_sha256"],
    }
    if failures is not None:
        report["scenario_failures"] = failures
    (out / "gate.json").write_text(json.dumps(report))
    return out


def _fail(cls, ratio):
    return {"status": "FAIL", "bad_pairs": [{"b": "req", "class": cls, "ratio": ratio}]}


def test_intermittent_fail_accepted_only_after_rerun_passes(repo, tmp_path):
    import os
    import time

    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 9\n")
    reports = tmp_path / "reports"
    full = _report_with(reports, repo, "a-full", "FAIL", {
        "anthropic": {"baseline": "PASS", "recovery_multi": "FAIL"}, "codex": {"baseline": "PASS"}},
        failures={"anthropic": {"recovery_multi": _fail("tail-block-append", 0.9216)}})
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "anthropic/recovery_multi=FAIL" in result.message
    time.sleep(0.01)
    rerun = _report_with(reports, repo, "b-rerun", "PASS", {"anthropic": {"recovery_multi": "PASS"}},
                         scenarios=["recovery_multi"])
    later = time.time() + 5
    os.utime(rerun / "gate.json", (later, later))
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "1 of 2 passing reruns" in result.message  # one lucky pass is not enough
    rerun2 = _report_with(reports, repo, "c-rerun", "PASS", {"anthropic": {"recovery_multi": "PASS"}},
                          scenarios=["recovery_multi"])
    os.utime(rerun2 / "gate.json", (later + 1, later + 1))
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert result.ok and result.report == full


def _full_fail_then_rerun(repo, tmp_path, provider, scenario, failure, sequence=("PASS", "PASS")):
    """Full run with one non-PASS scenario, then reruns of it in order (oldest first)."""
    import os
    import time

    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 9\n")
    reports = tmp_path / "reports"
    per = {"anthropic": {"baseline": "PASS"}, "codex": {"baseline": "PASS"}}
    per[provider][scenario] = failure["status"]
    full = _report_with(reports, repo, "a-full", "FAIL", per, failures={provider: {scenario: failure}})
    time.sleep(0.01)
    later = time.time() + 5
    for index, rerun_status in enumerate(sequence):
        rerun_failure = rerun_status if isinstance(rerun_status, dict) else {"status": rerun_status}
        status = rerun_failure["status"]
        rerun = _report_with(reports, repo, f"b-rerun-{index:02d}", "PASS" if status == "PASS" else status,
                             {provider: {scenario: status}}, scenarios=[scenario],
                             failures=None if status == "PASS" else {provider: {scenario: rerun_failure}})
        os.utime(rerun / "gate.json", (later + index, later + index))
    return cache_gate.check(repo, base=base, report_root=reports), full


@pytest.mark.parametrize("provider,scenario,failure", [
    ("codex", "boundary_marker", _fail("append", 0.0)),        # documented 2026-10-12
    ("codex", "cch_collision", _fail("append", 0.0)),          # observed 2026-10-15
    ("codex", "boundary_marker", _fail("append", 0.835)),      # observed 2026-10-15, pure append
    ("codex", "reminder_block", _fail("append", 0.0)),         # observed 2026-10-16
    ("anthropic", "recovery_multi", _fail("tail-block-append", 0.8721)),
    ("anthropic", "recovery_multi", _fail("tail-block-append", 0.944)),
    ("codex", "baseline", {"status": "PROVIDER-ERROR", "bad_pairs": []}),
])
def test_documented_shapes_accepted_after_rerun(repo, tmp_path, provider, scenario, failure):
    result, full = _full_fail_then_rerun(repo, tmp_path, provider, scenario, failure)
    assert result.ok and result.report == full


@pytest.mark.parametrize("provider,scenario,failure,why", [
    ("anthropic", "boundary_marker", _fail("append", 0.835), "not a documented"),   # Claude short read is ours
    ("codex", "boundary_marker", _fail("history-changed", 0.0), "not a documented"),
    ("anthropic", "recovery", _fail("tail-block-append", 0.93), "not a documented"),  # wrong scenario
    ("anthropic", "recovery_multi", _fail("tail-block-append", 0.70), "not a documented"),
    ("codex", "baseline", _fail("tools-changed", 0.5), "not a documented"),
    ("codex", "baseline", {"status": "FAIL", "bad_pairs": []}, "without bad pairs"),
    ("codex", "boundary_marker", _fail("append", 0.9), "not a documented"),    # unobserved partial read
    ("codex", "boundary_marker", _fail("append", 0.5), "not a documented"),
    ("codex", "boundary_marker", _fail("append", 0.79), "not a documented"),
    ("codex", "boundary_marker", _fail("append", 0.84), "not a documented"),   # no band around 0.835
    ("codex", "boundary_marker", _fail("append", 0.82), "not a documented"),
])
def test_undocumented_shapes_block_even_after_rerun(repo, tmp_path, provider, scenario, failure, why):
    result, _ = _full_fail_then_rerun(repo, tmp_path, provider, scenario, failure)
    assert not result.ok and why in result.message


def test_old_report_without_failure_detail_is_not_rerun_accepted(repo, tmp_path):
    import os
    import time

    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 9\n")
    reports = tmp_path / "reports"
    _report_with(reports, repo, "a-full", "FAIL", {
        "anthropic": {"baseline": "PASS"}, "codex": {"baseline": "PASS", "boundary_marker": "FAIL"}})
    time.sleep(0.01)
    rerun = _report_with(reports, repo, "b-rerun", "PASS", {"codex": {"boundary_marker": "PASS"}},
                         scenarios=["boundary_marker"])
    later = time.time() + 5
    os.utime(rerun / "gate.json", (later, later))
    assert not cache_gate.check(repo, base=base, report_root=reports).ok


def test_shape_table_matches_safety_doc():
    """The gate's table and docs/cache-proxy-safety.md must describe the same shapes."""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cache-proxy-safety.md").read_text()
    section = doc.split("### How to read the runner's result", 1)[1].split("###", 1)[0]
    for shape in cache_gate.KNOWN_INTERMITTENT_SHAPES:
        assert f"`{shape['class']}`" in section
        assert f"{shape['min_ratio']}" in section and f"{shape['max_ratio']}" in section
        if shape["scenario"]:
            assert f"`{shape['scenario']}`" in section
    assert section.count("Known-shape table:") == 1


def test_runner_records_failure_shapes(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", Path(__file__).resolve().parents[1] / "scripts" / "cache_regression.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    args = type("A", (), {"scenarios": [], "base_url": "x"})()
    results = [
        {"scenario": "baseline", "provider": "anthropic", "status": "PASS", "bad_pairs": []},
        {"scenario": "boundary_marker", "provider": "codex", "status": "FAIL",
         "bad_pairs": [{"b": "r", "class": "append", "ratio": 0.835}]},
    ]
    runner._write_gate_report(tmp_path, args=args, ident={"code": {"head": "x"}}, live="abc",
                              results=results, overall="FAIL")
    report = json.loads((tmp_path / "gate.json").read_text())
    assert report["scenario_failures"] == {"codex": {"boundary_marker": {
        "status": "FAIL", "bad_pairs": [{"b": "r", "class": "append", "ratio": 0.835}], "error": None}}}


def test_deterministic_gpt_append_at_0_9_blocked(repo, tmp_path):
    """A real ~0.9 regression that fails its rerun too must block (branch D verifier fix 4)."""
    import os
    import time

    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 9\n")
    reports = tmp_path / "reports"
    per = {"anthropic": {"baseline": "PASS"}, "codex": {"baseline": "PASS", "boundary_marker": "FAIL"}}
    _report_with(reports, repo, "a-full", "FAIL", per,
                 failures={"codex": {"boundary_marker": _fail("append", 0.9)}})
    time.sleep(0.01)
    rerun = _report_with(reports, repo, "b-rerun", "FAIL", {"codex": {"boundary_marker": "FAIL"}},
                         scenarios=["boundary_marker"],
                         failures={"codex": {"boundary_marker": _fail("append", 0.9)}})
    later = time.time() + 5
    os.utime(rerun / "gate.json", (later, later))
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "append at 0.9" in result.message


def test_report_missing_a_provider_refused(repo, tmp_path):
    """gate.json must show both routes actually ran (branch D verifier fix 3)."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/obs_agent/session.py", "# CACHE-PREFIX WARNING\nY = 9\n")
    reports = tmp_path / "reports"
    per = {"anthropic": {"baseline": "PASS"}, "codex": {"baseline": "PASS"}}
    _report_with(reports, repo, "a-pass", "PASS", per, providers=["anthropic"])
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "providers missing ['codex']" in result.message
    _report_with(reports, repo, "b-fail", "FAIL", {**per, "codex": {"baseline": "PASS", "x": "FAIL"}},
                 failures={"codex": {"x": _fail("append", 0.0)}}, providers=["codex"])
    result = cache_gate.check(repo, base=base, report_root=reports)
    assert not result.ok and "providers missing ['anthropic']" in result.message


# Rerun rule (bead ops-1.11.3): reruns read in time order; no FAIL after a pass, at most one failed
# rerun (itself a documented shape), then two passes after any FAIL; PROVIDER-ERROR/SKIPPED are no verdict.
_RM = ("anthropic", "recovery_multi", _fail("tail-block-append", 0.9241))
_GPT0 = ("codex", "boundary_marker", _fail("append", 0.0))


@pytest.mark.parametrize("shape,sequence", [
    (_RM, ("PASS", "PASS")),
    (_RM, ("PASS", "PASS", "PASS")),
    (_RM, (_fail("tail-block-append", 0.8721), "PASS", "PASS")),       # one documented failed rerun
    (_RM, ("PASS", "PROVIDER-ERROR", "PASS")),                          # no-verdict rerun skipped
    (_GPT0, ("SKIPPED", "PASS", "PASS")),
    (("codex", "baseline", {"status": "PROVIDER-ERROR", "bad_pairs": []}), ("PASS",)),  # no FAIL seen: one pass
])
def test_rerun_rule_accepts(repo, tmp_path, shape, sequence):
    result, full = _full_fail_then_rerun(repo, tmp_path, *shape, sequence=sequence)
    assert result.ok and result.report == full


@pytest.mark.parametrize("shape,sequence,why", [
    (_RM, (), "0 of 2 passing reruns"),
    (_RM, ("PASS",), "1 of 2 passing reruns"),
    (_RM, ("PROVIDER-ERROR", "PASS"), "1 of 2 passing reruns"),
    # observed 2026-10-16 on 2a1eb44: full FAIL 0.9241, rerun 1 FAIL without bad pairs, rerun 2 PASS
    (_RM, ({"status": "FAIL", "bad_pairs": []}, "PASS"), "rerun FAIL: FAIL without bad pairs"),
    (_RM, ({"status": "FAIL", "bad_pairs": []}, "PASS", "PASS"), "rerun FAIL: FAIL without bad pairs"),
    (_RM, ("PASS", _fail("tail-block-append", 0.9), "PASS", "PASS"), "after a passing rerun"),
    (_RM, ("PASS", "PASS", _fail("tail-block-append", 0.9)), "after a passing rerun"),
    (_RM, (_fail("tail-block-append", 0.9),) * 2 + ("PASS", "PASS"), "2 failed reruns"),
    (_RM, (_fail("history-changed", 0.5), "PASS", "PASS"), "rerun FAIL: history-changed"),
    (_RM, (_fail("tail-block-append", 0.70), "PASS", "PASS"), "not a documented"),
    (_GPT0, ("PASS", _fail("append", 0.0), "PASS"), "after a passing rerun"),
    (_GPT0, (_fail("append", 0.6), "PASS", "PASS"), "not a documented"),  # rerun shape outside the table
    (("codex", "baseline", {"status": "PROVIDER-ERROR", "bad_pairs": []}), (_fail("append", 0.0), "PASS"),
     "1 of 2 passing reruns"),  # first verdict was a FAIL: two passes then
])
def test_rerun_rule_blocks(repo, tmp_path, shape, sequence, why):
    result, _ = _full_fail_then_rerun(repo, tmp_path, *shape, sequence=sequence)
    assert not result.ok and why in result.message


def test_rerun_rule_matches_safety_doc():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cache-proxy-safety.md").read_text()
    section = doc.split("### How to read the runner's result", 1)[1].split("###", 1)[0]
    rule = " ".join(section.split("Rerun rule:", 1)[1].split("\n- ", 1)[0].split())
    words = {1: "one", 2: "two", 3: "three"}
    assert f"pass {words[cache_gate.RERUN_PASSES_AFTER_FAIL]} times in a row" in rule
    assert f"at most {words[cache_gate.RERUN_FAILS_ALLOWED]} rerun" in rule
    assert "after a passing rerun" in rule and "time order" in rule
    for status in cache_gate.RERUN_STATUSES_WITHOUT_SHAPE:
        assert f"`{status}`" in rule
