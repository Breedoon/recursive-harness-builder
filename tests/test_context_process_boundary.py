"""Exercise SessionManager -> real SDK transport -> child-observed argv/env.

The child is an inert executable probe, not a model. The SDK transport is not
mocked. Binary interpretation is covered separately by the loopback-only test.
"""

import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from obs_agent.config import OBSConfig
from obs_agent.session import SessionManager


BUDGET_KEYS = (
    "OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
)


@pytest.fixture
def isolated_options(monkeypatch):
    for key in (
        "DISABLE_COMPACT", "DISABLE_AUTO_COMPACT", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("obs_agent.session.create_obs_tools", lambda *args, **kwargs: {})
    monkeypatch.setattr("obs_agent.session.create_hook_matchers", lambda *args, **kwargs: {})


@pytest.fixture
def probe_executable(tmp_path: Path) -> Path:
    if os.name == "nt":
        pytest.skip("The executable shebang probe is exercised on Ubuntu CI")
    probe = tmp_path / "claude-process-probe"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('2.1.59 (Claude Code)')\n"
        "else:\n"
        f"    keys = {BUDGET_KEYS!r}\n"
        "    print(json.dumps({'type': 'probe', 'argv': sys.argv[1:], "
        "'budget_env': {key: os.environ.get(key) for key in keys}, 'pid': os.getpid()}))\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    return probe


async def observe_sdk_child(options, probe_executable: Path) -> dict:
    options.cli_path = str(probe_executable)
    transport = SubprocessCLITransport(prompt="unused", options=options)
    try:
        await transport.connect()
        messages = [message async for message in transport.read_messages()]
    finally:
        await transport.close()
    assert len(messages) == 1
    assert messages[0]["type"] == "probe"
    assert messages[0]["pid"] != os.getpid()
    return messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("window, selector", [(100_000, "200k"), (200_000, "200k"),
                                              (400_000, "1m"), (1_000_000, "1m")])
async def test_actual_sdk_child_receives_requested_budget(
    tmp_path, monkeypatch, isolated_options, probe_executable, window, selector,
):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "10")
    manager = SessionManager(config=OBSConfig(vault_path=tmp_path, cache_proxy_enabled=False))
    manager.model_override = f"gpt[{window // 1000}k]"
    manager.set_sdk_env_overrides({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000",
                                   "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "20"})
    options = manager.create_options()
    observed = await observe_sdk_child(options, probe_executable)

    model_arg = observed["argv"][observed["argv"].index("--model") + 1]
    assert model_arg == f"gpt-6-sol[{selector}]"
    assert observed["budget_env"] == {key: options.env[key] for key in BUDGET_KEYS}
    # Stale DAEMON-level values (10 / 200000 above) are superseded by the plan,
    # but explicit PER-SESSION values win (vault-u3b.13, R3: "these environmental
    # variables ... override our settings").
    assert observed["budget_env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"
    assert observed["budget_env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "20"
    settings_arg = observed["argv"][observed["argv"].index("--settings") + 1]
    settings_env = json.loads(settings_arg)["env"]
    assert {key: settings_env[key] for key in BUDGET_KEYS} == observed["budget_env"]
    assert manager.hook_state.effective_model.endswith(f"[{window // 1000}k]" if window < 1_000_000 else "[1m]")
    assert os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "10"


@pytest.mark.asyncio
async def test_inherited_child_keeps_400k_while_fresh_child_can_select_100k(
    tmp_path, isolated_options, probe_executable,
):
    config = OBSConfig(vault_path=tmp_path, model="gpt[400k]", cache_proxy_enabled=False)
    parent = SessionManager(config=config)
    parent_options = parent.create_options()
    inherited = SessionManager(config=config)
    inherited.model_override = parent.hook_state.effective_model
    child = SessionManager(config=config)
    child.model_override = "gpt[100k]"
    # Inheriting the parent's non-budget env must not pin the child's budget.
    # (Explicit per-session values for the plan-owned keys now win on purpose,
    # vault-u3b.13 / R3, so a real launcher must not copy those keys blindly.)
    from obs_agent.claude_context import CONTEXT_PLAN_ENV_KEYS

    child.set_sdk_env_overrides(
        {k: v for k, v in parent_options.env.items() if k not in CONTEXT_PLAN_ENV_KEYS}
    )

    inherited_options = inherited.create_options()
    child_options = child.create_options()
    inherited_observation = await observe_sdk_child(inherited_options, probe_executable)
    child_observation = await observe_sdk_child(child_options, probe_executable)
    assert inherited_observation["budget_env"]["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "400000"
    assert child_observation["budget_env"]["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "100000"
    assert config.model == "gpt[400k]"
    assert parent_options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"


@pytest.mark.asyncio
@pytest.mark.real_get_client
async def test_reconnect_rebuilds_controls_and_preserves_session(
    tmp_path, isolated_options, probe_executable,
):
    manager = SessionManager(config=OBSConfig(vault_path=tmp_path, cache_proxy_enabled=False))
    manager.model_override = "gpt[400k]"
    manager.set_session_id("resume-context-regression")
    observed = []

    def make_client(options):
        async def connect():
            observed.append(await observe_sdk_child(options, probe_executable))
        return AsyncMock(connect=AsyncMock(side_effect=connect))

    with patch("obs_agent.session.ClaudeSDKClient", side_effect=make_client):
        await manager.reconnect()
        manager.model_override = "gpt[100k]"
        await manager.reconnect()
        await manager.disconnect()
    assert [row["budget_env"]["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] for row in observed] == ["400000", "100000"]
    for row in observed:
        assert row["argv"][row["argv"].index("--resume") + 1] == "resume-context-regression"
    assert manager.session_id == "resume-context-regression"


def test_operator_cap_and_requested_metadata_are_distinct(tmp_path, isolated_options):
    manager = SessionManager(config=OBSConfig(
        vault_path=tmp_path, model="gpt[400k]", auto_compact_window_tokens=150_000,
        cache_proxy_enabled=False,
    ))
    options = manager.create_options()
    assert options.model == "gpt-6-sol[1m]"
    assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "400000"
    assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
    assert int(980_000 * float(options.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]) / 100) == 117_000
    assert manager.hook_state.effective_model == "gpt-6-sol[400k]"
