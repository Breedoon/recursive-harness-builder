"""Scoped /unschedule and global /stop all (bead vault-maw0)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import schedule_scope
from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

from tests.test_telegram import _TEST_GAP, _make_context, _make_update, _state

TEAM = "2026-08-12-08-05-root"
OTHER_TEAM = "2026-08-13-09-00-other"


def test_creation_ownership_is_warning_only_prefix_or_mapping():
    owners = schedule_scope.ScheduleOwnership(
        beads={"ops-1": {"status": "open"}, "ops-2": {"status": "closed"}},
        owners={"mapped": {"bead": "ops-1"}},
    )
    assert "schedule ownership rule" in owners.creation_warnings(schedule_id="new", description="unnamed")[0]
    assert not owners.creation_warnings(schedule_id="new", description="[ops-1] work")
    assert not owners.creation_warnings(schedule_id="mapped", description="legacy")
    assert "orphaned" in owners.creation_warnings(schedule_id="new", description="[ops-2] closed")[0]
    assert "orphaned" in owners.creation_warnings(schedule_id="new", description="[ops-missing] missing")[0]


def test_adopted_nonbead_route_tag_matches_inventory_and_stays_protected():
    owners = schedule_scope.ScheduleOwnership(
        beads={"ops-1": {"status": "open", "labels": ["responsibility"]},
               "ops-2": {"status": "closed"}},
        owners={"mapped": {"bead": "ops-1"}},
    )
    assert owners.owner_of(schedule_id="mapped", description="[deckpress-phase2] legacy")[0] == "ops-1"
    assert not owners.creation_warnings(schedule_id="mapped", description="[deckpress-phase2] legacy")
    assert owners.protection(schedule_id="mapped", description="[deckpress-phase2] legacy", tree=TEAM).protected
    # A real bead prefix still wins, including a closed one: do not silently reparent it.
    assert owners.owner_of(schedule_id="mapped", description="[ops-2] closed")[0] == "ops-2"
    assert "orphaned" in owners.creation_warnings(schedule_id="mapped", description="[ops-2] closed")[0]


def _sched(bot, schedule_id, route, description=""):
    bot._register_topic_schedule(
        _TopicScheduleRecord(
            schedule_id=schedule_id,
            route=route,
            description=description,
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=300,
            prompt="run",
            reset_session=False,
            next_run_at=5000.0,
        )
    )


def _tree(bot):
    """Root, branch, leaf (under branch), sibling in TEAM; one agent in OTHER_TEAM.

    The leaf and the other-team agent have no client (idle/cold) to prove schedules are found
    from the schedule store, not only from active routes.
    """
    root = _state(bot, thread_id=None)
    branch = _state(bot, thread_id=101)
    leaf = _state(bot, thread_id=102)
    sibling = _state(bot, thread_id=103)
    other = _state(bot, chat_id=11111, thread_id=7)
    root.agent_lineage = ("Root",)
    branch.agent_lineage = ("Root", "Branch")
    leaf.agent_lineage = ("Root", "Branch", "Leaf")
    sibling.agent_lineage = ("Root", "Sibling")
    other.agent_lineage = ("Other",)
    ids = {
        root.route: (TEAM, TEAM),
        branch.route: (TEAM, "branch-agent"),
        leaf.route: (TEAM, "leaf-agent"),
        sibling.route: (TEAM, "sibling-agent"),
        other.route: (OTHER_TEAM, OTHER_TEAM),
    }
    for st in (root, branch, leaf, sibling, other):
        team, agent = ids[st.route]
        st.session_manager.set_sdk_env_overrides(
            {"CLAUDE_CODE_TEAM_NAME": team, "CLAUDE_CODE_AGENT_NAME": agent}
        )
    bot._route_inbox_target_keys_by_route.update(ids)
    bot._route_inbox_targets.update({v: k for k, v in ids.items()})
    return root, branch, leaf, sibling, other


@pytest.fixture
def ownership_files(tmp_path, monkeypatch):
    beads = tmp_path / "issues.jsonl"
    owners = tmp_path / "owners.json"
    rows = [
        {"id": "ops-2", "status": "open", "labels": ["responsibility"], "metadata": {"owner_team": TEAM}},
        {"id": "ops-1", "status": "open", "labels": [], "metadata": {"owner_team": TEAM}},
        {"id": "reg-1", "status": "open", "labels": [], "metadata": {"owner_team": OTHER_TEAM}},
        {"id": "old-1", "status": "closed", "labels": ["responsibility"], "metadata": {}},
    ]
    beads.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    owners.write_text(json.dumps({"schedules": {"s-adopted": {"bead": "reg-1", "owner_team": OTHER_TEAM}}}))
    monkeypatch.setenv("OBS_BEADS_ISSUES_FILE", str(beads))
    monkeypatch.setenv("OBS_SCHEDULE_OWNERS_FILE", str(owners))
    return beads, owners


def _members():
    return {
        TEAM: {"agent_name": TEAM, "lineage": ["Root"]},
        "branch-agent": {"agent_name": "branch-agent", "lineage": ["Root", "Branch"]},
        "leaf-agent": {"agent_name": "leaf-agent", "lineage": ["Root", "Branch", "Leaf"]},
        "sibling-agent": {"agent_name": "sibling-agent", "lineage": ["Root", "Sibling"]},
    }


def _remaining(bot):
    return set(bot._topic_schedules_by_id)


class TestParseScopeArgs:
    def test_scopes_flags_and_mentions(self):
        assert schedule_scope.parse_scope_args([]).scope is None
        assert schedule_scope.parse_scope_args(["tree"]).scope == "tree"
        assert schedule_scope.parse_scope_args(["ALL@obs_bot"]).scope == "all"
        parsed = schedule_scope.parse_scope_args(["branch", "--include-protected"])
        assert parsed.scope == "branch" and parsed.include_protected
        assert schedule_scope.parse_scope_args(["tree", "\u2014include-protected"]).include_protected
        assert schedule_scope.parse_scope_args(["abc123"]).rest == ("abc123",)
        assert schedule_scope.parse_scope_args(["abc123"]).scope is None

    def test_branch_rule(self):
        assert schedule_scope.lineage_in_branch(("R", "B", "L"), ("R", "B"))
        assert not schedule_scope.lineage_in_branch(("R", "S"), ("R", "B"))
        assert not schedule_scope.lineage_in_branch(("R",), ())


class TestScopedUnschedule:
    async def test_branch_removes_self_and_idle_descendants_only(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-root", root.route)
        _sched(bot, "s-branch", branch.route)
        _sched(bot, "s-leaf", leaf.route, "monthly strategic tier")
        _sched(bot, "s-sibling", sibling.route)
        _sched(bot, "s-other", other.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["branch"]
            await bot.handle_unschedule(_make_update("/unschedule branch", thread_id=101), ctx)
        assert _remaining(bot) == {"s-root", "s-sibling", "s-other"}
        text = " ".join(c.kwargs["text"] for c in ctx.bot.send_message.call_args_list)
        assert "removed 2 schedule(s)" in text and "s-leaf" in text

    async def test_tree_from_leaf_covers_whole_tree_not_other_trees(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        for sid, st in (("s-root", root), ("s-branch", branch), ("s-leaf", leaf), ("s-sibling", sibling), ("s-other", other)):
            _sched(bot, sid, st.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree", thread_id=102), ctx)
        assert _remaining(bot) == {"s-other"}

    async def test_tree_spares_protected_unless_included(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-resp", leaf.route, "[ops-2] Reflection watcher")  # responsibility -> protected
        _sched(bot, "s-mission", root.route, "[ops-1] mission heartbeat")  # this tree's own work
        _sched(bot, "s-adopted", sibling.route, "no prefix, adopted by other tree")  # other-team owner
        _sched(bot, "s-closed", branch.route, "[old-1] closed responsibility")  # orphan -> removed
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree", thread_id=101), ctx)
            assert _remaining(bot) == {"s-resp", "s-adopted"}
            text = ctx.bot.send_message.call_args.kwargs["text"]
            assert "kept 2 protected" in text and "--include-protected" in text
            ctx = _make_context()
            ctx.args = ["tree", "--include-protected"]
            await bot.handle_unschedule(_make_update("/unschedule tree --include-protected", thread_id=101), ctx)
        assert _remaining(bot) == set()

    async def test_all_spares_only_responsibilities(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-resp", leaf.route, "[ops-2] watcher")
        _sched(bot, "s-adopted", sibling.route)
        _sched(bot, "s-other", other.route)
        _sched(bot, "s-root", root.route)
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_unschedule(_make_update("/unschedule all", thread_id=1), ctx)
        assert _remaining(bot) == {"s-resp"}
        text = ctx.bot.send_message.call_args.kwargs["text"]
        assert f"- {OTHER_TEAM}: 1" in text and f"- {TEAM}: 2" in text

    async def test_unreadable_owner_data_degrades_to_report(self, config, monkeypatch, tmp_path):
        bad = tmp_path / "owners.json"
        bad.write_text("{not json")
        monkeypatch.setenv("OBS_SCHEDULE_OWNERS_FILE", str(bad))
        monkeypatch.setenv("OBS_BEADS_ISSUES_FILE", str(tmp_path / "missing.jsonl"))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, *_ = _tree(bot)
        _sched(bot, "s-root", root.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree"), ctx)
        assert _remaining(bot) == set()
        assert "owner data incomplete" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_single_id_with_scope_words_still_works(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=1)
        assert bot._get_state(route) is not None
        _sched(bot, "abc12345", route)
        ctx = _make_context()
        ctx.args = ["abc12345"]
        await bot.handle_unschedule(_make_update("/unschedule abc12345", thread_id=1), ctx)
        assert _remaining(bot) == set()


class TestStopAllEveryTree:
    async def test_stop_all_interrupts_active_routes_in_every_chat(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        here = _state(bot, thread_id=1)
        elsewhere = _state(bot, chat_id=22222, thread_id=5)
        idle = _state(bot, chat_id=33333, thread_id=9)
        for st in (here, elsewhere):
            client = MagicMock()
            client.interrupt = AsyncMock()
            st.session_manager._client = client
            st.session_manager._connected = True
        elsewhere.busy = True
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_stop(_make_update("/stop all", thread_id=1), ctx)
        here.session_manager._client.interrupt.assert_awaited_once()
        elsewhere.session_manager._client.interrupt.assert_awaited_once()
        assert elsewhere.hook_state.interrupt_flag is True
        assert idle.hook_state.interrupt_flag is False  # idle disconnected routes untouched
        text = ctx.bot.send_message.call_args.kwargs["text"]
        assert "every tree" in text and "interrupted 2" in text and "/unschedule all" in text

    async def test_stop_all_marks_child_tasks_stopped_not_resumed(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child = _state(bot, chat_id=22222, thread_id=5)
        child.busy = True
        client = MagicMock()
        client.interrupt = AsyncMock()
        child.session_manager._client = client
        child.session_manager._connected = True
        with patch.object(bot, "_mark_child_task_terminal_request") as mark:
            ctx = _make_context()
            ctx.args = ["all"]
            await bot.handle_stop(_make_update("/stop all", thread_id=1), ctx)
        assert any(call.args == (child.route, "stopped") for call in mark.call_args_list)
        assert child.hook_state.stop_requested_at is not None
