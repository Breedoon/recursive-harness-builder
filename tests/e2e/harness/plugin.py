"""pytest plugin for obs-e2e (obs-22 contract C3: markers, fixtures, collection, timing).

Loaded with ``-p tests.e2e.harness.plugin`` by the runner's per-lane workers.

Markers:
- ``@pytest.mark.needs(fresh_forum=False, credential="dev"|"sub"|"claude-2", all_lanes=False)``:
  sharding needs. ``credential`` restricts the test to lanes of that credential class;
  ``all_lanes=True`` runs the test on every lane (smoke and isolation suites).
- ``@pytest.mark.known_bug("<bead>")``: strict xfail, allowed only when
  ``tests/e2e/known_bugs.yaml`` qualifies the bead (design-v3 §2.4: a bead, two
  reproductions on the R13 baseline by run id, a one-line product-bug classification
  naming the OBS code path). An unqualified marker is an error, never a silent xfail.
- ``@pytest.mark.suite("smoke"|"isolation"|"ported"|"journeys"|"stress"|"binary"|"lifecycle"|
  "damaged-history"|"local"|"incident"|"migration")``: suite membership for ``--suite``.
- ``@pytest.mark.both_runtimes``: the test runs once per runtime in the same lane
  (``runtime`` fixture: ``cc-2.1.59`` and ``obs-cli``; the obs-cli half is skipped,
  visibly, until the run has a ``--cli-path`` or the code lists a release).

Fixtures: ``lane`` (session-scoped :class:`~tests.e2e.harness.lane.Lane`) and
``runtime``. Environment: ``OBS_E2E_LANE``, ``OBS_E2E_RUN_DIR`` (set by the runner).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

SUITES = ("smoke", "isolation", "ported", "journeys", "stress", "binary", "lifecycle", "damaged-history",
          "local", "incident", "migration")
RUNTIMES = ("cc-2.1.59", "obs-cli")
KNOWN_BUGS = Path(__file__).resolve().parents[1] / "known_bugs.yaml"


def pytest_addoption(parser):
    group = parser.getgroup("obs-e2e")
    group.addoption("--e2e-suite", default=None, help="comma-separated suite names or marker/keyword tags")
    group.addoption("--e2e-repeat", type=int, default=1, help="run every selected test K times (vm-scripts-3 cycles)")


def pytest_configure(config):
    for line in (
        "needs(fresh_forum=False, credential=None, all_lanes=False): obs-e2e sharding needs",
        "known_bug(bead): strict xfail for a qualified known product bug (tests/e2e/known_bugs.yaml)",
        "suite(*names): obs-e2e suite membership",
        "both_runtimes: run once per CLI runtime in the same lane",
    ):
        config.addinivalue_line("markers", line)


def _marker_info(item) -> dict:
    needs = {}
    for mark in item.iter_markers("needs"):
        needs.update(mark.kwargs)
    suites: list[str] = []
    for mark in item.iter_markers("suite"):
        suites.extend(str(a) for a in mark.args)
    known = [str(a) for m in item.iter_markers("known_bug") for a in m.args]
    return {
        "needs": needs,
        "suite": sorted(set(suites)),
        "both_runtimes": item.get_closest_marker("both_runtimes") is not None,
        "known_bug": known[0] if known else None,
        "markers": sorted({m.name for m in item.iter_markers()}),
    }


def _selected(item, wanted: list[str]) -> bool:
    info = _marker_info(item)
    for tag in wanted:
        if tag in info["suite"] or tag in info["markers"] or tag in item.keywords:
            return True
    return False


def load_known_bugs(path: Path = KNOWN_BUGS) -> dict[str, dict]:
    """Parse known_bugs.yaml (PyYAML if present; else JSON or the '- key: value' subset)."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    entries = None
    try:
        import yaml  # type: ignore

        entries = yaml.safe_load(text)
    except ImportError:
        try:
            entries = json.loads(text)
        except ValueError:
            entries, current = [], None
            for raw in text.splitlines():
                line = raw.split(" #", 1)[0].rstrip()
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if line.startswith("- "):
                    current = {}
                    entries.append(current)
                    line = "  " + line[2:]
                if current is None or ":" not in line:
                    continue
                key, _, value = line.strip().partition(":")
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    current[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
                else:
                    current[key] = value.strip("'\"")
    out: dict[str, dict] = {}
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("bead"):
            out[str(entry["bead"])] = entry
    return out


def known_bug_qualified(entry: dict | None) -> tuple[bool, str]:
    if not entry:
        return False, "no entry"
    runs = entry.get("runs") or entry.get("reproductions") or []
    if len([r for r in runs if str(r).strip()]) < 2:
        return False, "needs two reproduction run ids on the R13 baseline"
    if not str(entry.get("classification") or "").strip():
        return False, "needs a one-line product-bug classification naming the OBS code path"
    return True, "qualified"


def pytest_collection_modifyitems(session, config, items):
    wanted = config.getoption("--e2e-suite")
    if wanted:
        tags = [t.strip() for t in wanted.split(",") if t.strip()]
        keep, drop = [], []
        for item in items:
            (keep if _selected(item, tags) else drop).append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep
    repeat = int(config.getoption("--e2e-repeat") or 1)
    if repeat > 1:
        items[:] = [item for _ in range(repeat) for item in items]
    bugs = load_known_bugs()
    for item in items:
        bead = _marker_info(item)["known_bug"]
        if bead:
            ok, why = known_bug_qualified(bugs.get(bead))
            if ok:
                item.add_marker(pytest.mark.xfail(strict=True, reason=f"known bug {bead}"))
            else:
                item.add_marker(pytest.mark.obs_e2e_unqualified_known_bug(bead=bead, why=why))
    out = os.environ.get("OBS_E2E_COLLECT_OUT")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(
            json.dumps([{"nodeid": item.nodeid, **_marker_info(item)} for item in items], indent=1), encoding="utf-8"
        )


def pytest_generate_tests(metafunc):
    if "runtime" in metafunc.fixturenames and metafunc.definition.get_closest_marker("both_runtimes"):
        metafunc.parametrize("runtime", RUNTIMES, indirect=True)


def _obs_cli_available(lane) -> bool:
    if lane.manifest.get("cli_path"):
        return True
    release_file = Path(lane.info["code_dir"]) / "deploy" / "obs-cli-releases.json"
    try:
        return bool((json.loads(release_file.read_text(encoding="utf-8")).get("releases") or {}))
    except (OSError, ValueError):
        return False


@pytest.fixture
def runtime(request, lane):
    value = getattr(request, "param", "cc-2.1.59")
    if value == "obs-cli" and not _obs_cli_available(lane):
        pytest.skip("obs-cli runtime unavailable in this run (no --cli-path, no release listed)")
    return value


def _lane_env() -> tuple[str, Path]:
    name = os.environ.get("OBS_E2E_LANE")
    run_dir = os.environ.get("OBS_E2E_RUN_DIR")
    if not name or not run_dir:
        pytest.exit("obs-e2e: OBS_E2E_LANE and OBS_E2E_RUN_DIR must be set (run tests through `obs-e2e run`)", 2)
    return name, Path(run_dir)


@pytest.fixture(scope="session")
async def lane():
    from .lane import Lane

    name, run_dir = _lane_env()
    obj = Lane(run_dir / "lanes" / name, run_dir)
    await obj.start()
    obj.timing.record("suite_start")
    try:
        yield obj
    finally:
        obj.timing.record("suite_end")
        await obj.close()


def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid)[:150]


@pytest.fixture(autouse=True)
def _obs_e2e_test_bookkeeping(request, lane):
    mark = request.node.get_closest_marker("obs_e2e_unqualified_known_bug")
    if mark is not None:
        pytest.fail(f"@known_bug({mark.kwargs['bead']!r}) is not qualified in tests/e2e/known_bugs.yaml: {mark.kwargs['why']}")
    spent = sum(float(e.get("cost_usd") or 0) for e in lane.events.find("turn_end", after=0))
    cap = float(getattr(lane.profile, "spend_cap_usd", 0) or 0)
    if cap and spent > cap:
        pytest.fail(f"lane {lane.name} spend cap reached: ${spent:.2f} > ${cap:.2f} (profile {lane.profile.name})")
    lane.begin_test(request.node.nodeid)
    lane.timing.record("test_start", nodeid=request.node.nodeid)
    started = time.time()
    try:
        yield
    finally:
        lane.timing.record("test_end", nodeid=request.node.nodeid, duration_s=time.time() - started)
        log_dir = lane.run_dir / "tests" / lane.name
        log_dir.mkdir(parents=True, exist_ok=True)
        events = lane.events.events[lane.events.cursor:]
        (log_dir / f"{_slug(request.node.nodeid)}.events.jsonl").write_text(
            "".join(json.dumps({k: v for k, v in e.items() if not k.startswith("_")}, default=str) + "\n" for e in events),
            encoding="utf-8",
        )
