"""Recovery fixture hygiene; constructed schedules are not live-gate acceptance."""
import asyncio
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@dataclass
class Tool:
    name: str
    input: dict


@dataclass
class Assistant:
    content: list


class Terminal:
    pass


@pytest.fixture
def repro(monkeypatch):
    source = Path(__file__).with_name("prefix_repro.py")
    monkeypatch.syspath_prepend(str(source.parent))
    monkeypatch.delitem(sys.modules, "prefix_diff", raising=False)
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        sdk = ModuleType("claude_agent_sdk")
        for name in ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
                     "HookMatcher", "ResultMessage", "ToolUseBlock"):
            setattr(sdk, name, type(name, (), {}))
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    spec = importlib.util.spec_from_file_location("recovery_effects_repro", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AssistantMessage", Assistant)
    monkeypatch.setattr(module, "ToolUseBlock", Tool)
    monkeypatch.setattr(module, "ResultMessage", Terminal)
    assert Path(module.pd.__file__).resolve() == source.with_name("prefix_diff.py").resolve()
    return module


def sleep_message():
    return Assistant([Tool("Bash", {"command": "sleep 60"})])


def waiting_stream(closed):
    async def stream():
        try:
            yield sleep_message()
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    return stream()


def test_ready_effects_are_observed_without_a_settle_delay(repro, tmp_path):
    path = tmp_path / "effects.txt"
    path.write_text("step3\nstep4\n")
    closed = []
    result = asyncio.run(repro._wait_recovery_effects(waiting_stream(closed), path, 1))
    assert result["status"] == "observed"
    assert result["checks"] == 1
    assert result["sleep_tool_seen"]
    assert result["effects_wait_seconds"] < 0.05
    assert closed == [True]


def test_late_effects_are_read_from_the_actual_file(repro, tmp_path):
    path = tmp_path / "effects.txt"
    closed = []

    async def run():
        async def write():
            path.write_text("step3\n")
            await asyncio.sleep(0.08)
            path.write_text("step3\nstep4\n")
        producer = asyncio.create_task(write())
        result = await repro._wait_recovery_effects(waiting_stream(closed), path, 1)
        await producer
        return result

    result = asyncio.run(run())
    assert result["status"] == "observed"
    assert result["checks"] >= 2
    assert result["effects_wait_seconds"] >= 0.06
    assert closed == [True]


@pytest.mark.parametrize("ledger", [None, "step3\n", "step4\nstep3\n", "step3\nstep4\nstep3\n"])
def test_missing_or_wrong_effects_remain_bounded_failures(repro, tmp_path, ledger):
    path = tmp_path / "effects.txt"
    if ledger is not None:
        path.write_text(ledger)
    closed = []
    result = asyncio.run(repro._wait_recovery_effects(waiting_stream(closed), path, 0.03))
    assert result["status"] == "timed_out"
    assert result["sleep_tool_seen"]
    assert result["effects_wait_seconds"] >= 0.03
    assert closed == [True]


def test_post_tool_deadline_does_not_shorten_model_response_time(repro, tmp_path):
    path = tmp_path / "effects.txt"
    path.write_text("step3\nstep4\n")

    async def stream():
        await asyncio.sleep(0.06)
        yield sleep_message()
        await asyncio.Event().wait()

    result = asyncio.run(repro._wait_recovery_effects(stream(), path, 0.02))
    assert result["status"] == "observed"
    assert result["elapsed_seconds"] >= 0.05
    assert result["effects_wait_seconds"] < 0.02


@pytest.mark.parametrize("terminal", [False, True])
def test_complete_ledger_without_inflight_milestone_cannot_pass(repro, tmp_path, terminal):
    path = tmp_path / "effects.txt"
    path.write_text("step3\nstep4\n")

    async def stream():
        yield Terminal() if terminal else object()

    result = asyncio.run(repro._wait_recovery_effects(stream(), path, 1))
    assert result["status"] == ("terminal_before_ready" if terminal else "stream_ended")
    assert not result["sleep_tool_seen"]


def test_terminal_before_effects_is_not_recovered_as_valid_setup(repro, tmp_path):
    async def stream():
        yield sleep_message()
        yield Terminal()

    result = asyncio.run(repro._wait_recovery_effects(stream(), tmp_path / "absent", 1))
    assert result["status"] == "terminal_before_ready"
    assert result["sleep_tool_seen"]


def test_stream_keeps_draining_while_effects_are_pending(repro, tmp_path):
    path = tmp_path / "effects.txt"
    consumed = []

    async def stream():
        yield sleep_message()
        for i in range(150):
            consumed.append(i)
            if i == 125:
                path.write_text("step3\nstep4\n")
            yield object()
        await asyncio.Event().wait()

    result = asyncio.run(repro._wait_recovery_effects(stream(), path, 1))
    assert result["status"] == "observed"
    assert len(consumed) >= 126


def test_outer_cancellation_closes_the_message_iterator(repro, tmp_path):
    closed = []

    async def stream():
        try:
            await asyncio.Event().wait()
            yield object()
        finally:
            closed.append(True)

    async def run():
        await asyncio.wait_for(repro._wait_recovery_effects(stream(), tmp_path / "absent", 1), 0.02)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
    assert closed == [True]


@pytest.fixture
def scenario_environment(repro, monkeypatch, tmp_path):
    repro.WORK_ROOT = tmp_path
    source = tmp_path / "parent.jsonl"
    source.write_bytes(b"constructed immutable parent state")
    state = SimpleNamespace(disconnected=False, forks=0)
    target = SimpleNamespace(target_uuid="safe-target",
                             health=SimpleNamespace(needs_recovery=True, unsafe_tail_reason="inflight"))

    def resolve(**kwargs):
        return target

    def fork(**kwargs):
        state.forks += 1
        return "fixture-child"

    for name, members in {
        "obs_agent.jsonl_fork": {"fork_session_jsonl": fork},
        "obs_agent.jsonl_health": {"resolve_safe_jsonl_target": resolve},
        "obs_agent.context_jsonl": {"find_session_jsonl": lambda **kwargs: source},
    }.items():
        module = ModuleType(name)
        for key, value in members.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(repro.uuid, "uuid4", lambda: "fixture-parent")
    monkeypatch.setattr(repro, "_options", lambda *args, **kwargs: None)

    async def no_turn(*args, **kwargs):
        return None

    monkeypatch.setattr(repro, "_turn", no_turn)
    monkeypatch.setattr(repro, "_session", no_turn)
    return state, source


def test_scenario_disconnects_but_never_forks_an_unready_fixture(repro, monkeypatch, scenario_environment):
    state, _ = scenario_environment

    class Client:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            pass

        async def query(self, text):
            pass

        async def receive_messages(self):
            yield Terminal()

        async def disconnect(self):
            state.disconnected = True

    monkeypatch.setattr(repro, "ClaudeSDKClient", Client)
    with pytest.raises(RuntimeError, match="required effects before recovery not observed"):
        asyncio.run(repro.sc_recovery_multi(SimpleNamespace()))
    assert state.disconnected
    assert state.forks == 0


@pytest.mark.parametrize("change", [None, "duplicate_effect", "source_changed"])
def test_existing_post_recovery_predicates_are_not_weakened(repro, monkeypatch, scenario_environment, change):
    state, source = scenario_environment
    ledger = repro.WORK_ROOT / "cwd/effects-fixture-parent.txt"

    class Client:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            pass

        async def query(self, text):
            ledger.write_text("step3\nstep4\n")

        async def receive_messages(self):
            yield sleep_message()
            await asyncio.Event().wait()

        async def disconnect(self):
            state.disconnected = True

    async def recovered(*args, **kwargs):
        if change == "duplicate_effect":
            with ledger.open("a") as output:
                output.write("step3\n")
        if change == "source_changed":
            source.write_bytes(b"changed source control")

    monkeypatch.setattr(repro, "ClaudeSDKClient", Client)
    monkeypatch.setattr(repro, "_session", recovered)
    sessions, meta = asyncio.run(repro.sc_recovery_multi(SimpleNamespace()))
    assert sessions == ["fixture-parent", "fixture-child"]
    assert state.disconnected and state.forks == 1
    assert meta["effects_unchanged"] == (change != "duplicate_effect")
    assert meta["source_unchanged"] == (change != "source_changed")
