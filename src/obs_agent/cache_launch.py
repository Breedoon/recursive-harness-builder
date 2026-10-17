"""Supervisor launch admission with persistent, byte-verified fallback (vault-nsha).

CACHE-PREFIX WARNING: select an admitted source snapshot before importing the daemon.
Never let a crash turn the mutable development checkout into a deployment implicitly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from obs_agent import cache_gate as gate


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1) + "\n")
    temp.replace(path)


def _sensitive_bytes(repo: Path, names: set[str]) -> dict:
    return {name: gate._sha256_file(repo / name) for name in sorted(names)}


def _fallback(state: Path) -> tuple[dict | None, str]:
    try:
        record = json.loads((state / "current.json").read_text())
        release = Path(record["repo"])
        # A pointer alone is not proof: rehash the saved source on every launch.
        if gate.code_identity(release) != record["code"]:
            return None, "saved release bytes or external CLIProxyAPI changed"
        if not (release / "src/obs_agent/telegram_main.py").is_file():
            return None, "saved release has no daemon entrypoint"
        return record, "verified admitted release"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None, "no usable admitted release"


def snapshot(repo: Path, state: Path, target: dict, report: Path | None) -> dict:
    """Copy exact working bytes and a private git index; never reset the source checkout."""
    state.mkdir(parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=target["head"][:12] + "-", dir=state))
    # No shared object dependency: a source gc or repository removal must not invalidate rollback.
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo), str(release)],
                   check=True, capture_output=True)
    gate._git(release, "update-ref", "HEAD", target["head"])
    gate._git(release, "read-tree", target["head"])
    paths = set(gate._git(repo, "ls-files", "-z").split("\0"))
    paths.update(gate._git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--",
                           "src", "scripts", "deploy").split("\0"))
    for rel in sorted(paths - {""}):
        source = repo / rel
        if not source.is_file() or source.suffix in (".pyc", ".pyo"):
            continue
        dest = release / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    for name in (".env", ".venv", ".obs-agent"):
        if (repo / name).exists():
            (release / name).symlink_to(repo / name, target_is_directory=(repo / name).is_dir())
    if gate.code_identity(repo) != target or gate.code_identity(release) != target:
        raise RuntimeError("source changed while copying or snapshot identity mismatch")
    return {"repo": str(release), "code": target, "report": str(report) if report else None,
            "admitted_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def _restart_override(target: dict) -> str | None:
    try:
        entries = gate.bypass_log_path().read_text().splitlines()
    except OSError:
        return None
    for line in reversed(entries):
        try:
            entry = json.loads(line)
            source = entry.get("source", "")
            if source != "cli" and not source.startswith("telegram:/restart user="):
                continue  # a push, plain restart, or emergency start is not deployment approval
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(entry["at"])).total_seconds()
            if 0 <= age <= 300 and entry.get("code") == target:
                return entry["reason"]
        except (ValueError, KeyError, TypeError):
            continue
    return None


def select(repo: Path, state: Path, *, report_root: Path | None = None, dry_run: bool = False) -> dict:
    repo, state = Path(repo).resolve(), Path(state).resolve()
    previous, previous_reason = _fallback(state)
    result = None
    try:
        target = gate.code_identity(repo)
        report, notes = gate.find_passing_report(target, report_root)
        inherited = False
        if previous:
            baseline = Path(previous["repo"])
            names = gate.sensitive_files(repo) | gate.sensitive_files(baseline)
            inherited = _sensitive_bytes(repo, names) == _sensitive_bytes(baseline, names)
        override = _restart_override(target)
        if not report and not inherited and override:
            # Preserve the documented explicit escape hatch, but never call it a gated baseline.
            record = None if dry_run else snapshot(repo, state, target, None)
            answer = {"decision": "override", "selected": str(repo) if dry_run else record["repo"],
                      "code": target, "reason": f"explicit logged maintenance override: {override}"}
        elif report or inherited:
            reason = f"matching regression {report}" if report else "no sensitive byte changes from admitted release"
            if dry_run:
                return {"decision": "would-admit", "selected": str(repo), "code": target, "reason": reason}
            # Reuse an identical immutable release instead of making one on every crash.
            record = previous if previous and previous["code"] == target else snapshot(repo, state, target, report)
            _atomic_json(state / "current.json", record)
            answer = {"decision": "admitted", "selected": record["repo"], "code": target, "reason": reason}
        else:
            reason = "no matching full two-provider regression; " + "; ".join(notes)
            result = gate.GateResult(False, message=reason, target=target)
            answer = {"decision": "fallback" if previous else "emergency-current",
                      "selected": previous["repo"] if previous else str(repo), "code": target,
                      "reason": reason + "; " + previous_reason}
    except Exception as exc:
        # Selection problems must never strand OBS down. Prefer previously rehashed bytes.
        answer = {"decision": "fallback" if previous else "emergency-current",
                  "selected": previous["repo"] if previous else str(repo),
                  "reason": f"launch gate error: {type(exc).__name__}: {exc}; {previous_reason}"}
    if not dry_run:
        if answer["decision"] == "emergency-current":
            gate.log_bypass(reason=answer["reason"], source="supervisor launch", result=result)
        try:
            state.mkdir(parents=True, exist_ok=True)
            with (state / "launches.jsonl").open("a") as handle:
                handle.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **answer}) + "\n")
        except OSError as exc:
            print(f"cache launch audit unavailable: {exc}", file=sys.stderr)
    return answer


def install_control(destination: Path) -> Path:
    """Atomically install the selector independently of the mutable candidate source."""
    destination = destination.absolute()
    release = Path(tempfile.mkdtemp(prefix=destination.name + "-", dir=destination.parent))
    package = release / "obs_agent"
    package.mkdir()
    for name in ("__init__.py", "cache_gate.py", "cache_launch.py"):
        shutil.copy2(Path(__file__).parent / name, package / name)
    link = destination.with_suffix(".new")
    link.symlink_to(release, target_is_directory=True)
    link.replace(destination)
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("/workspace/obs"))
    parser.add_argument("--state", type=Path, default=gate.STATE_DIR / "obs-admitted-releases")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--install-control", type=Path)
    args = parser.parse_args(argv)
    if args.install_control:
        print(install_control(args.install_control))
        return 0
    answer = select(args.repo, args.state, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(answer, indent=1))
    else:
        print(f"cache launch: {answer['decision']}: {answer['reason']}", file=sys.stderr)
        print(answer["selected"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
