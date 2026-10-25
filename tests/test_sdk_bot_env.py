"""Model-free checks at the SDK's real subprocess boundary."""

import json
import os
from unittest.mock import patch

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

import obs_agent.session  # Install the same SDK patches as every managed agent.


@pytest.mark.asyncio
@pytest.mark.parametrize("resume,fork", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("profile", ["prod", "test"])
async def test_real_sdk_spawns_strip_production_tokens(tmp_path, resume, fork, profile):
    evidence = tmp_path / "children.jsonl"
    cli = tmp_path / "fake-cli"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "keys=['OBS_PROD_TELEGRAM_BOT_TOKEN','OBS_PROD_TELEGRAM_BOT_TOKENS',"
        "'OBS_TELEGRAM_BOT_TOKEN','OBS_TELEGRAM_BOT_TOKENS','OBS_TEST_TELEGRAM_BOT_TOKEN',"
        "'OBS_MEDIA_TELEGRAM_BOT_TOKEN','ALIAS','MIXED','OVERRIDE','OTHER']\n"
        "with open(os.environ['CHILD_EVIDENCE'],'a') as f: "
        "f.write(json.dumps({'version':'-v' in sys.argv,'env':{k:os.environ[k] for k in keys if k in os.environ}})+'\\n')\n"
        "if '-v' in sys.argv: print('2.1.59')\n"
    )
    cli.chmod(0o700)
    parent = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CHILD_EVIDENCE": str(evidence),
        "OBS_PROFILE": profile,
        "OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-prod-one",
        "OBS_PROD_TELEGRAM_BOT_TOKENS": "fixture-prod-one,fixture-prod-two",
        "OBS_TELEGRAM_BOT_TOKEN": "fixture-prod-one" if profile == "prod" else "fixture-test",
        "OBS_TELEGRAM_BOT_TOKENS": "fixture-prod-two,fixture-test",
        "OBS_TEST_TELEGRAM_BOT_TOKEN": "fixture-test",
        "OBS_MEDIA_TELEGRAM_BOT_TOKEN": "fixture-media",
        "ALIAS": "fixture-prod-two",
        "MIXED": "fixture-prod-one,fixture-media",
        "OTHER": "keep-me",
    }
    overrides = {"OVERRIDE": "fixture-prod-two", "OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-prod-override"}
    options = ClaudeAgentOptions(cli_path=str(cli), cwd=str(tmp_path), env=overrides,
                                resume="fixture-resume" if resume else None, fork_session=fork)
    native_open_process = anyio.open_process

    async def checked_open_process(*args, **kwargs):
        assert dict(os.environ) == parent
        assert options.env == overrides
        return await native_open_process(*args, **kwargs)

    with patch.dict(os.environ, parent, clear=True):
        with patch("anyio.open_process", side_effect=checked_open_process):
            transport = SubprocessCLITransport(prompt="not sent to a model", options=options)
            await transport.connect()
            await transport._process.wait()
            await transport.close()
        assert dict(os.environ) == parent
        assert options.env == overrides
    children = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert [child["version"] for child in children] == [True, False]
    for child in children:
        env = child["env"]
        assert all("fixture-prod" not in value for value in env.values())
        assert "OBS_PROD_TELEGRAM_BOT_TOKEN" not in env
        assert "OBS_PROD_TELEGRAM_BOT_TOKENS" not in env
        assert env["OBS_TEST_TELEGRAM_BOT_TOKEN"] == "fixture-test"
        assert env["OBS_MEDIA_TELEGRAM_BOT_TOKEN"] == "fixture-media"
        assert env["OBS_TELEGRAM_BOT_TOKENS"] == "fixture-test"
        assert env["MIXED"] == "fixture-media"
        assert env["OTHER"] == "keep-me"
        if profile == "test":
            assert env["OBS_TELEGRAM_BOT_TOKEN"] == "fixture-test"


@pytest.mark.asyncio
async def test_failed_spawn_restores_daemon_env_and_explicit_overrides(tmp_path):
    options = ClaudeAgentOptions(cli_path="/not/a/real/cli", cwd=str(tmp_path),
                                env={"OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-override"})
    parent = {"OBS_PROFILE": "prod", "OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-prod"}
    with patch.dict(os.environ, parent, clear=True):
        transport = SubprocessCLITransport(prompt="unused", options=options)
        with pytest.raises(Exception):
            await transport.connect()
        assert dict(os.environ) == parent
        assert options.env == {"OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-override"}


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["prod", "test"])
async def test_generic_only_profiles_preserve_nonproduction_tokens(profile):
    from claude_agent_sdk._internal.transport import subprocess_cli

    parent = {"OBS_PROFILE": profile, "OBS_TELEGRAM_BOT_TOKEN": "fixture-generic"}
    observed = {}

    async def open_process(*args, **kwargs):
        observed.update(kwargs["env"])
        assert dict(os.environ) == parent

    with patch.dict(os.environ, parent, clear=True):
        with patch("anyio.open_process", side_effect=open_process):
            await subprocess_cli.anyio.open_process(["unused"])
    assert ("OBS_TELEGRAM_BOT_TOKEN" in observed) == (profile == "test")


@pytest.mark.asyncio
async def test_explicit_env_does_not_mutate_shared_anyio_or_daemon():
    import asyncio
    from claude_agent_sdk._internal.transport import subprocess_cli

    parent = {"OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-prod"}
    override = {"OBS_PROD_TELEGRAM_BOT_TOKEN": "fixture-override", "ALIAS": "fixture-prod", "KEEP": "fixture-test"}
    calls = []

    async def open_process(*args, **kwargs):
        assert dict(os.environ) == parent
        calls.append(kwargs["env"])
        await asyncio.sleep(0)
        assert dict(os.environ) == parent
        raise asyncio.CancelledError

    async def spawn():
        with pytest.raises(asyncio.CancelledError):
            await subprocess_cli.anyio.open_process(["unused"], env=override)

    with patch.dict(os.environ, parent, clear=True):
        with patch("anyio.open_process", side_effect=open_process) as patched:
            await asyncio.gather(spawn(), spawn())
            assert anyio.open_process is patched
        assert dict(os.environ) == parent
    assert calls == [{"KEEP": "fixture-test"}, {"KEEP": "fixture-test"}]
    assert override["ALIAS"] == "fixture-prod"
