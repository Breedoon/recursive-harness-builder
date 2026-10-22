"""Command-line entry point for the container-isolated OBS E2E harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[3]


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obs-e2e", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="launch isolated lanes and run selected E2E suites")
    run.add_argument("--code", required=True, help="Git worktree directory or commit")
    run.add_argument("--cli-path", type=Path, help="replacement CLI executable to test")
    run.add_argument("--lanes", type=_positive, default=5)
    run.add_argument("--suite", required=True, help="comma-separated suite names or tags (all selects every suite)")
    profiles = run.add_mutually_exclusive_group(required=True)
    profiles.add_argument("--profile")
    profiles.add_argument("--lane-profiles", help="comma-separated profile per requested lane")
    run.add_argument("--repeat", type=_positive, default=1)
    run.add_argument("--keep-lanes", action="store_true")
    run.add_argument("--prod-send", action="store_true", help="enable runner-only update-free production-bot calls")
    run.add_argument("--userbot-lock-timeout", type=_nonnegative, default=1800)
    run.add_argument("--lane-ready-timeout", type=_nonnegative, default=180)
    run.add_argument("--run-id", help="YYYYmmddTHHMMSSZ- followed by six hexadecimal digits")
    run.add_argument("-k", dest="keyword", metavar="EXPR")
    run.add_argument("--tests", type=Path, default=Path("tests/e2e"))
    lint = commands.add_parser("lint", help="reject fixed sleeps in E2E tests")
    lint.add_argument("paths", nargs="*")
    lint.add_argument("--json", action="store_true")
    from . import credcheck
    credcheck.build_parser(commands.add_parser("credcheck", help="check test credential validity"))
    down = commands.add_parser("down", help="remove only the containers labelled with a run id")
    down.add_argument("run_id")
    commands.add_parser("ps", help="list harness containers")
    commands.add_parser("install-host", help="install the host aggregate-resource slice and build image")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            from . import launcher
            return launcher.run(args)
        if args.command == "lint":
            from . import lint
            return lint.main(argv[1:])
        if args.command == "credcheck":
            from . import credcheck
            return credcheck.main(argv[1:])
        from . import docker
        if args.command == "down":
            docker.rm_run(args.run_id)
            return 0
        if args.command == "ps":
            result = docker.ps()
            if result is not None:
                print(json.dumps(result, indent=2))
            return 0
        if args.command == "install-host":
            from .launcher import install_host
            return install_host()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # External command and HTTP error strings may include secret-bearing argv.
        print(f"obs-e2e: harness/setup error ({exc.__class__.__name__})", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
