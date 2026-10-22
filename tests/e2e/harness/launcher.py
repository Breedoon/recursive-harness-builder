"""obs-test-side launcher for the SSH-controlled, container-isolated E2E run."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets as random_secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[3]
E2E_ROOT = Path("/workspace/runtime/e2e")
PYTHON = "/workspace/obs/.venv/bin/python"
BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}\Z")


class HarnessError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_dir() -> Path:
    return Path(os.environ.get("OBS_E2E_REGISTRY_DIR") or E2E_ROOT / "registry").resolve()


def _atomic_json(path: Path, data) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _git(worktree: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(worktree), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_identity(code: str, run_dir: Path) -> tuple[Path, dict]:
    requested = Path(code).expanduser()
    if requested.is_dir():
        worktree = requested.resolve()
        head = _git(worktree, "rev-parse", "HEAD").decode().strip()
        status = _git(worktree, "status", "--porcelain=v1", "-z")
        diff = _git(worktree, "diff", "HEAD", "--binary")
        untracked = sorted(name for name in _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name)
        digest = hashlib.sha256(diff)
        untracked_rows = []
        for raw in untracked:
            path = worktree / os.fsdecode(raw)
            if path.is_symlink():
                sha = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
            elif path.is_file():
                sha = _sha_file(path)
            else:
                continue
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
            digest.update(bytes.fromhex(sha))
            untracked_rows.append({"path": os.fsdecode(raw), "sha256": sha})
        identity = {"requested": str(worktree), "worktree": str(worktree), "commit": head,
                    "dirty": bool(status), "dirty_hash": digest.hexdigest(),
                    "status_porcelain": os.fsdecode(status).split("\0"), "untracked": untracked_rows}
        if (worktree / ".env").exists() or (worktree / ".env").is_symlink():
            code_dir = run_dir / "code"
            code_dir.mkdir()
            # INVARIANT: a checkout's .env never becomes a container-readable mount.
            # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Isolation
            for raw in _git(worktree, "ls-files", "-z").split(b"\0"):
                if not raw:
                    continue
                relative = Path(os.fsdecode(raw))
                if relative.name == ".env":
                    continue
                source, target = worktree / relative, code_dir / relative
                if not source.exists() and not source.is_symlink():
                    continue  # A tracked deletion is part of the working-tree state.
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_symlink():
                    if source.resolve().name == ".env":
                        continue
                    target.symlink_to(os.readlink(source))
                elif source.is_file():
                    shutil.copy2(source, target)
            identity.update(snapshot="tracked-working-tree", mounted_path=str(code_dir))
            return code_dir, identity
        identity["mounted_path"] = str(worktree)
        return worktree, identity
    commit = _git(HARNESS_ROOT, "rev-parse", "--verify", f"{code}^{{commit}}").decode().strip()
    code_dir = run_dir / "code"
    code_dir.mkdir()
    archive = _git(HARNESS_ROOT, "archive", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        members = [member for member in files.getmembers() if Path(member.name).name != ".env"]
        files.extractall(code_dir, members=members, filter="data")
    return code_dir, {"requested": code, "commit": commit, "dirty": False,
                      "dirty_hash": None, "snapshot": "git-archive", "mounted_path": str(code_dir)}


def _common_env() -> dict[str, str]:
    return {"PATH": BASE_PATH, "HOME": "/home/agent", "LANG": "C.UTF-8", "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(HARNESS_ROOT)}


def _mounts(code_dir: Path, run_dir: Path, cli_path: Path | None):
    from .docker import Mount
    paths = [code_dir, HARNESS_ROOT, Path("/workspace/obs/.venv"), Path("/workspace/runtime/bin"),
             Path("/workspace/runtime/reference/claude-code-src"),
             Path("/workspace/runtime/preserved/cache-proxy-request-log-freeze-20261020T200401Z"),
             E2E_ROOT / "fixtures"]
    if cli_path is not None and not any(cli_path.is_relative_to(path) for path in paths):
        paths.append(cli_path)
    mounts = [Mount(run_dir, str(run_dir))]
    seen = {str(run_dir)}
    for path in paths:
        if str(path) not in seen:
            mounts.append(Mount(path, str(path), ro=True))
            seen.add(str(path))
    return mounts


def _runner_env(secret_env: dict[str, str], userbot_id: int, run_id: str, *, prod_send: bool = False) -> dict[str, str]:
    from .secrets import resolve
    env = _common_env()
    for name in ("OBS_TEST_TELEGRAM_SESSION", "OBS_TEST_TELEGRAM_API_ID", "OBS_TEST_TELEGRAM_API_HASH"):
        env[name] = resolve(f"env:{name}", secret_env)
    if prod_send:
        from .profiles import load_profile
        env["OBS_E2E_PROD_SEND_TOKEN"] = resolve(load_profile("prod-send").bot_token_ref, secret_env)
    env.update(OBS_E2E_RUN_ID=run_id, OBS_E2E_USERBOT_ID=str(userbot_id),
               OBS_E2E_REGISTRY_DIR=str(_registry_dir()), OBS_E2E_LOCK_DIR=str(E2E_ROOT / "locks"))
    return env


def _userbot_id(secret_env: dict[str, str]) -> int:
    # The canonical holder check records the test principal; no second Telethon client is created here.
    raw = secret_env.get("OBS_TEST_TELEGRAM_PRINCIPAL_USER_ID") or secret_env.get("OBS_TEST_TELEGRAM_ALLOWED_USERS", "")
    value = next((value.strip() for value in raw.split(",") if value.strip()), None)
    if value is None:
        raise HarnessError("OBS_TEST_TELEGRAM_ALLOWED_USERS or OBS_TEST_TELEGRAM_PRINCIPAL_USER_ID is required")
    return int(value)


def _stream_runner(docker, name: str, run_dir: Path) -> int:
    process = docker.logs(name, follow=True)
    if isinstance(process, str):
        if process:
            print(process, end="", flush=True)
    else:
        try:
            assert process.stdout is not None
            with (run_dir / "runner" / "orchestrator.log").open("a", encoding="utf-8") as stream:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    stream.write(line)
                    stream.flush()
            if process.wait() != 0:
                raise HarnessError("runner log stream failed")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
    # logs --follow ends at the container's exit. Inspect the actual exit, not SSH's status.
    state = docker.inspect(name).get("State", {})
    if state.get("Running"):
        raise HarnessError("runner log stream ended while the runner was still running")
    return int(state.get("ExitCode", 2))


def _run_once(args) -> int:
    from . import credcheck, creds, docker, profiles, provision
    from .results import compute_timing, write_summary
    from .secrets import bot_id_of, fingerprint, production_bot_tokens, read_env_file, resolve
    import httpx

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + random_secrets.token_hex(3)
    if not RUN_ID.fullmatch(run_id):
        raise HarnessError("run id must be YYYYmmddTHHMMSSZ- followed by six lowercase hexadecimal digits")
    run_dir, private = E2E_ROOT / "runs" / run_id, E2E_ROOT / "private" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    owns_private = False
    manifest = {"run_id": run_id, "started_at": _now(), "harness_root": str(HARNESS_ROOT),
                "suite": args.suite, "tests": str(args.tests), "keyword": args.keyword,
                "requested_lanes": args.lanes, "repeat": args.repeat,
                "keep_lanes": args.keep_lanes, "prod_send": getattr(args, "prod_send", False),
                "caller_canary": "OBS_PROFILE_FORCED_BY_CALLER" if "OBS_PROFILE_FORCED_BY_CALLER" in os.environ else None,
                "userbot_lock_timeout": args.userbot_lock_timeout,
                "lane_ready_timeout": args.lane_ready_timeout, "lanes": [], "lanes_excluded": [], "bots_excluded": [],
                "forums_path": str(_registry_dir() / "forums.json")}
    _atomic_json(run_dir / "manifest.json", manifest)
    resync, sentinel, result = None, None, 2
    isolation, isolation_before = None, None
    previous_handlers = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        private.mkdir(mode=0o700, parents=True, exist_ok=False)
        owns_private = True
        private.chmod(0o700)
        for directory in ("runner", "isolation", "tests", "sockets", "lanes"):
            (run_dir / directory).mkdir()
        (E2E_ROOT / "fixtures").mkdir(parents=True, exist_ok=True)
        (E2E_ROOT / "locks").mkdir(parents=True, exist_ok=True)
        _registry_dir().mkdir(parents=True, exist_ok=True)
        code_dir, identity = code_identity(args.code, run_dir)
        manifest["code"] = identity
        cli_path = args.cli_path.expanduser().resolve() if args.cli_path else None
        manifest["cli_path"] = str(cli_path) if cli_path else None
        if cli_path:
            if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
                raise HarnessError("--cli-path must be an executable file")
            manifest["cli"] = {"path": str(cli_path), "sha256": _sha_file(cli_path)}
        secret_env = read_env_file()
        userbot_id = _userbot_id(secret_env)
        manifest["userbot_id"] = userbot_id
        production = production_bot_tokens(secret_env)
        lane_profiles = profiles.lane_profiles(args)
        if len(lane_profiles) != args.lanes:
            raise HarnessError("profile resolution did not return one profile per requested lane")
        pools = [profile.bot_pool_tokens(secret_env) for profile in lane_profiles]
        candidates = list(dict.fromkeys(token for pool in pools for token in pool))
        reserved_ids = set()
        for profile in lane_profiles:
            for excluded in profile.excluded_bots(secret_env):
                if excluded["bot_id"] not in reserved_ids:
                    manifest["bots_excluded"].append(excluded)
                    reserved_ids.add(excluded["bot_id"])
        manifest["candidate_bot_count"] = len(candidates)
        _atomic_json(run_dir / "manifest.json", manifest)
        # INVARIANT: prod-send is runner-only; production bots must never be lane pollers.
        # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Profiles#Secrets
        if any(token in production for token in candidates):
            raise HarnessError("a candidate lane bot resolves to a production token")
        if not candidates:
            raise HarnessError("no unreserved candidate lane bots remain")
        prepared = {}
        manifest["bot_probes_started_at"] = _now()
        print(f"obs-e2e {run_id}: checking {len(candidates)} candidate bots concurrently; "
              f"reserved={len(manifest['bots_excluded'])}", flush=True)
        with httpx.Client() as client, ThreadPoolExecutor(max_workers=1) as probes:
            checking = probes.submit(credcheck.check_bots, candidates, holder_probe=True, client=client)
            manifest["provisioning_started_at"] = _now()
            for index, profile in enumerate(lane_profiles, start=1):
                lane_dir = provision.make_lane_dirs(run_dir, f"lane{index}")
                provision.build_fixture_vault(lane_dir)
                provision.write_lane_bin(lane_dir, code_dir)
                prepared[index] = (lane_dir, creds.write_lane_credentials(lane_dir / "home"))
            image = docker.ensure_image(HARNESS_ROOT / "deploy" / "obs-e2e")
            manifest["image"] = image
            manifest["provisioning_finished_at"] = _now()
            checked_rows = checking.result()
        manifest["bot_probes_finished_at"] = _now()
        manifest["bot_probes"] = checked_rows
        checks = dict(zip(candidates, checked_rows))
        for token, checked in checks.items():
            if not checked.get("valid") or not checked.get("getUpdates_ok"):
                reason = "held:probe-conflict" if checked.get("held_by_other_poller") else "invalid:bot-check"
                manifest["bots_excluded"].append({"bot_id": bot_id_of(token), "reason": reason})
        resolved, used = [], set()
        for index, (profile, pool) in enumerate(zip(lane_profiles, pools), start=1):
            token = next((token for token in pool if token not in used and checks[token].get("valid")
                          and checks[token].get("getUpdates_ok")), None)
            if token is None:
                manifest["lanes_excluded"].append({"lane": f"lane{index}", "profile": profile.name,
                                                   "reason": "pool:insufficient-free-bots"})
                continue
            used.add(token)
            resolved.append((index, profile, token, checks[token]))
        _atomic_json(run_dir / "manifest.json", manifest)
        if not resolved:
            raise HarnessError("no valid unheld lane bots remain")
        homes = []
        for index, profile, token, checked in resolved:
            name = f"lane{index}"
            lane_dir, credentials = prepared[index]
            env = provision.lane_env(profile, run_dir=run_dir, lane=name, lane_index=index - 1,
                                     code_dir=code_dir, harness_root=HARNESS_ROOT,
                                     bot_token=token, userbot_id=userbot_id)
            env["PYTHONPATH"] = str(HARNESS_ROOT)
            provision.write_env_file(private / f"{name}.env", env)
            row = {"run_id": run_id, "lane": name, "index": index - 1, "profile": profile.name,
                   "credential_class": profile.credential_class, "route": profile.route, "model": profile.model,
                   "code_dir": str(code_dir), "harness_root": str(HARNESS_ROOT),
                   "cli_path": str(cli_path) if cli_path else None, "lane_dir": str(lane_dir),
                   "bot_id": checked["bot_id"], "bot_username": checked.get("username"),
                   "proxy_port": 29925, "needs_cliproxy": profile.needs_cliproxy,
                   "env_names": sorted(env), "container": f"obs-e2e-{run_id}-{name}", "created_at": _now()}
            provision.write_lane_json(lane_dir, row)
            manifest["lanes"].append({**row, "bot_token_fingerprint": fingerprint(token), "credentials": credentials})
            homes.append(lane_dir / "home")
        resync = creds.ResyncThread(homes, interval=600)
        resync.start()
        provision.write_env_file(private / "runner.env", _runner_env(secret_env, userbot_id, run_id,
                                                                   prod_send=getattr(args, "prod_send", False)))
        provision.write_env_file(private / "relay.env", _common_env())
        manifest["flags"] = {"cgroup_parent": "obs-e2e.slice", "memory": "14g", "memory_swap": "14g",
                             "cpus": 8, "pids_limit": 8192, "user": "1000:1000", "init": True}
        if {"isolation", "all"}.intersection(part.strip() for part in args.suite.split(",")):
            from . import isolation
            isolation_before = isolation.before_run(run_dir, with_sentinel=True)
            sentinel = isolation_before["host_sentinel"]["pid"]
            manifest["host_sentinel_pid"] = sentinel
        _atomic_json(run_dir / "manifest.json", manifest)
        common = _mounts(code_dir, run_dir, cli_path)
        if any(row["needs_cliproxy"] for row in manifest["lanes"]):
            docker.run_container(f"obs-e2e-{run_id}-relay", image, role="relay", run_id=run_id,
                                 env_file=private / "relay.env", mounts=common, network="container:obs-test",
                                 add_host_gateway=False, workdir=str(HARNESS_ROOT),
                                 command=[PYTHON, "-m", "tests.e2e.harness.relay", "unix-to-tcp", "--unix",
                                          str(run_dir / "sockets" / "cliproxy.sock"), "--target", "127.0.0.1:8317"])
        for row in manifest["lanes"]:
            lane_dir = run_dir / "lanes" / row["lane"]
            mounts = [*common, docker.Mount(lane_dir / "state", "/workspace/runtime/state"),
                      docker.Mount(lane_dir / "logs", "/workspace/runtime/logs"),
                      docker.Mount(lane_dir / "home", "/home/agent"),
                      docker.Mount(lane_dir / "vault", "/workspace/runtime/git/obs-vault-active")]
            docker.run_container(row["container"], image, role="lane", run_id=run_id, lane=row["lane"],
                                 env_file=private / f"{row['lane']}.env", mounts=mounts, workdir=str(HARNESS_ROOT),
                                 command=[PYTHON, "-m", "tests.e2e.harness.lanectl", "serve", "--lane-dir", str(lane_dir)])
        runner_name = f"obs-e2e-{run_id}-runner"
        # INVARIANT: the registry's atomic rename must not expose other runs' private envs.
        # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Isolation
        runner_mounts = [docker.Mount(run_dir, str(run_dir)),
                         docker.Mount(_registry_dir(), str(_registry_dir())),
                         docker.Mount(E2E_ROOT / "locks", str(E2E_ROOT / "locks")),
                         docker.Mount(HARNESS_ROOT, str(HARNESS_ROOT), ro=True),
                         docker.Mount(Path("/workspace/obs/.venv"), "/workspace/obs/.venv", ro=True)]
        docker.run_container(runner_name, image, role="runner", run_id=run_id,
                             env_file=private / "runner.env", mounts=runner_mounts, add_host_gateway=False,
                             workdir=str(HARNESS_ROOT),
                             command=[PYTHON, "-m", "tests.e2e.harness.orchestrator", "--run-dir", str(run_dir),
                                      "--userbot-lock-timeout", str(args.userbot_lock_timeout),
                                      "--lane-ready-timeout", str(args.lane_ready_timeout)])
        result = _stream_runner(docker, runner_name, run_dir)
        if result not in (0, 1, 2, 3):
            result = 2
        if not (run_dir / "summary.json").exists():
            raise HarnessError("runner exited without summary.json")
    except KeyboardInterrupt:
        result = 2
        _atomic_json(run_dir / "manifest.json", manifest)
        write_summary(run_dir, error="launcher interrupted", exit_code=result)
    except Exception as exc:
        result = 2
        message = str(exc) if isinstance(exc, HarnessError) else exc.__class__.__name__
        _atomic_json(run_dir / "manifest.json", manifest)
        write_summary(run_dir, error=message, exit_code=result)
        print(f"obs-e2e: {message}", file=sys.stderr, flush=True)
    finally:
        # Ignore repeated interrupts while releasing the resources owned by this exact run.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        teardown_errors = []
        if not args.keep_lanes:
            try:
                docker.rm_run(run_id)
            except Exception as exc:
                teardown_errors.append(f"containers: {exc.__class__.__name__}")
        if resync is not None:
            resync.stop()
            resync.join(timeout=30)
            if resync.is_alive():
                teardown_errors.append("credential re-sync thread did not stop")
        try:
            if owns_private:
                shutil.rmtree(private)
        except FileNotFoundError:
            pass
        except OSError as exc:
            teardown_errors.append(f"private files: {exc.__class__.__name__}")
        current = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if isolation_before is not None:
            try:
                after = isolation.after_run(run_dir, isolation_before,
                                             prod_send_exercised_required=bool(current.get("prod_send")))
                proof = isolation.summarize(run_dir, after)
                current["isolation_all_pass"] = proof["all_pass"]
                if result == 0 and "isolation" in current.get("selected_suites", []) and not proof["all_pass"]:
                    result = 1
                    write_summary(run_dir, error="isolation probes failed; see isolation/summary.json", exit_code=result)
            except Exception as exc:
                teardown_errors.append(f"isolation checks: {exc.__class__.__name__}")
            finally:
                if sentinel is not None:
                    try:
                        isolation.stop_host_sentinel(sentinel)
                    except Exception as exc:
                        teardown_errors.append(f"sentinel: {exc.__class__.__name__}")
        # Preserve the runner's measured finish; setup failures still receive an end timestamp.
        current.setdefault("finished_at", _now())
        current["teardown_errors"] = teardown_errors
        if teardown_errors:
            result = 2
            write_summary(run_dir, error="teardown incomplete: " + "; ".join(teardown_errors), exit_code=result)
        current["launcher_exit_code"] = result
        _atomic_json(run_dir / "manifest.json", current)
        if not (run_dir / "timing.json").exists():
            compute_timing(run_dir)
        if teardown_errors:
            print("obs-e2e teardown incomplete: " + "; ".join(teardown_errors), file=sys.stderr, flush=True)
        print(f"obs-e2e run directory: {run_dir}", flush=True)
        if (run_dir / "summary.json").exists():
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            print(summary["summary_line"], flush=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return result


def run(args) -> int:
    return _run_once(args)


def install_host() -> int:
    from . import docker
    context = HARNESS_ROOT / "deploy" / "obs-e2e"
    # Use exactly ensure_image's content-address calculation; no Docker operation on --help.
    tag = docker._context_digest(context)[:12]
    command = ["sudo", "-n", "bash", docker.to_host_path(context / "install-host.sh"),
               docker.to_host_path(context), tag]
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "ubuntu-host", shlex.join(command)], check=False)
    return 0 if result.returncode == 0 else 2
