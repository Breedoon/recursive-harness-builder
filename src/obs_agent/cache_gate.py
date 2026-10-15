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
reflections and weekly reviews see it. The kill switch (``--plain``, ``/restart plain``,
supervisorctl) and crash/watchdog restarts are never gated, so a crash-loop fix can always be
deployed; a plain restart that skips a failing gate is logged as well.
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


def code_identity(repo: Path, *, cliproxy_dir: Path | None = None) -> dict:
    """HEAD commit, hash of uncommitted tracked changes, and CLIProxyAPI identity."""
    repo = Path(repo)
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


def sensitive_files(repo: Path) -> set[str]:
    """Repo-relative files that carry the CACHE-PREFIX marker, plus the fixed extras."""
    repo = Path(repo)
    found: set[str] = set(EXTRA_SENSITIVE)
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


def changed_files(repo: Path, base: str | None) -> set[str] | None:
    """Files changed from *base* to the working tree (None = base unknown)."""
    if not base:
        return None
    try:
        committed = _git(Path(repo), "diff", "--name-only", f"{base}", "--", *IDENTITY_PATHS)
    except subprocess.CalledProcessError:
        return None
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


def _report_matches(report: dict, target: dict) -> tuple[bool, str]:
    if report.get("result") != "PASS":
        return False, f"result {report.get('result')}"
    providers = set(report.get("providers") or [])
    missing = [p for p in REQUIRED_PROVIDERS if p not in providers]
    if missing:
        return False, f"providers missing {missing}"
    if report.get("scenarios") not in (None, "all"):
        return False, "not the full scenario set"
    code = report.get("code") or {}
    if code.get("head") != target["head"] or code.get("dirty") != target["dirty"]:
        return False, "different code identity"
    if target.get("proxy_source_sha256") and report.get("proxy_source") != target["proxy_source_sha256"]:
        return False, "proxy under test did not load this src/cache_proxy.py"
    if target.get("cliproxy") and code.get("cliproxy") not in (None, target["cliproxy"]):
        return False, "different CLIProxyAPI"
    return True, "match"


def find_passing_report(target: dict, report_root: Path | None = None) -> tuple[Path | None, list[str]]:
    root = Path(report_root or REPORT_ROOT)
    notes: list[str] = []
    if not root.is_dir():
        return None, [f"no reports under {root}"]
    for gate_file in sorted(root.glob("*/gate.json"), reverse=True):
        try:
            report = json.loads(gate_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ok, why = _report_matches(report, target)
        if ok:
            return gate_file.parent, notes
        if len(notes) < 5:
            notes.append(f"{gate_file.parent.name}: {why}")
    return None, notes


@dataclass
class GateResult:
    ok: bool
    touched: list[str] = field(default_factory=list)
    report: Path | None = None
    message: str = ""
    target: dict = field(default_factory=dict)


def check(repo: Path, *, base: str | None = None, report_root: Path | None = None) -> GateResult:
    repo = Path(repo)
    target = code_identity(repo)
    running = read_running_record()
    if base is None and running:
        base = running.get("head")
        if running.get("dirty") not in (None, "clean"):
            base = None  # running code had uncommitted changes: cannot diff from a commit
    sensitive = sensitive_files(repo)
    changed = changed_files(repo, base)
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
        "gate_message": result.message if result else None,
    }
    path = bypass_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
