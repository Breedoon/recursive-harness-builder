"""Independent probe roots get independent cache identities; descendants retain them."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
READY = "Reply with the single word: ready."


@pytest.fixture
def driver(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("prefix_repro_isolation_test", ROOT / "scripts/prefix_repro.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    calls = []

    class FakeClient:
        def __init__(self, *, options):
            self.options = options
            self.disconnected = False

        async def connect(self):
            return None

        async def disconnect(self):
            self.disconnected = True

        async def __aenter__(self):
            await self.connect()
            return self

        async def __aexit__(self, *_):
            await self.disconnect()

    async def turn(client, prompt, **_):
        calls.append((client, prompt))
        return "done"

    monkeypatch.setattr(module, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(module, "_options", lambda args, **kw: kw)
    monkeypatch.setattr(module, "_turn", turn)
    monkeypatch.setattr(module, "WORK_ROOT", tmp_path)
    return module, calls


@pytest.mark.asyncio
async def test_independent_roots_are_seeded_once_without_mutating_prompts(driver):
    module, calls = driver
    prompts = [READY, "Reply two."]
    await module._session(SimpleNamespace(), prompts, session_id="root-a")
    await module._session(SimpleNamespace(), prompts, session_id="root-b")
    assert prompts == [READY, "Reply two."]
    assert calls[0][1].startswith(READY) and "root-a" in calls[0][1]
    assert calls[2][1].startswith(READY) and "root-b" in calls[2][1]
    assert calls[0][1] != calls[2][1]
    assert calls[1][1] == calls[3][1] == "Reply two."


@pytest.mark.asyncio
async def test_roots_without_explicit_session_id_are_still_independent(driver):
    module, calls = driver
    await module._session(SimpleNamespace(), [READY])
    await module._session(SimpleNamespace(), [READY])
    assert calls[0][1] != calls[1][1]


@pytest.mark.asyncio
async def test_resume_does_not_add_a_new_nonce_to_the_next_user_turn(driver):
    module, calls = driver
    await module._session(SimpleNamespace(), [READY, "Continue."], resume="child-copied-from-root")
    assert [prompt for _, prompt in calls] == [READY, "Continue."]
    assert calls[0][0].options["resume"] == "child-copied-from-root"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["sc_queued_midturn", "sc_recovery", "sc_recovery_multi"])
async def test_direct_client_roots_use_the_same_initial_nonce_contract(driver, monkeypatch, scenario):
    module, calls = driver

    class FirstTurnCaptured(Exception):
        pass

    async def capture(client, prompt, **_):
        calls.append((client, prompt))
        raise FirstTurnCaptured

    monkeypatch.setattr(module, "_turn", capture)
    with pytest.raises(FirstTurnCaptured):
        await getattr(module, scenario)(SimpleNamespace())
    client, prompt = calls[0]
    assert prompt.startswith(READY)
    assert client.options["session_id"] in prompt
    assert client.disconnected


def test_actual_cache_key_distinguishes_roots_but_not_their_copied_lineage(driver):
    module, _ = driver
    names = {"_strip_cache_control", "_msg_bytes", "normalize_metadata", "assign_conversation_cache_key"}
    parsed = ast.parse((ROOT / "src/cache_proxy.py").read_text())
    nodes = [copy.deepcopy(n) for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    namespace = {"hashlib": hashlib, "json": json, "re": re, "stats": {"metadata_normalized": 0}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "cache-key-pure-functions", "exec"), namespace)

    def request(prompt):
        return {"model": "gpt-6.1-sol", "metadata": {"user_id": "fixture_session_00000000-0000-0000-0000-000000000001"},
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}

    def key(body):
        value = copy.deepcopy(body)
        namespace["normalize_metadata"](value)
        return namespace["assign_conversation_cache_key"](value)

    assert key(request(READY)) == key(request(READY))  # the old cross-case collision
    root = request(module._initial_user_prompt(READY, "root-a"))
    other = request(module._initial_user_prompt(READY, "root-b"))
    assert key(root) != key(other)
    for i, route in enumerate(("resume", "recovery", "fork"), start=2):
        child = copy.deepcopy(root)
        child["metadata"] = {"user_id": f"fixture_session_00000000-0000-0000-0000-{i:012d}"}
        child["messages"].extend([
            {"role": "assistant", "content": [{"type": "text", "text": "ready"}]},
            {"role": "user", "content": [{"type": "text", "text": "Continue."}]},
        ])
        assert child["messages"][0] == root["messages"][0]
        assert key(child) == key(root)
