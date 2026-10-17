#!/usr/bin/env python3
"""One-command live cache regression run for the OBS cache proxy.

CACHE-PREFIX WARNING: the cache proxy (src/cache_proxy.py) and every place that
builds or replays conversation history are easy to break silently. A break
does not raise an error: the API simply stops reusing its cache and re-bills
the whole conversation (one session lost 391k tokens to one changed system
section on 2026-10-12). Unit tests did not catch any of the six real break
mechanisms found so far. This runner is the check; the full checklist is in
docs/cache-proxy-safety.md.

What it does
  1. Identity: asks the proxy which source it loaded (/health source_sha256)
     and compares it with src/cache_proxy.py on disk and at git HEAD.
  2. Runs every live scenario of scripts/prefix_repro.py (a real Claude Code
     CLI, cheap model, driven through the proxy) once per provider route:
     "anthropic" (Claude) and "codex" (GPT via CLIProxyAPI). Each scenario is a
     separate process with its own time limit.
  3. Fails fast on provider trouble: after --max-provider-errors consecutive
     scenarios on one route end in PROVIDER-ERROR (the upstream returned 5xx
     errors) the rest of that route is SKIPPED instead of burning time.
  4. Prints one summary and writes everything to --out-dir.

Statuses
  PASS            every request pair was a byte-append with >= threshold cache read
  FAIL            a cache-prefix regression (or a driver error with no upstream
                  5xx) -- read the pair diff, then run scripts/prefix_diff.py
  PROVIDER-ERROR  the scenario failed or timed out AND the upstream returned 5xx
                  errors meanwhile -- probably provider overload, not the proxy;
                  rerun later before trusting a PASS
  SKIPPED         not run because the route kept returning provider errors

Exit status: 0 all PASS; 1 at least one FAIL; 2 no FAIL but some
PROVIDER-ERROR/SKIPPED (inconclusive -- rerun that route later).

Examples
  # production proxy, both routes (about 20-40 min; spends a little credit)
  .venv/bin/python scripts/cache_regression.py
  # a scratch proxy you started from the working tree on another port
  .venv/bin/python scripts/cache_regression.py --base-url http://127.0.0.1:28942 \
      --log-dir /workspace/runtime/cache-proxy-scratch/new/requests
  # one route, a few scenarios
  .venv/bin/python scripts/cache_regression.py --providers anthropic ws_resume volatile_flip
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PREFIX_REPRO = HERE / "prefix_repro.py"
PROXY_SRC = REPO / "src" / "cache_proxy.py"
DEFAULT_BASE_URL = "http://127.0.0.1:28925"
DEFAULT_LOG_DIR = os.environ.get("CACHE_PROXY_REQUEST_LOG_DIR",
                                  "/workspace/runtime/logs/cache-proxy/requests")
OUT_ROOT = Path(os.environ.get("CACHE_REGRESSION_OUT", "/workspace/runtime/cache-regression"))
ANTHROPIC_ONLY = {"volatile_flip", "ws_resume", "recovered_poison"}  # mirrors prefix_repro.ANTHROPIC_ONLY


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(base_url: str) -> dict:
    out: dict = {"base_url": base_url}
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=10) as r:
            out["health"] = json.loads(r.read())
    except Exception as e:  # proxy down is itself a finding
        out["health_error"] = f"{type(e).__name__}: {e}"
    out["disk_sha256"] = _sha256(PROXY_SRC.read_bytes())
    try:
        head = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:src/cache_proxy.py"],
                              capture_output=True, check=True).stdout
        out["head_sha256"] = _sha256(head)
        out["head_commit"] = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                                            capture_output=True, text=True).stdout.strip()
    except Exception as e:
        out["head_error"] = str(e)
    return out


def scenario_names(provider: str) -> list[str]:
    listing = subprocess.run([sys.executable, str(PREFIX_REPRO), "--list"],
                             capture_output=True, text=True, check=True).stdout
    names = [ln.split()[0] for ln in listing.splitlines() if ln.strip()]
    return [n for n in names if provider == "anthropic" or n not in ANTHROPIC_ONLY]


def run_one(args, provider: str, name: str, out_dir: Path) -> dict:
    json_out = out_dir / f"{provider}-{name}.json"
    cmd = [sys.executable, str(PREFIX_REPRO), name, "--provider", provider,
           "--base-url", args.base_url, "--log-dir", args.log_dir,
           "--threshold", str(args.threshold), "--scenario-timeout", str(args.scenario_timeout),
           "--json-out", str(json_out)]
    if args.model and provider == "anthropic":
        cmd += ["--model", args.model]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.scenario_timeout + 120)
        stdout, rc = proc.stdout, proc.returncode
        (out_dir / f"{provider}-{name}.txt").write_text(stdout + "\n--- stderr ---\n" + proc.stderr)
    except subprocess.TimeoutExpired:
        stdout, rc = "", -1
    res = {"scenario": name, "provider": provider, "seconds": round(time.time() - t0), "rc": rc}
    try:
        r = json.loads(json_out.read_text())[0]
        res.update(status=r.get("status") or ("PASS" if r.get("ok") else "FAIL"),
                   sessions=r.get("sessions") or [], error=r.get("error"),
                   provider_errors=len(r.get("provider_errors") or []),
                   bad_pairs=[{"b": p.get("b"), "class": p.get("class"), "ratio": p.get("ratio")}
                              for p in r.get("pairs", []) if not p.get("ok")],
                   requests=r.get("requests") or [])
        if res["status"] == "ERROR":  # driver error without upstream 5xx: treat as a failure
            res["status"] = "FAIL"
    except Exception:
        res.update(status="FAIL", error="no result written (runner crashed or was killed)",
                   sessions=[], provider_errors=0, bad_pairs=[], requests=[])
    return res


def _code_identity() -> dict:
    """Identity of the code under test, taken when the run STARTS (vault-3px5)."""
    try:
        sys.path.insert(0, str(REPO / "src"))
        from obs_agent.cache_gate import code_identity
        return code_identity(REPO)
    except Exception as e:  # the report stays usable for humans even if identity fails
        return {"error": str(e)}


def _write_gate_report(out_dir: Path, *, args, ident: dict, live: str | None, results: list,
                       overall: str) -> None:
    """gate.json: what scripts/cache_gate.py (vault-3px5) matches before a restart or merge."""
    code = ident.get("code") or _code_identity()
    providers_run = sorted({r["provider"] for r in results if r.get("status") != "SKIPPED"})
    route_ok = {p: all(r.get("status") == "PASS" for r in results if r["provider"] == p)
                for p in {r["provider"] for r in results}}
    scenario_results: dict = {}
    scenario_failures: dict = {}
    for r in results:
        scenario_results.setdefault(r["provider"], {})[r["scenario"]] = r.get("status")
        if r.get("status") not in ("PASS", None):
            # The bad pairs' class and ratio decide whether the gate may accept this FAIL on a rerun:
            # only shapes documented in docs/cache-proxy-safety.md qualify (cache_gate.py).
            scenario_failures.setdefault(r["provider"], {})[r["scenario"]] = {
                "status": r.get("status"),
                "bad_pairs": r.get("bad_pairs") or [],
                "error": r.get("error"),
            }
    report = {
        "result": overall,
        "scenario_results": scenario_results,
        "scenario_failures": scenario_failures,
        # every route that actually ran; the gate refuses a report missing either required route
        "providers": providers_run,
        "providers_all_pass": sorted(p for p, ok in route_ok.items() if ok),
        "scenarios": "all" if not args.scenarios else list(args.scenarios),
        "code": code,
        "proxy_source": live,
        "base_url": args.base_url,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (out_dir / "gate.json").write_text(json.dumps(report, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", help="default: every scenario")
    ap.add_argument("--providers", default="anthropic,codex")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                    help="the request log dir OF THE PROXY UNDER TEST (scratch proxies have their own)")
    ap.add_argument("--model", default=None, help="Claude model for the anthropic route (default: harness Haiku)")
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--scenario-timeout", type=int, default=600)
    ap.add_argument("--max-provider-errors", type=int, default=2,
                    help="consecutive PROVIDER-ERROR results on one route before skipping the rest")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) if args.out_dir else OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    ident = identity(args.base_url)
    ident["code"] = _code_identity()
    (out_dir / "identity.json").write_text(json.dumps(ident, indent=1))
    live = (ident.get("health") or {}).get("source_sha256")
    print(f"proxy {args.base_url}: " + (f"pid {ident['health'].get('pid')} loaded {live[:12]}"
                                         if live else f"UNREACHABLE ({ident.get('health_error')})"))
    print(f"  src/cache_proxy.py on disk {ident['disk_sha256'][:12]}, at HEAD "
          f"{ident.get('head_commit', '?')} {ident.get('head_sha256', '?')[:12]}")
    if not live:
        print("RESULT: FAIL -- proxy not reachable; nothing tested.")
        return 1
    if live != ident["disk_sha256"]:
        print("  NOTE: the proxy is NOT running the code on disk. Expected for a scratch proxy on "
              "old code; for production it means a restart is pending or the deploy did not load.")

    results: list[dict] = []
    for provider in [p.strip() for p in args.providers.split(",") if p.strip()]:
        names = args.scenarios or scenario_names(provider)
        names = [n for n in names if provider == "anthropic" or n not in ANTHROPIC_ONLY]
        streak = 0
        for i, name in enumerate(names):
            if streak >= args.max_provider_errors:
                for rest in names[i:]:
                    results.append({"scenario": rest, "provider": provider, "status": "SKIPPED",
                                    "sessions": [], "bad_pairs": [], "requests": []})
                    print(f"[{provider}] {rest:20s} SKIPPED (route kept returning provider errors)")
                break
            res = run_one(args, provider, name, out_dir)
            results.append(res)
            streak = streak + 1 if res["status"] == "PROVIDER-ERROR" else 0
            extra = ""
            if res["status"] == "FAIL":
                extra = f"  bad pairs: {res['bad_pairs'][:3]}" + (f"  error: {res['error']}" if res.get("error") else "")
            elif res["status"] == "PROVIDER-ERROR":
                extra = f"  upstream 5xx: {res['provider_errors']}"
            print(f"[{provider}] {name:20s} {res['status']:14s} {res['seconds']:4d}s{extra}", flush=True)
            (out_dir / "results.json").write_text(json.dumps(results, indent=1))

    (out_dir / "results.json").write_text(json.dumps(results, indent=1))
    # session ids of this run, so fleet tallies can exclude harness traffic
    (out_dir / "harness_sessions.txt").write_text(
        "\n".join(s for r in results for s in r.get("sessions", [])) + "\n")

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nSUMMARY " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"   (files: {out_dir})")
    fails = [r for r in results if r["status"] == "FAIL"]
    overall = ("FAIL" if fails else "INCONCLUSIVE"
               if any(r["status"] in ("PROVIDER-ERROR", "SKIPPED") for r in results) else "PASS")
    _write_gate_report(out_dir, args=args, ident=ident, live=live, results=results, overall=overall)
    if fails:
        print("RESULT: FAIL -- cache-prefix regression. For each bad pair run:\n"
              f"  .venv/bin/python scripts/prefix_diff.py --log-dir {args.log_dir} --prev-any <request id>\n"
              "  (or prefix_diff.py <earlier req id> <later req id>) and read the first divergence.")
        return 1
    if any(r["status"] in ("PROVIDER-ERROR", "SKIPPED") for r in results):
        print("RESULT: INCONCLUSIVE -- no cache FAIL, but some scenarios hit provider errors or were "
              "skipped. Rerun that route later; do not call it a PASS.")
        return 2
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
