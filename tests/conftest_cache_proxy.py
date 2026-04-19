"""Shared test infrastructure for cache-normalizing proxy tests.

Provides:
- Proxy subprocess lifecycle fixtures (start/stop, health check, random port)
- JSONL analysis helpers with adjusted cache metric (baseline subtraction)
- SDK session management helpers (run turns, extract usage)
- Environment setup (CLAUDECODE unsetting, ANTHROPIC_BASE_URL, API keys)
- Temporary project directory fixtures

The adjusted cache metric follows the specification in
.claude/skills/jsonl-analysis/SKILL.md exactly: subtract a baseline
(globally-cached system prompt ~21-25K tokens), classify as HIT/MISS/EDGE.

Run these tests with the project's own .venv (NOT the shared obs-venv)::

    ~/Documents/obs/.venv/bin/python -m pytest -m live tests/test_cache_proxy*.py

The project .venv has claude_agent_sdk, pytest-asyncio, and all required deps.
The shared obs-venv (Python 3.8) does NOT have claude_agent_sdk.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Environment guard: fail fast if running with wrong Python.
# claude_agent_sdk is only installed in ~/Documents/obs/.venv/ (Python 3.12).
# ---------------------------------------------------------------------------
try:
    import claude_agent_sdk as _sdk_check  # noqa: F401
except ImportError:
    pytest.exit(
        "ERROR: claude_agent_sdk not found. These tests require the project's "
        "own .venv, NOT the shared obs-venv.\n"
        "Run with: ~/Documents/obs/.venv/bin/python -m pytest -m live tests/test_cache_proxy*.py",
        returncode=1,
    )

# ---------------------------------------------------------------------------
# Load .env for API keys (ANTHROPIC_API_KEY etc.) — needed in CI and clean
# environments where the shell doesn't have them set.
# Follows the same manual parsing pattern as tests/conftest.py (no python-dotenv dep).
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _key and _val and _key not in os.environ:
                os.environ[_key] = _val

# ---------------------------------------------------------------------------
# Unset CLAUDECODE before any SDK import — SDK refuses to run inside CC.
# Must happen at module level, before pytest collects tests that import SDK.
# ---------------------------------------------------------------------------
os.environ.pop("CLAUDECODE", None)
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION_PROXY = _PROJECT_ROOT / "src" / "cache_proxy.py"
_SPIKE_PROXY = _PROJECT_ROOT / "spikes" / "cache_normalizing_proxy.py"
# Also check the main tree in case worktree doesn't have the spike
_MAIN_SPIKE_PROXY = Path.home() / "Documents" / "obs" / "spikes" / "cache_normalizing_proxy.py"
_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Proxy port range: pick something unlikely to collide
_PROXY_PORT_MIN = 18200
_PROXY_PORT_MAX = 18999

# ~10K tokens per chunk. Used to inflate context for measurable cache behavior.
BULK_TEXT = (
    "This is synthetic padding text for cache normalization proxy testing. "
    "It has no semantic content. Its purpose is to inflate the prompt context "
    "to a size where cache behavior becomes measurable and meaningful. "
) * 500  # ~39K chars ≈ 10K tokens

# Model to use for all proxy tests (cheap, fast).
TEST_MODEL = "claude-haiku-4-5"


# ── Port allocation ───────────────────────────────────────────────────────


def _free_port() -> int:
    """Get a free port using OS allocation, biased toward our range."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    """Wait until a TCP port is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


# ── Proxy resolution ─────────────────────────────────────────────────────


def _resolve_proxy_script() -> Path:
    """Find the proxy script: production first, then spike, then main tree spike."""
    if _PRODUCTION_PROXY.exists():
        return _PRODUCTION_PROXY
    if _SPIKE_PROXY.exists():
        return _SPIKE_PROXY
    if _MAIN_SPIKE_PROXY.exists():
        return _MAIN_SPIKE_PROXY
    raise FileNotFoundError(
        f"Cache proxy script not found at any of:\n"
        f"  {_PRODUCTION_PROXY}\n"
        f"  {_SPIKE_PROXY}\n"
        f"  {_MAIN_SPIKE_PROXY}\n"
    )


# ── JSONL analysis helpers ────────────────────────────────────────────────


def find_session_jsonl(session_id: str) -> Path | None:
    """Find the JSONL file for a given session ID across all project dirs."""
    if not _PROJECTS_DIR.exists():
        return None
    for pd in _PROJECTS_DIR.iterdir():
        if not pd.is_dir():
            continue
        cand = pd / f"{session_id}.jsonl"
        if cand.exists():
            return cand
    return None


def extract_usage(session_id: str) -> list[dict]:
    """Extract deduplicated usage rows from assistant entries in a session.

    Deduplicates by message.id (multiple assistant entries per API call share
    the same message.id for thinking/text/tool_use splits).

    Returns list of dicts with keys: uuid, cr, cc, ip, tot, model, session_id.
    """
    path = find_session_jsonl(session_id)
    if not path:
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message", {})
            mid = msg.get("id")
            u = msg.get("usage")
            if not (mid and u) or mid in seen:
                continue
            seen.add(mid)
            cr = u.get("cache_read_input_tokens", 0)
            cc = u.get("cache_creation_input_tokens", 0)
            ip = u.get("input_tokens", 0)
            tot = cr + cc + ip
            if tot == 0:
                continue
            rows.append({
                "uuid": e.get("uuid"),
                "session_id": e.get("sessionId", ""),
                "cr": cr, "cc": cc, "ip": ip, "tot": tot,
                "model": msg.get("model"),
            })
    return rows


def extract_fork_first_turn(fork_session_id: str) -> dict | None:
    """Extract the fork's first turn using sessionId-based detection.

    The fork's first turn is the first assistant entry where entry.sessionId
    matches the fork's own session ID (= JSONL filename stem).

    WARNING: Do NOT use UUID comparison for this — it falsely reports cache hits
    from the parent's warm cache. See .claude/skills/jsonl-analysis/SKILL.md.
    """
    path = find_session_jsonl(fork_session_id)
    if not path:
        return None
    seen: set[str] = set()
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "assistant":
                continue
            if e.get("sessionId") != fork_session_id:
                continue
            msg = e.get("message", {})
            mid = msg.get("id")
            u = msg.get("usage")
            if not (mid and u) or mid in seen:
                continue
            seen.add(mid)
            cr = u.get("cache_read_input_tokens", 0)
            cc = u.get("cache_creation_input_tokens", 0)
            ip = u.get("input_tokens", 0)
            tot = cr + cc + ip
            if tot > 0:
                return {"cr": cr, "cc": cc, "ip": ip, "tot": tot}
    return None


def compute_baseline(cache_reads: list[int]) -> int:
    """Compute the globally-cached system prompt baseline.

    baseline = min(cr for cr in cache_reads[1:] if cr > 0)
    Skip turn 0 (may be cold start), exclude zeros (daemon restart artifacts).
    If no qualifying turns, baseline = 0.
    """
    qualifying = [cr for cr in cache_reads[1:] if cr > 0]
    return min(qualifying) if qualifying else 0


def classify_cache_hit(
    cache_read: int,
    prev_total_input: int,
    baseline: int,
) -> str:
    """Classify a turn's cache behavior using the adjusted metric.

    Returns 'HIT', 'MISS', or 'EDGE'.

    The adjusted metric subtracts the globally-cached system prompt baseline
    to isolate conversation-level cache behavior:
      adjusted_read = cache_read - baseline
      adjusted_prefix = prev_total_input - baseline
      rate = adjusted_read / adjusted_prefix

    Classification:
      HIT:  rate >= 0.95, OR (abs_delta < 2000 AND rate >= 0.50)
      MISS: rate < 0.05 (including negative)
      EDGE: everything else (practically MISS for dashboards)

    Spec: .claude/skills/jsonl-analysis/SKILL.md § Cache Hit Metric
    """
    adjusted_read = cache_read - baseline
    adjusted_prefix = prev_total_input - baseline
    rate = adjusted_read / adjusted_prefix if adjusted_prefix > 0 else 0
    abs_delta = abs(adjusted_prefix - adjusted_read)

    if rate >= 0.95 or (abs_delta < 2000 and rate >= 0.50):
        return "HIT"
    elif rate < 0.05:
        return "MISS"
    else:
        return "EDGE"


def assert_cache_hit(
    cache_read: int,
    prev_total_input: int,
    baseline: int,
    label: str = "",
) -> None:
    """Assert that a turn is classified as HIT. Provides diagnostic info on failure."""
    classification = classify_cache_hit(cache_read, prev_total_input, baseline)
    adjusted_read = cache_read - baseline
    adjusted_prefix = prev_total_input - baseline
    rate = adjusted_read / adjusted_prefix if adjusted_prefix > 0 else 0

    assert classification == "HIT", (
        f"Expected HIT but got {classification}"
        f"{f' ({label})' if label else ''}. "
        f"cache_read={cache_read:,}, prev_total={prev_total_input:,}, "
        f"baseline={baseline:,}, adjusted_read={adjusted_read:,}, "
        f"adjusted_prefix={adjusted_prefix:,}, rate={rate:.2%}"
    )


# ── SDK session helpers ───────────────────────────────────────────────────


async def run_turn(client: ClaudeSDKClient, prompt: str) -> tuple[str | None, dict]:
    """Send a prompt and collect the session ID + last usage stats.

    Returns (session_id, usage_dict). usage_dict has keys:
    cache_read_input_tokens, cache_creation_input_tokens, input_tokens.

    NOTE: The SDK only returns real usage on the first turn within a client
    connection. Subsequent turns return zero'd usage. For multi-turn tests,
    use `get_proxy_usage_for_turns()` to read actual usage from the proxy log.
    """
    session_id = None
    last_usage: dict = {}
    await client.query(prompt)
    async for msg in client.receive_messages():
        if hasattr(msg, "session_id") and msg.session_id:
            session_id = msg.session_id
        if hasattr(msg, "usage") and isinstance(msg.usage, dict):
            u = msg.usage
            if (u.get("cache_read_input_tokens", 0) +
                    u.get("cache_creation_input_tokens", 0) +
                    u.get("input_tokens", 0) > 0):
                last_usage = u
        t = getattr(msg, "subtype", None)
        if t in ("success", "error_max_turns"):
            break
    return session_id, last_usage


def get_proxy_usage_for_turns(
    start_offset: int = 0,
    log_path: str | Path = "/tmp/cache-proxy-log/usage.jsonl",
) -> list[dict]:
    """Read proxy usage log entries starting from offset.

    Returns list of dicts with keys matching extract_usage() format:
    cr, cc, ip, tot.

    The proxy log records one entry per API request, in chronological order.
    Use start_offset to skip entries from previous turns/tests.

    This is the reliable way to get per-turn usage — the SDK only returns real
    usage on the first turn within a client connection (turns 2+ return zeros).
    """
    raw = read_proxy_usage_log(log_path)
    rows = []
    for entry in raw[start_offset:]:
        rows.append({
            "cr": entry.get("cache_read", 0),
            "cc": entry.get("cache_creation", 0),
            "ip": entry.get("input_tokens", 0),
            "tot": entry.get("total", 0),
            "cache_read_input_tokens": entry.get("cache_read", 0),
            "cache_creation_input_tokens": entry.get("cache_creation", 0),
            "input_tokens": entry.get("input_tokens", 0),
        })
    return rows


def proxy_log_length(
    log_path: str | Path = "/tmp/cache-proxy-log/usage.jsonl",
) -> int:
    """Return current number of entries in the proxy usage log."""
    return len(read_proxy_usage_log(log_path))


def make_sdk_options(
    project_dir: str | Path,
    proxy_port: int,
    *,
    resume: str | None = None,
    fork_session: bool = False,
    model: str = TEST_MODEL,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions configured for proxy testing.

    Sets up env to route through the cache proxy, disable background tasks,
    disable git instructions (cache optimization), and unset CLAUDECODE.
    """
    env = {
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        "CLAUDECODE": "",
        "CLAUDE_CODE_ENTRYPOINT": "",
        "ANTHROPIC_BASE_URL": f"http://localhost:{proxy_port}",
    }

    kwargs: dict = {
        "model": model,
        "cwd": str(project_dir),
        "permission_mode": "bypassPermissions",
        "env": env,
    }
    if resume:
        kwargs["resume"] = resume
    if fork_session:
        kwargs["fork_session"] = True

    return ClaudeAgentOptions(**kwargs)


def fmt_usage(u: dict) -> str:
    """Format usage dict for human-readable output."""
    cr = u.get("cache_read_input_tokens", u.get("cr", 0))
    cc = u.get("cache_creation_input_tokens", u.get("cc", 0))
    ip = u.get("input_tokens", u.get("ip", 0))
    tot = cr + cc + ip
    rate = 100.0 * cr / tot if tot else 0
    return f"tot={tot:,}  cr={cr:,}  cc={cc:,}  ip={ip}  ({rate:.0f}% cached)"


# ── Proxy usage log helpers ───────────────────────────────────────────────


def read_proxy_usage_log(log_path: str | Path = "/tmp/cache-proxy-log/usage.jsonl") -> list[dict]:
    """Read the proxy's structured usage log."""
    path = Path(log_path)
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def read_proxy_bodies(
    body_dir: str | Path = "/tmp/cache-proxy-log/bodies/",
    req_num: int | None = None,
) -> tuple[dict | None, dict | None]:
    """Read pre/post normalization request bodies for a specific request.

    If req_num is None, returns the latest pair.
    Returns (pre_body, post_body) or (None, None) if not found.
    """
    bd = Path(body_dir)
    if not bd.exists():
        return None, None

    if req_num is not None:
        pre = bd / f"req{req_num:03d}_pre.json"
        post = bd / f"req{req_num:03d}_post.json"
    else:
        # Find the latest pair
        pre_files = sorted(bd.glob("req*_pre.json"))
        if not pre_files:
            return None, None
        pre = pre_files[-1]
        post = bd / pre.name.replace("_pre.json", "_post.json")

    pre_body = json.loads(pre.read_text()) if pre.exists() else None
    post_body = json.loads(post.read_text()) if post.exists() else None
    return pre_body, post_body


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def proxy_script() -> Path:
    """Resolve path to the cache proxy script."""
    return _resolve_proxy_script()


@pytest.fixture
def proxy_port() -> int:
    """Allocate a free port for the proxy."""
    return _free_port()


@pytest.fixture
def proxy(proxy_port: int, proxy_script: Path) -> Generator[int, None, None]:
    """Start the cache proxy subprocess and yield its port.

    Lifecycle:
    1. Start proxy on a random port
    2. Wait for TCP health check (max 10s)
    3. Yield the port
    4. Terminate proxy (SIGTERM, then SIGKILL after 5s)

    Usage log is cleared at start to avoid cross-test contamination.
    """
    # Clear previous proxy logs
    log_dir = Path("/tmp/cache-proxy-log")
    usage_log = log_dir / "usage.jsonl"
    body_dir = log_dir / "bodies"
    for f in [usage_log]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    # Clear body dir
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    proc = subprocess.Popen(
        [sys.executable, str(proxy_script), str(proxy_port)],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    # Health check: wait for TCP port to accept connections
    if not _wait_for_port("127.0.0.1", proxy_port, timeout=10.0):
        stderr_out = ""
        if proc.poll() is not None and proc.stderr:
            stderr_out = proc.stderr.read().decode(errors="replace")
        proc.kill()
        pytest.fail(
            f"Cache proxy failed to start on port {proxy_port} within 10s. "
            f"Script: {proxy_script}, stderr: {stderr_out}"
        )

    yield proxy_port

    # Teardown: capture proxy logs before stopping
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    # Read and log stderr for post-mortem diagnostics
    if proc.stderr:
        stderr_output = proc.stderr.read().decode(errors="replace").strip()
        if stderr_output:
            print(f"\n--- cache proxy stderr (port {proxy_port}) ---")
            print(stderr_output)
            print("--- end proxy stderr ---\n")


@pytest.fixture
def proxy_with_bodies(proxy_port: int, proxy_script: Path) -> Generator[int, None, None]:
    """Same as proxy fixture but with SAVE_BODIES=True for normalization inspection.

    Sets CACHE_PROXY_SAVE_BODIES=1 env var. The production proxy should check this;
    the spike proxy has SAVE_BODIES=True hardcoded so bodies are always saved there.
    """
    log_dir = Path("/tmp/cache-proxy-log")
    usage_log = log_dir / "usage.jsonl"
    body_dir = log_dir / "bodies"
    for f in [usage_log]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    if body_dir.exists():
        for f in body_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass

    env = os.environ.copy()
    env["CACHE_PROXY_SAVE_BODIES"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(proxy_script), str(proxy_port)],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )

    if not _wait_for_port("127.0.0.1", proxy_port, timeout=10.0):
        proc.kill()
        pytest.fail(f"Cache proxy (with bodies) failed to start on port {proxy_port}")

    yield proxy_port

    # Teardown: capture proxy logs before stopping
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    # Read and log stderr for post-mortem diagnostics
    if proc.stderr:
        stderr_output = proc.stderr.read().decode(errors="replace").strip()
        if stderr_output:
            print(f"\n--- cache proxy (with bodies) stderr (port {proxy_port}) ---")
            print(stderr_output)
            print("--- end proxy stderr ---\n")


@pytest.fixture
def test_project(tmp_path: Path) -> Path:
    """Create a minimal project directory for SDK sessions.

    Includes CLAUDE.md and a few files for read/edit operations.
    """
    project = tmp_path / "proxy-test-project"
    project.mkdir()

    (project / "CLAUDE.md").write_text(
        "# Test Project\n\n"
        "Minimal project for cache proxy testing. Answer concisely.\n"
    )

    # Files for triggering file operations (system-reminder injection)
    data_dir = project / "data"
    data_dir.mkdir()
    (data_dir / "sample.txt").write_text(
        "This is a sample data file for testing file operations.\n"
        "It contains multiple lines of text.\n"
        "Agents can read and modify this file.\n"
    )
    (data_dir / "config.json").write_text(
        json.dumps({"version": 1, "name": "test-config", "active": True}, indent=2)
    )

    # A Python file for tool-use testing
    (project / "hello.py").write_text(
        'def greet(name: str) -> str:\n'
        '    return f"Hello, {name}!"\n'
    )

    return project


@pytest.fixture
async def parent_session(proxy: int, test_project: Path):
    """Run a parent session with bulk text and return (session_id, usage_rows).

    Creates 3 turns with ~10K tokens each for measurable cache behavior.
    The session is fully disconnected when returned.

    This is an async fixture — pytest-asyncio (asyncio_mode="auto") handles it.
    """
    opts = make_sdk_options(test_project, proxy)
    client = ClaudeSDKClient(opts)
    await client.connect()
    parent_sid = None
    usage_rows = []
    try:
        prompts = [
            f"Reference document A:\n\n{BULK_TEXT}\n\nReply with exactly: READY",
            f"Reference document B:\n\n{BULK_TEXT}\n\nReply with exactly: ACK2",
            "Reply with exactly: ACK3",
        ]
        for prompt in prompts:
            sid, usage = await run_turn(client, prompt)
            if sid:
                parent_sid = sid
            usage_rows.append(usage)
    finally:
        await client.disconnect()

    if not parent_sid:
        pytest.fail("Failed to get parent session ID")
    return parent_sid, usage_rows
