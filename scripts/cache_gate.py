#!/usr/bin/env python3
"""Cache-prefix gate CLI (bead vault-3px5). Logic: src/obs_agent/cache_gate.py.

CACHE-PREFIX WARNING: this is the enforcement point for docs/cache-proxy-safety.md.

  cache_gate.py check [--base SHA]   exit 0 = may go live, 3 = refused (prints why and what to run)
  cache_gate.py identity             print the code identity a regression report must match
  cache_gate.py install-hook         install .git/hooks/pre-push: a push to main is refused while
                                     cache-sensitive changes lack a passing report
  cache_gate.py pre-push             (called by the hook; reads git's stdin)

Override for a push: OBS_CACHE_GATE_SKIP="<reason>" git push ... (logged like a restart bypass).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from obs_agent import cache_gate  # noqa: E402

HOOK = """#!/bin/sh
# Installed by scripts/cache_gate.py install-hook (vault-3px5).
exec "{python}" "{script}" pre-push "$@"
"""


def _pre_push(argv: list[str]) -> int:
    # git runs hooks from the root of the worktree being pushed; gate THAT checkout.
    repo = Path(os.popen("git rev-parse --show-toplevel").read().strip() or REPO)
    rc = 0
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref not in ("refs/heads/main", "refs/heads/master") or set(local_sha) == {"0"}:
            continue
        base = None if set(remote_sha) == {"0"} else remote_sha
        result = cache_gate.check(repo, base=base, target_ref=local_sha)
        if result.ok:
            print(result.message, file=sys.stderr)
            continue
        reason = os.environ.get("OBS_CACHE_GATE_SKIP", "").strip()
        if reason:
            cache_gate.log_bypass(reason=reason, source=f"git pre-push {remote_ref}", result=result)
            print(f"cache gate bypassed for push (logged): {reason}", file=sys.stderr)
            continue
        print(result.message.replace("--skip-cache-gate", "OBS_CACHE_GATE_SKIP=<reason> git push"), file=sys.stderr)
        rc = 3
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--base", default=None, help="commit currently live (default: running-code record)")
    sub.add_parser("identity")
    sub.add_parser("install-hook")
    sub.add_parser("pre-push")
    args, rest = ap.parse_known_args(argv)
    if args.cmd == "check":
        result = cache_gate.check(REPO, base=args.base)
        print(result.message)
        if result.touched:
            print("touched: " + ", ".join(result.touched))
        return 0 if result.ok else 3
    if args.cmd == "identity":
        print(json.dumps(cache_gate.code_identity(REPO), indent=1))
        return 0
    if args.cmd == "install-hook":
        git_dir = Path(os.popen(f"git -C '{REPO}' rev-parse --git-common-dir").read().strip())
        if not git_dir.is_absolute():
            git_dir = REPO / git_dir
        hook = git_dir / "hooks" / "pre-push"
        hook.write_text(HOOK.format(python=sys.executable, script=Path(__file__).resolve()))
        hook.chmod(0o755)
        print(f"installed {hook}")
        return 0
    return _pre_push(rest)


if __name__ == "__main__":
    raise SystemExit(main())
