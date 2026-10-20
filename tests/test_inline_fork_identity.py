"""vault-mngu: an inline reply-fork is the SAME agent on a new conversation branch.

Replying to an earlier message in a topic must keep the agent's team key,
agent name and lineage, must not prime a fork bootstrap, and must leave the
original branch reachable (rewind). Before the fix the fork re-derived the root
team key from the current time, so every inline fork minted a new tree.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from obs_agent.lineage import ObsBootstrap
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import TelegramBot, TelegramRoute, _TelegramMessageBinding

_TEST_GAP = 0.05
TEAM = "2026-10-13-14-11-proc-work"


def _bot_and_state(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = bot._get_state(TelegramRoute(chat_id=67890, thread_id=None))
    state.session_manager.set_session_id("sid-root")
    return bot, state


def _bind(bot, state, message_id: int, *, session_id: str, uuid: str) -> None:
    bot._message_map[(67890, message_id)] = _TelegramMessageBinding(
        jsonl_uuid=uuid,
        session_id=session_id,
        role="assistant",
        route=state.route,
    )


def _trigger(reply_to: int) -> QueuedMessage:
    return QueuedMessage(text="follow up", telegram_message_id=99, reply_to_message_id=reply_to)


def _fake_bot():
    fake = MagicMock()
    fake.send_message = AsyncMock()
    return fake


def _bootstrap(team: str, agent: str, lineage: tuple[str, ...]) -> ObsBootstrap:
    return ObsBootstrap(
        raw_xml="<obs-bootstrap/>",
        lineage=lineage,
        origin="trunk_start",
        is_fork=False,
        session_id="sid-root",
        agent_id=None,
        parent_session_id=None,
        root_team_key=team,
        agent_name=agent,
        parent_agent_name=None,
        parent_display_name=None,
    )


class TestInlineForkIsSameAgent:
    async def test_identity_survives_fork_after_clock_moves(self, config):
        bot, state = _bot_and_state(config)
        state.agent_lineage = ("Proc work",)
        state.session_manager.set_sdk_env_overrides(
            bot._build_team_worker_env(team_name=TEAM, agent_name=TEAM)
        )
        _bind(bot, state, 5, session_id="sid-root", uuid="assistant-1")
        bot._session_heads["sid-root"] = "assistant-latest"

        # Four days later: the old code stamped this time into a new root key.
        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork"), \
                patch("obs_agent.lineage.time.time", return_value=1_792_000_000.0):
            proceed, _ = await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(5), bot=_fake_bot(),
            )

        assert proceed is True
        assert state.session_id == "sid-fork"
        env = state.session_manager.sdk_env_overrides
        assert env["CLAUDE_CODE_TEAM_NAME"] == TEAM
        assert env["CLAUDE_CODE_AGENT_NAME"] == TEAM
        assert state.agent_lineage == ("Proc work",)
        assert state.pending_obs_bootstrap is None, "an inline fork must not prime a bootstrap"
        assert bot._route_inbox_target_keys_by_route[state.route] == (TEAM, TEAM)

    async def test_identity_recovered_from_source_jsonl_when_env_missing(self, config):
        bot, state = _bot_and_state(config)
        _bind(bot, state, 5, session_id="sid-root", uuid="assistant-1")
        bot._session_heads["sid-root"] = "assistant-latest"
        boot = _bootstrap(TEAM, "child-x", ("Proc work", "Child X"))

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork"), \
                patch("obs_agent.telegram.find_latest_obs_bootstrap_for_session", return_value=boot):
            proceed, _ = await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(5), bot=_fake_bot(),
            )

        assert proceed is True
        env = state.session_manager.sdk_env_overrides
        assert env["CLAUDE_CODE_TEAM_NAME"] == TEAM
        assert env["CLAUDE_CODE_AGENT_NAME"] == "child-x"
        assert state.agent_lineage == ("Proc work", "Child X")
        assert state.pending_obs_bootstrap is None

    async def test_rewind_to_original_branch_keeps_identity(self, config):
        bot, state = _bot_and_state(config)
        state.agent_lineage = ("Proc work",)
        state.session_manager.set_sdk_env_overrides(
            bot._build_team_worker_env(team_name=TEAM, agent_name=TEAM)
        )
        _bind(bot, state, 5, session_id="sid-root", uuid="assistant-1")
        _bind(bot, state, 9, session_id="sid-root", uuid="assistant-latest")
        bot._session_heads["sid-root"] = "assistant-latest"

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork"):
            await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(5), bot=_fake_bot(),
            )
        assert state.session_id == "sid-fork"

        # Reply to the original branch's latest message: rewind, no new fork.
        with patch("obs_agent.telegram.fork_session_jsonl") as mock_fork:
            proceed, _ = await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(9), bot=_fake_bot(),
            )
        assert proceed is True
        mock_fork.assert_not_called()
        assert state.session_id == "sid-root"
        env = state.session_manager.sdk_env_overrides
        assert env["CLAUDE_CODE_TEAM_NAME"] == TEAM
        assert state.pending_obs_bootstrap is None

    async def test_fork_without_any_identity_falls_back_to_priming(self, config):
        bot, state = _bot_and_state(config)
        _bind(bot, state, 5, session_id="sid-root", uuid="assistant-1")
        bot._session_heads["sid-root"] = "assistant-latest"

        with patch("obs_agent.telegram.fork_session_jsonl", return_value="sid-fork"), \
                patch("obs_agent.telegram.find_latest_obs_bootstrap_for_session", return_value=None):
            proceed, _ = await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(5), bot=_fake_bot(),
            )

        assert proceed is True
        assert state.pending_obs_bootstrap is not None
        assert 'origin>inline_fork<' in state.pending_obs_bootstrap or "inline_fork" in state.pending_obs_bootstrap

    async def test_unmapped_reply_still_refuses(self, config):
        bot, state = _bot_and_state(config)
        fake = _fake_bot()
        with patch("obs_agent.telegram.fork_session_jsonl") as mock_fork:
            proceed, _ = await bot._resolve_session_for_trigger(
                state=state, trigger_message=_trigger(12345), bot=fake,
            )
        assert proceed is False
        mock_fork.assert_not_called()
