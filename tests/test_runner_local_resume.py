"""Local-model turns that end in a backend API error resume themselves (bead obs-8)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock

from obs_agent import runner as runner_mod
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent, _is_transient_local_error


def _api_error(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [TextBlock(text=text)]
    msg.session_id = "sid-1"
    msg.isApiErrorMessage = True
    msg.error = None
    msg.num_turns = None
    msg.total_cost_usd = None
    return msg


def _text(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [TextBlock(text=text)]
    msg.session_id = "sid-1"
    msg.isApiErrorMessage = False
    msg.error = None
    return msg


def _client(turns: list[list]) -> AsyncMock:
    client = AsyncMock()
    calls = iter(turns)

    async def receive():
        for m in next(calls):
            yield m
    client.receive_response = receive
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    return client


async def _run(config, model, turns, *, healthy=True):
    hook_state = HookState()
    from obs_agent.session import SessionManager

    mgr = SessionManager(config=config, hook_state=hook_state)
    mgr.model_override = model
    client = _client(turns)
    waits = []

    async def wait(self, url):
        waits.append(url)
        return healthy
    with patch.object(mgr, "get_client", new=AsyncMock(return_value=client)), \
            patch.object(ConversationRunner, "_wait_for_local_backend", new=wait):
        r = ConversationRunner(mgr, hook_state, config)
        events = [e async for e in r.run("do the task")]
    return events, client, waits


@pytest.mark.parametrize("text", [
    "API Error: The socket connection was closed unexpectedly.",
    "API Error: 529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}",
    "API Error: Unable to connect to API (ConnectionRefused)",
    "API Error: overloaded_error: local upstream wedged: no engine progress (watchdog recovering; retry)",
])
def test_transient_local_errors(text):
    assert _is_transient_local_error(text)


@pytest.mark.parametrize("text", [
    "Prompt is too long", "API Error: 400 invalid_request_error: bad tool", "Done.", None,
])
def test_non_transient_errors(text):
    assert not _is_transient_local_error(text)


async def test_spark_turn_ending_in_backend_error_resumes_after_recovery(config):
    events, client, waits = await _run(
        config, "local-sparks-glm-5.3-flash-uncensored",
        [[_api_error("API Error: The socket connection was closed unexpectedly.")],
         [_text("Continued and finished.")]])
    assert waits and waits[0].endswith("/health")
    assert client.query.await_count == 2
    resume = client.query.await_args_list[1].args[0]
    assert resume.startswith("(System: The local model backend failed")
    assert "socket connection was closed" in resume
    assert [e.text for e in events if isinstance(e, TextEvent)][-1] == "Continued and finished."


async def test_resumes_are_bounded(config):
    err = [_api_error("API Error: 529 overloaded_error")]
    events, client, waits = await _run(
        config, "local-sparks-glm-5.3-flash-uncensored", [err] * (runner_mod._LOCAL_RESUME_MAX + 1))
    assert len(waits) == runner_mod._LOCAL_RESUME_MAX
    assert client.query.await_count == runner_mod._LOCAL_RESUME_MAX + 1


async def test_backend_not_recovering_leaves_the_turn_ended(config):
    events, client, waits = await _run(
        config, "local-sparks-glm-5.3-flash-uncensored",
        [[_api_error("API Error: 529 overloaded_error")]], healthy=False)
    assert len(waits) == 1 and client.query.await_count == 1


async def test_hosted_model_errors_are_not_auto_resumed(config):
    events, client, waits = await _run(
        config, "claude-opus-4-6", [[_api_error("API Error: 529 overloaded_error")]])
    assert waits == [] and client.query.await_count == 1


async def test_permanent_local_error_is_not_resumed(config):
    events, client, waits = await _run(
        config, "local-sparks-glm-5.3-flash-uncensored", [[_api_error("Prompt is too long")]])
    assert waits == [] and client.query.await_count == 1


@pytest.mark.parametrize("doc,expected", [
    ({"status": "ok", "local_upstream": {"state": "healthy"}}, True),
    ({"status": "ok", "local_upstream": {"state": "unknown"}}, True),   # watchdog absent
    ({"status": "ok"}, True),                                           # route without a watchdog
])
async def test_wait_for_local_backend_reads_the_verdict(config, monkeypatch, doc, expected):
    import io
    import json as _json
    hook_state = HookState()
    from obs_agent.session import SessionManager

    r = ConversationRunner(SessionManager(config=config, hook_state=hook_state), hook_state, config)
    monkeypatch.setattr(runner_mod, "_LOCAL_RESUME_MIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(runner_mod.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(_json.dumps(doc).encode()))
    assert await r._wait_for_local_backend("http://x/health") is expected


async def test_wait_for_local_backend_gives_up_while_unhealthy(config, monkeypatch):
    import io
    import json as _json
    hook_state = HookState()
    from obs_agent.session import SessionManager

    r = ConversationRunner(SessionManager(config=config, hook_state=hook_state), hook_state, config)
    monkeypatch.setattr(runner_mod, "_LOCAL_RESUME_MIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(runner_mod, "_LOCAL_RESUME_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(runner_mod, "_LOCAL_RESUME_POLL_SECONDS", 0.01)
    doc = {"local_upstream": {"state": "restarting"}}
    monkeypatch.setattr(runner_mod.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(_json.dumps(doc).encode()))
    assert await r._wait_for_local_backend("http://x/health") is False
