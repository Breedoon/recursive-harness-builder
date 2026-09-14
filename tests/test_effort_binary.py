"""Installed Claude binary -> loopback-only fake API, never a real provider.

Reuse the context compatibility suite's isolated workspace and fake Anthropic
server. These probes verify that the environment reaches an actual HTTP body,
not merely that ClaudeAgentOptions contains the desired strings.
"""
import json
import os
import subprocess

import pytest

from test_claude_context_binary import _fake_anthropic_api, binary_workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("OBS_RUN_LOCAL_CLAUDE_CONTEXT_TESTS") != "1",
    reason="Explicit opt-in and a loopback-only network namespace are required",
)


@pytest.mark.parametrize("model,effort", [
    *(('gpt-5.6-sol', level) for level in ('low', 'medium', 'high', 'xhigh', 'max')),
    *(('claude-opus-4-6', level) for level in ('low', 'medium', 'high', 'max')),
])
def test_installed_cli_sends_effort_in_http_request(binary_workspace, tmp_path, model, effort):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from obs_agent.config import OBSConfig
    from obs_agent.session import SessionManager

    project, home = binary_workspace
    manager = SessionManager(config=OBSConfig(
        vault_path=project, model=model, cache_proxy_enabled=False,
    ))
    manager.effort_override = effort
    options = manager.create_options()
    options.mcp_servers = {}
    options.hooks = {}
    options.tools = []
    options.permission_mode = "default"
    options.max_turns = 1
    options.system_prompt = "Answer briefly."
    options.extra_args = {}
    transport = SubprocessCLITransport(prompt="unused", options=options)

    with _fake_anthropic_api(capture_bodies=True) as (base_url, requests):
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home), "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "sk-offline-effort-probe",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            **json.loads(options.settings)["env"],
        }
        user_message = {
            "type": "user", "session_id": "default", "parent_tool_use_id": None,
            "message": {"role": "user", "content": "Reply with OK."},
        }
        result = subprocess.run(
            transport._build_command(), input=json.dumps(user_message) + "\n",
            cwd=project, env=child_env, text=True, capture_output=True, timeout=45,
        )
    diagnostic = f"exit={result.returncode}\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
    assert result.returncode == 0, diagnostic
    bodies = [body for path, body in requests if path.split("?")[0] == "/v1/messages"]
    assert bodies, diagnostic
    for body in bodies:
        assert body.get("output_config", {}).get("effort") == effort, body
        if model.startswith("gpt-"):
            assert body["thinking"]["type"] == "adaptive", body
            assert "budget_tokens" not in body["thinking"], body
