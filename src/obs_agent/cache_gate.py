"""Cache-prefix gate: no production restart or merge of cache-sensitive code without a matching
passing live cache regression report (bead vault-3px5).

CACHE-PREFIX WARNING: this module decides whether cache-sensitive code may go live. Keep its
file-set derivation tied to the ``CACHE-PREFIX`` markers so it cannot drift from
docs/cache-proxy-safety.md.

Why: a cache break raises no error and no unit test caught any of the real break mechanisms;
the checklist was a convention relayed by message and was skipped (OBS 653c463 reached
production unchecked on 2026-10-12). Daniel 2026-10-13/15: enforce it, "potentially by like
hooks".

How it decides (``check``):

1. The code about to run is identified by git HEAD plus a hash of uncommitted changes, and the
   CLIProxyAPI binary/config hash (``code_identity``).
2. The code running now is read from the record telegram_main writes at start
   (``running-code.json``). Unknown -> every cache-sensitive file counts as touched.
3. Touched cache-sensitive files = files that changed between the two and carry a
   ``CACHE-PREFIX`` marker, plus ``src/obs_agent/tools.py`` (tool schemas are part of every
   request prefix), ``pyproject.toml``/``uv.lock`` (the bundled Claude Code CLI version) and a
   changed CLIProxyAPI binary or config. None touched -> pass at no cost.
4. Otherwise a ``gate.json`` written by ``scripts/cache_regression.py`` must exist whose result is
   PASS on both provider routes, for the full scenario set, for exactly this code identity, with
   the proxy under test having loaded this ``src/cache_proxy.py``.

Overrides: ``--skip-cache-gate REASON`` on ``python -m obs_agent.maintenance_restart`` and
``/restart skip-cache-gate REASON`` in Telegram. Every bypass is appended to the bypass log so
reflections and weekly reviews see it. The kill switch still stops unconditionally. Every
Supervisor start uses cache_launch to select an admitted snapshot or the last admitted fallback;
if neither is usable it logs an emergency current-code start rather than keeping OBS down.
A matching explicit maintenance override remains valid for five minutes, but never replaces
the last gated fallback. A plain restart that skips a failing gate is logged as well.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "CACHE-PREFIX"
EXTRA_SENSITIVE = ("src/obs_agent/tools.py", "pyproject.toml", "uv.lock")
SCAN_DIRS = ("src", "scripts", "deploy")
IDENTITY_PATHS = ("src", "scripts", "deploy", "pyproject.toml", "uv.lock")
REQUIRED_PROVIDERS = ("anthropic", "codex")
CLIPROXY_DIR = Path(os.environ.get("OBS_CLIPROXY_DIR", "/workspace/runtime/cliproxy"))
STATE_DIR = Path(os.environ.get("OBS_STATE_DIR", "/workspace/runtime/state"))
REPORT_ROOT = Path(os.environ.get("CACHE_REGRESSION_OUT", "/workspace/runtime/cache-regression"))


def running_record_path() -> Path:
    return Path(os.environ.get("OBS_RUNNING_CODE_FILE") or (STATE_DIR / "obs-running-code.json"))


def bypass_log_path() -> Path:
    return Path(os.environ.get("OBS_CACHE_GATE_LOG") or (STATE_DIR / "cache-gate-bypass.jsonl"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def cliproxy_identity(cliproxy_dir: Path | None = None) -> str | None:
    base = Path(cliproxy_dir or CLIPROXY_DIR)
    parts = [_sha256_file(base / "cli-proxy-api"), _sha256_file(base / "config.yaml")]
    if not any(parts):
        return None
    return hashlib.sha256("|".join(p or "-" for p in parts).encode()).hexdigest()


def code_identity(repo: Path, *, cliproxy_dir: Path | None = None, ref: str | None = None) -> dict:
    """Working-tree identity, or the clean committed bytes of an explicit push target."""
    repo = Path(repo)
    if ref is not None:
        head = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        source = subprocess.run(["git", "-C", str(repo), "show", f"{head}:src/cache_proxy.py"],
                                capture_output=True, check=False)
        return {"head": head, "dirty": "clean", "cliproxy": cliproxy_identity(cliproxy_dir),
                "proxy_source_sha256": hashlib.sha256(source.stdout).hexdigest() if source.returncode == 0 else None}
    head = _git(repo, "rev-parse", "HEAD").strip()
    diff = _git(repo, "diff", "HEAD", "--", *IDENTITY_PATHS)
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "--", "src", "scripts", "deploy")
    untracked_files = [line for line in untracked.splitlines() if line.strip() and not line.endswith(".pyc")]
    dirty_payload = diff
    for rel in sorted(untracked_files):
        dirty_payload += f"\n?? {rel} {_sha256_file(repo / rel)}"
    dirty = hashlib.sha256(dirty_payload.encode()).hexdigest() if dirty_payload.strip() else "clean"
    return {
        "head": head,
        "dirty": dirty,
        "cliproxy": cliproxy_identity(cliproxy_dir),
        "proxy_source_sha256": _sha256_file(repo / "src" / "cache_proxy.py"),
    }


def sensitive_files(repo: Path, ref: str | None = None) -> set[str]:
    """Repo-relative files that carry the CACHE-PREFIX marker, plus the fixed extras."""
    repo = Path(repo)
    found: set[str] = set(EXTRA_SENSITIVE)
    if ref is not None:
        paths = _git(repo, "ls-tree", "-r", "--name-only", ref, "--", *SCAN_DIRS).splitlines()
        for rel in paths:
            if MARKER in _git(repo, "show", f"{ref}:{rel}"):
                found.add(rel)
        return found
    for top in SCAN_DIRS:
        root = repo / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in (".pyc", ".pyo") or "__pycache__" in path.parts:
                continue
            try:
                if MARKER in path.read_text(encoding="utf-8", errors="ignore"):
                    found.add(path.relative_to(repo).as_posix())
            except OSError:
                continue
    return found


def changed_files(repo: Path, base: str | None, ref: str | None = None) -> set[str] | None:
    """Files changed from *base* to the working tree (None = base unknown)."""
    if not base:
        return None
    try:
        committed = _git(Path(repo), "diff", "--name-only", base, *([ref] if ref else []), "--", *IDENTITY_PATHS)
    except subprocess.CalledProcessError:
        return None
    if ref is not None:
        return set(committed.splitlines())
    untracked = _git(Path(repo), "ls-files", "--others", "--exclude-standard", "--", "src", "scripts", "deploy")
    return {line.strip() for line in (committed + "\n" + untracked).splitlines() if line.strip()}


def read_running_record() -> dict | None:
    try:
        data = json.loads(running_record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_running_record(repo: Path, *, pid: int | None = None) -> dict | None:
    """Called by telegram_main at start: record exactly which code is now running."""
    try:
        ident = code_identity(repo)
    except Exception:
        return None
    ident["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    ident["pid"] = pid if pid is not None else os.getpid()
    ident["repo"] = str(repo)
    path = running_record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ident, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return ident


def _same_code(report: dict, target: dict) -> tuple[bool, str]:
    code = report.get("code") or {}
    if code.get("head") != target["head"] or code.get("dirty") != target["dirty"]:
        return False, "different code identity"
    if target.get("proxy_source_sha256") and report.get("proxy_source") != target["proxy_source_sha256"]:
        return False, "proxy under test did not load this src/cache_proxy.py"
    if code.get("cliproxy") != target.get("cliproxy"):
        return False, "different CLIProxyAPI"
    return True, "match"


# Documented intermittent FAIL shapes (docs/cache-proxy-safety.md §How to read the runner's
# result). A FAIL is accepted on a passing rerun ONLY when every one of its bad pairs matches one
# of these rows exactly (ratio bounds inclusive); any other class, ratio or scenario, or a FAIL
# without bad pairs blocks. Keep this table and the doc identical — tests/test_cache_gate.py
# checks both directions. Bands are only as wide as the evidence (V1 / branch D verifier,
# 2026-10-16): a byte-intact but intermittent cache-key regression on our side would look like a
# partial GPT append, so an unobserved ratio must block until observations justify it.
# 1. GPT route, class `append`, ratio exactly 0: our bytes were a perfect prefix, the provider read
#    nothing (codex boundary_marker 2026-10-12; codex cch_collision 2026-10-15; codex
#    reminder_block 2026-10-16). Each passed on rerun.
# 2. GPT route, class `append`, ratio exactly 0.835: the single observed partial read, codex
#    boundary_marker on 2026-10-15 (cached 15104 of 18089), an exact point and not a band. Bead
#    ops-1.11.2 (2026-10-16): wire bodies pure append, same cache key, and 15104 is a cached length
#    shared across conversations (provider served a shorter shared-prefix entry). Only two
#    pure-append partial reads are known (both 15104), so no other partial ratio is accepted.
# 3. Claude route, scenario `recovery_multi`, class `tail-block-append`, ratio 0.85-0.95: the open
#    "Continue from where you left off." mechanism (bead vault-1r7n.9). Observed 0.8721, 0.9216,
#    0.944.
KNOWN_INTERMITTENT_SHAPES = (
    {"provider": "codex", "scenario": None, "class": "append", "min_ratio": 0.0, "max_ratio": 0.0},
    {"provider": "codex", "scenario": None, "class": "append", "min_ratio": 0.835, "max_ratio": 0.835},
    {"provider": "anthropic", "scenario": "recovery_multi", "class": "tail-block-append",
     "min_ratio": 0.85, "max_ratio": 0.95},
)
RERUN_STATUSES_WITHOUT_SHAPE = ("PROVIDER-ERROR", "SKIPPED")
# Rerun rule (bead ops-1.11.3; docs/cache-proxy-safety.md "Rerun rule"). Reruns of one scenario
# for the same code are read in time order; PROVIDER-ERROR/SKIPPED reruns are no verdict and are
# skipped. Accept only when (a) no rerun FAILs after a passing rerun, (b) at most
# RERUN_FAILS_ALLOWED reruns FAIL before the first pass, each itself a documented shape, and
# (c) the scenario then passes RERUN_PASSES_AFTER_FAIL times when any FAIL was seen (the full
# run's or a rerun's), or once when the full run had no cache verdict (PROVIDER-ERROR/SKIPPED).
# One lucky pass after a failure is no longer enough, and retrying until a pass cannot hide a
# flaky regression of ours: a later failure, or a second failed rerun, blocks this code identity.
RERUN_PASSES_AFTER_FAIL = 2
RERUN_FAILS_ALLOWED = 1


def known_intermittent(provider: str, scenario: str, failure: dict | None) -> tuple[bool, str]:
    """Is this non-PASS scenario result one the gate may accept on a passing rerun?"""
    status = (failure or {}).get("status")
    if status in RERUN_STATUSES_WITHOUT_SHAPE:
        return True, status  # not a cache verdict at all: the route did not run cleanly
    pairs = (failure or {}).get("bad_pairs") or []
    if not pairs:
        return False, "FAIL without bad pairs (crash or unrecorded shape)"
    for pair in pairs:
        cls, ratio = pair.get("class"), pair.get("ratio")
        if not isinstance(ratio, (int, float)):
            return False, f"{cls} with no ratio"
        if not any(
            shape["provider"] == provider
            and shape["scenario"] in (None, scenario)
            and shape["class"] == cls
            and shape["min_ratio"] <= ratio <= shape["max_ratio"]
            for shape in KNOWN_INTERMITTENT_SHAPES
        ):
            return False, f"{cls} at {ratio} is not a documented intermittent shape"
    return True, "documented shape"


def rerun_verdict(provider: str, scenario: str, original_status: str, reruns: list[dict]) -> tuple[bool, str]:
    """Apply the rerun rule to one documented non-PASS scenario. ``reruns`` are later same-code
    reports in chronological order (oldest first)."""
    fails = 0 if original_status in RERUN_STATUSES_WITHOUT_SHAPE else 1
    rerun_fails = passes = 0
    for rerun in reruns:
        status = (rerun.get("scenario_results") or {}).get(provider, {}).get(scenario)
        if status is None or status in RERUN_STATUSES_WITHOUT_SHAPE:
            continue  # scenario not rerun here, or the route did not run cleanly: no verdict
        if status == "PASS":
            passes += 1
            continue
        if passes:
            return False, f"rerun {status} after a passing rerun"
        failure = ((rerun.get("scenario_failures") or {}).get(provider) or {}).get(scenario) or {"status": status}
        known, why = known_intermittent(provider, scenario, failure)
        if not known:
            return False, f"rerun {status}: {why}"
        fails += 1
        rerun_fails += 1
        if rerun_fails > RERUN_FAILS_ALLOWED:
            return False, f"{rerun_fails} failed reruns (at most {RERUN_FAILS_ALLOWED})"
    needed = RERUN_PASSES_AFTER_FAIL if fails else 1
    if passes < needed:
        return False, f"{passes} of {needed} passing reruns"
    return True, f"{passes} passing reruns"


def _report_matches(report: dict, target: dict, reruns: list[dict] | None = None) -> tuple[bool, str]:
    """A full-set report for this code that passed — directly, or with every non-PASS scenario
    a DOCUMENTED intermittent shape (``KNOWN_INTERMITTENT_SHAPES``) whose later reruns of the same
    code satisfy the rerun rule (``rerun_verdict``; docs/cache-proxy-safety.md "Rerun rule")."""
    same, why = _same_code(report, target)
    if not same:
        return False, why
    if report.get("scenarios") not in (None, "all"):
        return False, "not the full scenario set"
    per = report.get("scenario_results") or {}
    # Both routes must have actually run: the report's top-level `providers` lists every route
    # the runner ran (scripts/cache_regression.py), and every scenario result must belong to one.
    providers = set(report.get("providers") or [])
    missing = [p for p in REQUIRED_PROVIDERS if p not in providers]
    if missing:
        return False, f"providers missing {missing}"
    if report.get("result") == "PASS":
        return True, "match"
    if not per:
        return False, f"result {report.get('result')}"
    missing = [p for p in REQUIRED_PROVIDERS if p not in per]
    if missing:
        return False, f"providers missing {missing}"
    unresolved = []
    for provider, scenarios in per.items():
        for scenario, status in scenarios.items():
            if status == "PASS":
                continue
            failure = ((report.get("scenario_failures") or {}).get(provider) or {}).get(scenario)
            if failure is None:
                failure = {"status": status}
            known, shape_why = known_intermittent(provider, scenario, failure)
            if not known:
                unresolved.append(f"{provider}/{scenario}={status} ({shape_why})")
                continue
            passed, rerun_why = rerun_verdict(provider, scenario, status, reruns or [])
            if not passed:
                unresolved.append(f"{provider}/{scenario}={status} ({rerun_why})")
    if unresolved:
        return False, f"result {report.get('result')}; not passed on rerun: {', '.join(unresolved[:6])}"
    return True, "match (non-PASS scenarios passed on rerun)"


def find_passing_report(target: dict, report_root: Path | None = None) -> tuple[Path | None, list[str]]:
    root = Path(report_root or REPORT_ROOT)
    notes: list[str] = []
    if not root.is_dir():
        return None, [f"no reports under {root}"]
    loaded: list[tuple[float, Path, dict]] = []
    for gate_file in root.glob("*/gate.json"):
        try:
            loaded.append((gate_file.stat().st_mtime, gate_file.parent, json.loads(gate_file.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    loaded.sort(key=lambda item: item[0], reverse=True)
    for index, (mtime, directory, report) in enumerate(loaded):
        reruns = [
            other for other_mtime, _, other in sorted(loaded, key=lambda item: item[0])
            if other_mtime >= mtime and other is not report and _same_code(other, target)[0]
        ]  # oldest first: the rerun rule reads them in order
        ok, why = _report_matches(report, target, reruns)
        if ok:
            return directory, notes
        if len(notes) < 5:
            notes.append(f"{directory.name}: {why}")
    return None, notes


@dataclass
class GateResult:
    ok: bool
    touched: list[str] = field(default_factory=list)
    report: Path | None = None
    message: str = ""
    target: dict = field(default_factory=dict)


def check(repo: Path, *, base: str | None = None, report_root: Path | None = None,
          target_ref: str | None = None) -> GateResult:
    repo = Path(repo)
    release = os.environ.get("OBS_CACHE_GATE_RELEASE")
    candidate = os.environ.get("OBS_CACHE_GATE_CANDIDATE")
    if target_ref is None and release and candidate and repo.resolve() == Path(release).resolve():
        repo = Path(candidate)  # a daemon in a saved release still gates the next candidate
    target = code_identity(repo, ref=target_ref) if target_ref else code_identity(repo)
    running = read_running_record()
    if base is None and running:
        base = running.get("head")
        if running.get("dirty") not in (None, "clean"):
            base = None  # running code had uncommitted changes: cannot diff from a commit
    sensitive = sensitive_files(repo, target_ref) if target_ref else sensitive_files(repo)
    if base:
        try:
            sensitive |= sensitive_files(repo, base)  # removed markers must not hide a change
        except subprocess.CalledProcessError:
            pass  # unknown base already forces the conservative changed-files path
    changed = changed_files(repo, base, target_ref) if target_ref else changed_files(repo, base)
    if changed is None:
        touched = sorted(sensitive)
        why = "running code unknown"
    else:
        touched = sorted(changed & sensitive)
        why = f"changed since {base[:12]}"
    if running and running.get("cliproxy") and target.get("cliproxy") and running["cliproxy"] != target["cliproxy"]:
        touched.append("CLIProxyAPI (/workspace/runtime/cliproxy binary or config)")
    if changed is not None and not touched:
        return GateResult(True, [], None, "cache gate: no cache-sensitive change; pass", target)
    report, notes = find_passing_report(target, report_root)
    if report is not None:
        return GateResult(True, touched, report, f"cache gate: passing regression report {report}", target)
    listed = ", ".join(touched[:12]) + (f" (+{len(touched) - 12} more)" if len(touched) > 12 else "")
    message = (
        f"cache gate REFUSED: cache-sensitive files ({why}): {listed}. There is no passing live cache "
        f"regression report for exactly this code (HEAD {target['head'][:12]}, "
        f"uncommitted {target['dirty'][:12]}). Run, from this checkout: "
        ".venv/bin/python scripts/cache_regression.py (scratch proxy first, see "
        "docs/cache-proxy-safety.md), then retry. Emergency only: --skip-cache-gate \"<reason>\" "
        "(logged)."
    )
    if notes:
        message += " Recent reports: " + "; ".join(notes)
    return GateResult(False, touched, None, message, target)


def log_bypass(*, reason: str, source: str, result: GateResult | None) -> None:
    entry = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
        "reason": reason,
        "touched": result.touched if result else None,
        "head": (result.target or {}).get("head") if result else None,
        "code": result.target if result else None,
        "gate_message": result.message if result else None,
    }
    path = bypass_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
