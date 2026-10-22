"""Sequential named scenario list; no autonomous reruns or unlisted scenarios."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", choices=("RP", "AP"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--scenarios", required=True, help="Comma-separated ids; 6 expands the three source sites")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--control-source-dir")
    parser.add_argument("--rerun-reason")
    parser.add_argument("--s1-mode", choices=("error", "stop"), default="error")
    args = parser.parse_args()
    scenarios = []
    for value in args.scenarios.split(","):
        if value == "6":
            scenarios.extend(("6", site) for site in ("rounds-cleanup", "rounds-failure", "post-reboot"))
        else:
            parts = value.split(":", 1)
            scenarios.append((parts[0], parts[1] if len(parts) > 1 else None))
    for scenario, site in scenarios:
        command = [sys.executable, str(HERE / "run_scenario.py"), "--identity", args.identity,
                   "--sha", args.sha, "--scenario", scenario, "--ledger", args.ledger, "--s1-mode", args.s1_mode]
        if args.rerun_reason:
            command.extend(["--rerun-reason", args.rerun_reason])
        if site:
            command.extend(["--control-site", site])
        if args.control_source_dir:
            command.extend(["--control-source-dir", args.control_source_dir])
        result = subprocess.run(command, capture_output=True, text=True)
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="", flush=True)
        try:
            row = json.loads(result.stdout)
        except ValueError:
            row = {"scenario": scenario, "site": site, "stdout": result.stdout, "stderr": result.stderr}
        row["returncode"] = result.returncode
        with Path(args.index).open("a") as output:
            output.write(json.dumps(row) + "\n")
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
