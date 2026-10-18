"""Spark liveness watchdog (bead obs-8, 2026-10-18): judgement and restart policy."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obs_agent import spark_watchdog as wd


def _health(rounds=0, running=1, ok=True, rows=0):
    return {"ok": ok, "requests_running": running, "rounds_total": rounds, "requests_total": 10,
            "completion_tokens_total": 5, "kv_shard": {"fetch": {"rows": rows}},
            "multi_prefill": {"rows": 0}, "spill": {"written_blocks": 0, "loaded": 0}}


def test_progressing_engine_is_healthy_even_with_long_requests():
    d = wd.Detector()
    d.reset(0)
    for t in range(0, 3600, 15):
        # a long cold prefill: only prompt-chunk fetch rows move, every 180s
        assert d.observe(t, _health(rows=t // 180)).state in ("healthy", "suspect")


def test_frozen_counters_with_requests_running_is_wedged_even_though_ok():
    d = wd.Detector()
    d.reset(0)
    d.observe(0, _health(rounds=5))
    assert d.observe(590, _health(rounds=5)).state == "suspect"
    v = d.observe(600, _health(rounds=5))
    assert v.state == "wedged" and "no engine progress" in v.reason


def test_idle_engine_is_never_wedged():
    d = wd.Detector()
    d.reset(0)
    for t in range(0, 7200, 15):
        assert d.observe(t, _health(running=0)).state == "healthy"


def test_unreachable_health_is_down_after_down_seconds():
    d = wd.Detector()
    d.reset(0)
    assert d.observe(0, None).state == "suspect"
    assert d.observe(299, None).state == "suspect"
    assert d.observe(300, None).state == "down"
    assert d.observe(315, _health()).state == "healthy"


def test_climbing_restart_count_is_a_crash_loop():
    d = wd.Detector()
    d.reset(0)
    d.observe_ranks(0, {"spark-2d31": {"restart_count": 10}})
    d.observe_ranks(60, {"spark-2d31": {"restart_count": 11}})
    assert d.observe(60, _health()).state == "healthy"
    d.observe_ranks(120, {"spark-2d31": {"restart_count": 12}})
    v = d.observe(120, _health())
    assert v.state == "down" and "spark-2d31" in v.reason


def test_maintenance_hands_off_and_resets_clocks():
    d = wd.Detector()
    d.reset(0)
    d.observe(0, _health(rounds=1))
    assert d.observe(700, _health(rounds=1), maintenance="lease held by x").state == "maintenance"
    # after the lease is released the stall clock starts again
    assert d.observe(710, _health(rounds=1)).state == "healthy"
    assert d.observe(1000, _health(rounds=1)).state == "suspect"


class FakeSparks:
    def __init__(self, ok=True, maint=None):
        self.ok, self.maint, self.restarts = ok, maint, []

    def maintenance(self):
        return self.maint

    def ranks(self):
        return {}

    def restart(self, reason):
        self.restarts.append(reason)
        return self.ok, "fake"


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(wd, "EVENTS_FILE", tmp_path / "events.jsonl")
    return tmp_path


def _run(sparks, health_fn, seconds, start=0.0):
    clock = {"t": start}

    def sleep(dt):
        clock["t"] += dt
    wd.run_forever(sparks, wd.Detector(), iterations=int(seconds / wd.POLL_SECONDS),
                   clock=lambda: clock["t"], sleep=sleep, health=health_fn)


def test_wedge_triggers_one_restart_and_publishes_states(state_dir):
    sparks = FakeSparks()
    _run(sparks, lambda: _health(rounds=1), 700)
    assert len(sparks.restarts) == 1 and "no engine progress" in sparks.restarts[0]
    events = [json.loads(l) for l in (state_dir / "events.jsonl").read_text().splitlines()]
    states = [e["state"] for e in events if e["event"] == "state"]
    assert states[states.index("restarting") + 1] == "healthy"


def test_failed_restarts_back_off(state_dir):
    sparks = FakeSparks(ok=False)
    _run(sparks, lambda: None, 3600)
    # first restart after 300s down, the next not before 600s more, then 1200s
    assert 2 <= len(sparks.restarts) <= 3
    assert json.loads((state_dir / "state.json").read_text())["state"] == "failed"


def test_no_restart_while_another_owner_holds_the_lease(state_dir):
    sparks = FakeSparks(maint="lease held by operator")
    _run(sparks, lambda: None, 3600)
    assert sparks.restarts == []
    assert json.loads((state_dir / "state.json").read_text())["state"] == "maintenance"


def test_observe_only_never_restarts(state_dir):
    sparks = FakeSparks()
    clock = {"t": 0.0}
    wd.run_forever(sparks, wd.Detector(), act=False, iterations=60,
                   clock=lambda: clock["t"], sleep=lambda dt: clock.__setitem__("t", clock["t"] + dt),
                   health=lambda: _health(rounds=1))
    assert sparks.restarts == []
    assert json.loads((state_dir / "state.json").read_text())["state"] == "wedged"


def test_low_memory_recycles_when_idle_and_forces_when_critical():
    d = wd.Detector()
    d.reset(0)
    d.observe_ranks(0, {"spark-2d31": {"restart_count": 0, "mem_available_gib": 2.5}})
    assert d.observe(0, _health(running=3, rounds=1)).state == "healthy"   # busy: wait for idle
    assert d.observe(15, _health(running=0, rounds=2)).state == "recycle"
    d.observe_ranks(30, {"spark-2d31": {"restart_count": 0, "mem_available_gib": 0.6}})
    v = d.observe(30, _health(running=5, rounds=3))
    assert v.state == "recycle" and "OOM imminent" in v.reason


def test_recycle_triggers_a_restart(state_dir):
    class LowMem(FakeSparks):
        def ranks(self):
            return {"spark-20ad": {"restart_count": 0, "mem_available_gib": 0.5}}
    sparks = LowMem()
    _run(sparks, lambda: _health(rounds=1, running=0), 30)
    assert len(sparks.restarts) == 1 and "MemAvailable" in sparks.restarts[0]


def test_restart_keeps_the_verdict_fresh(state_dir, monkeypatch):
    import time as _t
    monkeypatch.setattr(wd, "POLL_SECONDS", 0.05)
    seen = []

    class Slow(FakeSparks):
        def restart(self, reason):
            for _ in range(6):
                seen.append(json.loads((state_dir / "state.json").read_text())["updated"])
                _t.sleep(0.05)
            return True, "slow"
    ok, detail = wd.restart_publishing(Slow(), "drill")
    assert ok and detail == "slow"
    assert len(set(seen)) >= 3          # refreshed while the restart ran


def test_restart_exception_is_reported_not_raised(state_dir):
    class Boom(FakeSparks):
        def restart(self, reason):
            raise RuntimeError("ssh died")
    ok, detail = wd.restart_publishing(Boom(), "x")
    assert not ok and "ssh died" in detail
