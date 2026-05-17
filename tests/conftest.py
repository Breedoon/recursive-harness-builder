"""Shared test fixtures for OBS Agent."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Prevent real Claude Code CLI subprocess spawning in unit tests.
#
# Tests that mock ConversationRunner still hit SessionManager.get_client()
# which calls ClaudeSDKClient.connect() → launches a real CLI subprocess.
# These subprocesses fail (nested session detection) but their
# ThreadedChildWatcher threads accumulate across tests and eventually
# hang the test suite.  Patching get_client for affected tests prevents
# subprocess spawning entirely.
# Tests that explicitly test get_client behavior (e.g., TestClientLifecycle)
# should mark themselves with @pytest.mark.real_get_client to opt out.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_session_get_client(request):
    """Prevent SessionManager.get_client from spawning real CLI processes.

    Skipped for tests marked with ``@pytest.mark.real_get_client``.
    """
    if request.node.get_closest_marker("real_get_client"):
        yield
    else:
        with patch("obs_agent.session.SessionManager.get_client", new_callable=AsyncMock):
            yield

# ---------------------------------------------------------------------------
# Load .env file for credentials (Telegram API keys, session, etc.)
# Only sets variables not already in the environment.
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

from obs_agent.config import OBSConfig

# Persistent fixture project at project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_VAULT = _PROJECT_ROOT / "fixture_vault"
_CLONE_SCRIPT = _PROJECT_ROOT / "scripts" / "clone_vault.sh"
_DEFAULT_REAL_VAULT = _PROJECT_ROOT / "examples" / "recursive-workflow"


class AsyncIterFromList:
    """Helper to create async iterables from lists for mocking SDK query()."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


@pytest.fixture
def fixture_vault(tmp_path: Path) -> Path:
    """Path to a fixture vault for testing.

    Uses OBS_TEST_VAULT env var if set (e.g., from setup_fixture_vault.sh),
    otherwise creates a minimal temp vault structure.
    """
    env_path = os.environ.get("OBS_TEST_VAULT")
    if env_path:
        return Path(env_path)

    # Create minimal vault structure for unit tests
    vault = tmp_path / "vault"
    claude = vault / ".claude"
    (claude / "system").mkdir(parents=True)
    (claude / "skills").mkdir(parents=True)
    (claude / "memory").mkdir(parents=True)

    (vault / "CLAUDE.md").write_text("# OBS Agent\n\nTest context.\n")
    (claude / "memory.md").write_text("# Memory\n\nParent note for memory entries.\n")
    (claude / "skills.md").write_text("# Skills\n\nParent note for skills.\n")
    (claude / "system.md").write_text("# System\n\nParent note for system docs.\n")

    return vault


@pytest.fixture
def config(fixture_vault: Path) -> OBSConfig:
    """OBSConfig pointing at the fixture vault."""
    return OBSConfig(
        vault_path=fixture_vault,
        telegram_allowed_user_ids=[12345],
        telegram_state_db_path=fixture_vault / ".claude" / "telegram-state.sqlite3",
    )


@pytest.fixture
def e2e_vault(tmp_path: Path) -> Path:
    """Create a rich fixture vault for E2E tests.

    More complete than the minimal unit test vault - includes
    skills, meeting notes (immutable), and realistic content.
    """
    env_path = os.environ.get("OBS_TEST_VAULT")
    if env_path:
        return Path(env_path)

    vault = tmp_path / "vault"
    claude = vault / ".claude"

    # Core directories
    (claude / "system" / "sessions").mkdir(parents=True)
    (claude / "skills").mkdir(parents=True)
    (claude / "memory").mkdir(parents=True)
    (claude / "topics").mkdir(parents=True)
    (claude / "drafts").mkdir(parents=True)

    # Misc/Meeting Notes (immutable test data)
    meeting_notes = vault / "Misc" / "Meeting Notes"
    meeting_notes.mkdir(parents=True)
    (vault / "Misc" / "Meeting Notes.md").write_text(
        "# Meeting Notes\n\nParent note for transcripts.\n"
    )
    (meeting_notes / "2025-01-15 standup.md").write_text(
        "# 2025-01-15 Standup\n\nDiscussed project timeline.\n"
    )
    (meeting_notes / "2025-02-04 planning.md").write_text(
        "# 2025-02-04 Planning\n\nQuarterly goals review.\n"
    )

    # Vault knowledge dirs
    (vault / "Vault" / "CS").mkdir(parents=True)
    (vault / "Vault" / "CS" / "algorithms.md").write_text(
        "# Algorithms\n\nNotes on algorithms.\n"
    )

    # CLAUDE.md at vault root
    (vault / "CLAUDE.md").write_text(
        "# OBS Agent\n\n"
        "## Context\n\n"
        "### Current Focus\n"
        "Setting up the OBS Agent system.\n\n"
        "### Active Threads\n"
        "- Building the agent MVP\n"
        "- Testing vault operations\n\n"
        "### Recent Decisions\n"
        "- Using fork-based architecture (D018)\n"
    )

    # Parent notes
    (claude / "memory.md").write_text("# Memory\n\nParent note for daily memory logs.\n")
    (claude / "skills.md").write_text(
        "# Skills\n\n"
        "## Core Skills\n"
        "- file-conventions\n"
        "- update-context\n"
        "- manage-summaries\n"
        "- create-reference\n\n"
        "## Operational Skills\n"
        "- session-offboard\n"
        "- vault-search\n"
        "- git-commit\n"
    )
    (claude / "system.md").write_text("# System\n\nParent note for system docs.\n")

    # Core skill files
    for skill_name, desc in [
        ("file-conventions", "Master reference for vault file operations"),
        ("update-context", "Persist learnings to CLAUDE.md and topics"),
        ("manage-summaries", "Lazy-append one-line summaries to parent notes"),
        ("create-reference", "Create reference cards for external content"),
    ]:
        skill_dir = claude / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n"
            f"description: {desc}\n"
            f"metadata:\n  priority: core\n  triggers: always\n"
            f"---\n# {skill_name}\n\n{desc}.\n"
        )

    # Operational skill files
    for skill_name, desc in [
        ("session-offboard", "End-of-session context persistence"),
        ("vault-search", "Search strategies for finding vault content"),
        ("git-commit", "When and how to make meaningful vault commits"),
        ("daily-planning", "Planning with journal hierarchy"),
        ("process-meeting", "Meeting transcript handling"),
        ("ingest-content", "Process external content"),
        ("split-document", "Split growing files into directories"),
        ("proactive-behavior", "Connect dots and anticipate needs"),
    ]:
        skill_dir = claude / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n"
            f"description: {desc}\n"
            f"metadata:\n  priority: operational\n  triggers: on demand\n"
            f"---\n# {skill_name}\n\n{desc}.\n"
        )

    return vault


@pytest.fixture
def e2e_config(e2e_vault: Path) -> OBSConfig:
    """Config pointing at the E2E fixture vault."""
    return OBSConfig(
        vault_path=e2e_vault,
        telegram_state_db_path=e2e_vault / ".claude" / "telegram-state.sqlite3",
    )


def _ensure_fixture_vault() -> Path:
    """Ensure fixture_vault/ exists by running clone_vault.sh if needed."""
    if _FIXTURE_VAULT.is_dir():
        return _FIXTURE_VAULT
    if not _CLONE_SCRIPT.exists():
        raise FileNotFoundError(f"Clone script not found: {_CLONE_SCRIPT}")
    result = subprocess.run(
        [str(_CLONE_SCRIPT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"clone_vault.sh failed: {result.stderr}")
    if not _FIXTURE_VAULT.is_dir():
        raise RuntimeError("clone_vault.sh ran but fixture_vault/ still missing")
    return _FIXTURE_VAULT


def _resolve_real_vault_path() -> Path:
    """Resolve real vault path for safety checks."""
    raw = os.environ.get("OBS_REAL_VAULT_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_REAL_VAULT


def _same_path(a: Path, b: Path) -> bool:
    """Best-effort same-path comparison without requiring existence."""
    return a.expanduser().resolve(strict=False) == b.expanduser().resolve(strict=False)


def _ensure_template_clean_if_requested(template: Path) -> None:
    """Optional guard: fail if template fixture has uncommitted git changes."""
    required = os.environ.get("OBS_EVAL_REQUIRE_CLEAN_TEMPLATE", "").strip().lower()
    if required not in {"1", "true", "yes"}:
        return
    if not (template / ".git").is_dir():
        return
    result = subprocess.run(
        ["git", "-C", str(template), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to check template vault git status ({template}): {result.stderr}"
        )
    if result.stdout.strip():
        raise RuntimeError(
            "Template fixture vault has uncommitted changes. "
            "Refresh/clean it before running evals."
        )


def _materialize_eval_vault(template: Path) -> tuple[Path, Path]:
    """Copy template fixture into a per-run ephemeral vault."""
    run_root = Path(tempfile.mkdtemp(prefix="obs_eval_vault_run_"))
    eval_vault = run_root / "vault"
    shutil.copytree(template, eval_vault, symlinks=True)
    return run_root, eval_vault


def _assert_eval_vault_guardrails(eval_vault: Path, template: Path) -> None:
    """Prevent accidental use of the real vault path for eval runs."""
    real_vault = _resolve_real_vault_path()
    if _same_path(eval_vault, real_vault):
        raise RuntimeError(f"Unsafe eval vault path (real vault): {eval_vault}")
    if _same_path(template, real_vault):
        raise RuntimeError(
            "Unsafe fixture template path points to real vault. "
            "Set OBS_EVAL_TEMPLATE_VAULT to a safe template copy."
        )
    if _same_path(eval_vault, template):
        raise RuntimeError("Eval vault must be an ephemeral copy, not the template path")


@pytest.fixture(scope="session")
def eval_vault() -> Path:
    """Session-local ephemeral vault copy used by eval tests.

    Source template:
    - OBS_EVAL_TEMPLATE_VAULT (if set), else project fixture_vault/

    Safety:
    - Fail fast if real vault path is selected as template/target.
    - Optional clean-template guard via OBS_EVAL_REQUIRE_CLEAN_TEMPLATE=1.
    """
    template_raw = os.environ.get("OBS_EVAL_TEMPLATE_VAULT", "").strip()
    if template_raw:
        template = Path(template_raw)
        if not template.is_dir():
            raise FileNotFoundError(
                f"OBS_EVAL_TEMPLATE_VAULT does not exist or is not a directory: {template}"
            )
    else:
        template = _ensure_fixture_vault()

    _ensure_template_clean_if_requested(template)
    run_root, vault = _materialize_eval_vault(template)
    _assert_eval_vault_guardrails(vault, template)
    try:
        yield vault
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


@pytest.fixture(scope="session")
def eval_config(eval_vault: Path) -> OBSConfig:
    """OBSConfig for eval tests: real vault clone, daemon port 7833."""
    return OBSConfig(vault_path=eval_vault, daemon_port=7833)


# ---------------------------------------------------------------------------
# Register cache proxy fixtures as a pytest plugin so fixtures are discovered.
# conftest_cache_proxy.py defines proxy, proxy_port, test_project, etc.
# Must add tests/ to sys.path first since pytest doesn't auto-add it.
# ---------------------------------------------------------------------------
import sys as _sys
_tests_dir = str(Path(__file__).resolve().parent)
if _tests_dir not in _sys.path:
    _sys.path.insert(0, _tests_dir)

try:
    import conftest_cache_proxy  # noqa: F401
    pytest_plugins = ["conftest_cache_proxy"]
except ImportError:
    # conftest_cache_proxy is only present in the cache-proxy worktree
    pass
