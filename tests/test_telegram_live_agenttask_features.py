"""Live smoke tests for AgentTask improvement features.

Tests 3, 4, 5, 6, 7: Claude-only tests (no CLIProxyAPI needed).
Tests 1, 2, 8, 9, 10, 11: see Subtask 2 (requires CLIProxyAPI).

These tests use the TelegramForumPlatform harness with Haiku agents
for deterministic, parseable output.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.test_telegram_live_forum_topics import (
    _extract_topic_link,
    _send_and_wait_for_token,
    _wait_for_message_after_containing,
    _wait_for_message_containing,
    _LiveForumHarness,
    live_tg_forum,  # fixture import  # noqa: F401
)
from tests.test_telegram_live_smoke import (
    _wait_for_message_after_any_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _launch_agent_and_get_child_thread(
    harness: _LiveForumHarness,
    *,
    parent_thread_id: int,
    fork: bool,
    prompt: str,
    launch_token: str,
    extra_params: str = "",
    timeout: float = 240.0,
) -> int:
    """Launch an AgentTask and return the child thread ID."""
    baseline = await harness.platform.latest_bot_message_id(thread_id=parent_thread_id)
    fork_str = "true" if fork else "false"
    param_block = f"fork={fork_str}"
    if extra_params:
        param_block += f", {extra_params}"

    await harness.platform.send(
        (
            "This is a deterministic feature smoke test. "
            f"Use AgentTask exactly once with {param_block}, and prompt "
            f"'{prompt}' "
            f"After launching, reply with only {launch_token}."
        ),
        thread_id=parent_thread_id,
        require_done=False,
        timeout=timeout,
    )
    # Wait for the service message confirming launch
    launch_message = await _wait_for_message_after_containing(
        harness,
        thread_id=parent_thread_id,
        after_message_id=baseline,
        token="task launched",
        timeout=timeout + 60.0,
    )
    child_thread_id, _ = _extract_topic_link(launch_message.text)
    return child_thread_id


async def _launch_agent_and_expect_error(
    harness: _LiveForumHarness,
    *,
    parent_thread_id: int,
    instruction: str,
    ok_token: str,
    fail_token: str,
    timeout: float = 240.0,
) -> str:
    """Launch an AgentTask that may fail, return the matching token."""
    baseline = await harness.platform.latest_bot_message_id(thread_id=parent_thread_id)
    await harness.platform.send(
        instruction,
        thread_id=parent_thread_id,
        require_done=False,
        timeout=timeout,
    )
    result = await _wait_for_message_after_any_token(
        harness,
        thread_id=parent_thread_id,
        after_message_id=baseline,
        tokens=[ok_token, fail_token],
        timeout=timeout + 120.0,
    )
    return result.text


# ---------------------------------------------------------------------------
# Test 3: Prompt-from-file
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestPromptFromFile:
    async def test_live_smoke_prompt_from_file(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke PromptFile {tag}")

        # Prime the agent
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only PF-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"PF-PRIME-{tag}",
            timeout=180.0,
        )

        # --- Phase 1: Happy path — prompt_file loads and becomes the prompt ---
        prompt_file = Path(f"/tmp/obs_test_prompt_{tag}.md")
        prompt_file.write_text(
            f"Reply with exactly PF-FILE-{tag}. Do not add any other text."
        )
        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f'Use AgentTask exactly once with fork=false and prompt_file="{prompt_file}". '
                    "Do NOT provide a prompt parameter — only prompt_file. "
                    f"After launching, reply with only PF-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_msg = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline,
                token="task launched",
                timeout=240.0,
            )
            child_thread_id, _ = _extract_topic_link(launch_msg.text)

            # Child should reply with the content from the file
            child_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=f"PF-FILE-{tag}",
                timeout=240.0,
            )
            assert f"PF-FILE-{tag}" in child_reply.text, (
                f"Child did not use prompt from file: {child_reply.text}"
            )
        finally:
            prompt_file.unlink(missing_ok=True)

        # --- Phase 2: File not found error ---
        result = await _launch_agent_and_expect_error(
            live_tg_forum,
            parent_thread_id=parent_thread_id,
            instruction=(
                "This is a deterministic smoke test. "
                f'Try to use AgentTask with fork=false and prompt_file="/tmp/nonexistent_{tag}.md". '
                "Do NOT provide a prompt parameter. "
                f"If the tool returns an error, reply with only PFNOTFOUND-FAIL-{tag}. "
                f"If it launched successfully, reply with only PFNOTFOUND-OK-{tag}."
            ),
            ok_token=f"PFNOTFOUND-OK-{tag}",
            fail_token=f"PFNOTFOUND-FAIL-{tag}",
            timeout=180.0,
        )
        assert f"PFNOTFOUND-FAIL-{tag}" in result, (
            f"Expected file-not-found error, got: {result}"
        )

        # --- Phase 3: Both prompt and prompt_file — rejected ---
        prompt_file2 = Path(f"/tmp/obs_test_prompt2_{tag}.md")
        prompt_file2.write_text("dummy")
        try:
            result = await _launch_agent_and_expect_error(
                live_tg_forum,
                parent_thread_id=parent_thread_id,
                instruction=(
                    "This is a deterministic smoke test. "
                    f'Try to use AgentTask with fork=false, prompt="hello", '
                    f'and prompt_file="{prompt_file2}". '
                    f"If the tool returns an error, reply with only PFBOTH-FAIL-{tag}. "
                    f"If it launched successfully, reply with only PFBOTH-OK-{tag}."
                ),
                ok_token=f"PFBOTH-OK-{tag}",
                fail_token=f"PFBOTH-FAIL-{tag}",
                timeout=180.0,
            )
            assert f"PFBOTH-FAIL-{tag}" in result, (
                f"Expected mutual exclusion error, got: {result}"
            )
        finally:
            prompt_file2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 4: Python hooks basic
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestPythonHooksBasic:
    async def test_live_smoke_python_hooks_basic(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Hooks {tag}")

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only HOOK-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"HOOK-PRIME-{tag}",
            timeout=180.0,
        )

        # Create hook files
        good_hook = Path(f"/tmp/obs_test_hook_good_{tag}.py")
        good_hook.write_text(
            "from pathlib import Path\n"
            f'MARKER = "/tmp/obs_hook_called_{tag}.txt"\n'
            "def allow_all(hook_input, tool_use_id, context):\n"
            '    Path(MARKER).write_text(f"hook_called|tool={hook_input.get(\'tool_name\', \'unknown\')}")\n'
            "    return None\n"
        )

        crash_hook = Path(f"/tmp/obs_test_hook_crash_{tag}.py")
        crash_hook.write_text(
            "def crasher(hook_input, tool_use_id, context):\n"
            '    raise RuntimeError("Intentional crash for testing")\n'
        )

        marker_file = Path(f"/tmp/obs_hook_called_{tag}.txt")
        hooks_json = json.dumps({"PreToolUse": f"{good_hook}::allow_all"})

        try:
            # --- Phase 1: Good hook, verify it's called ---
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f"Use AgentTask exactly once with fork=false and "
                    f"hooks='{hooks_json}', "
                    f"and prompt 'Use Bash to run echo hello. "
                    f"Then reply with exactly HOOK-GOOD-{tag}.' "
                    f"After launching, reply with only HOOK-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_msg = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline,
                token="task launched",
                timeout=240.0,
            )
            child_thread_id, _ = _extract_topic_link(launch_msg.text)

            child_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=f"HOOK-GOOD-{tag}",
                timeout=240.0,
            )
            assert f"HOOK-GOOD-{tag}" in child_reply.text
            # Verify the hook was actually called
            assert marker_file.exists(), (
                f"Hook marker file not found at {marker_file} — hook was not invoked"
            )
            marker_content = marker_file.read_text()
            assert "hook_called" in marker_content

            # --- Phase 2: Crashing hook doesn't kill session ---
            crash_hooks_json = json.dumps({"PreToolUse": f"{crash_hook}::crasher"})
            marker_file.unlink(missing_ok=True)

            baseline2 = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f"Use AgentTask exactly once with fork=false and "
                    f"hooks='{crash_hooks_json}', "
                    f"and prompt 'Use Bash to run echo survived. "
                    f"Then reply with exactly HOOK-CRASH-{tag}.' "
                    f"After launching, reply with only HOOK-CRASH-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_msg2 = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline2,
                token="task launched",
                timeout=240.0,
            )
            crash_child_thread, _ = _extract_topic_link(launch_msg2.text)

            # Session should survive — the hook crash is swallowed
            crash_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=crash_child_thread,
                token=f"HOOK-CRASH-{tag}",
                timeout=240.0,
            )
            assert f"HOOK-CRASH-{tag}" in crash_reply.text, (
                "Session crashed despite hook error — stability violation"
            )

            # --- Phase 3: Hook file not found — error at launch ---
            result = await _launch_agent_and_expect_error(
                live_tg_forum,
                parent_thread_id=parent_thread_id,
                instruction=(
                    "This is a deterministic smoke test. "
                    f'Try to use AgentTask with fork=false and '
                    f'hooks=\'{json.dumps({"PreToolUse": f"/tmp/nonexistent_{tag}.py::func"})}\', '
                    f"and prompt 'reply hello'. "
                    f"If the tool returns an error, reply with only HOOKNOTFOUND-FAIL-{tag}. "
                    f"If it launched successfully, reply with only HOOKNOTFOUND-OK-{tag}."
                ),
                ok_token=f"HOOKNOTFOUND-OK-{tag}",
                fail_token=f"HOOKNOTFOUND-FAIL-{tag}",
                timeout=180.0,
            )
            # Note: hooks are best-effort — file-not-found may be logged and skipped
            # rather than blocking launch. Either outcome is acceptable for stability.

        finally:
            good_hook.unlink(missing_ok=True)
            crash_hook.unlink(missing_ok=True)
            marker_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 5: Hooks with agent spawning (evaluator pattern)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestHooksWithAgentSpawning:
    async def test_live_smoke_hooks_with_agent_spawning(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke HookAgent {tag}")

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only HOOKAGENT-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"HOOKAGENT-PRIME-{tag}",
            timeout=180.0,
        )

        # Create evaluator hook that spawns an agent via context["obs"]["launch_agent"]
        evaluator_hook = Path(f"/tmp/obs_test_hook_evaluator_{tag}.py")
        evaluator_hook.write_text(
            "import asyncio\n"
            "from pathlib import Path\n"
            "\n"
            "async def evaluator_guard(hook_input, tool_use_id, context):\n"
            '    tool_name = hook_input.get("tool_name", "")\n'
            '    if tool_name != "PlaceholderTool":\n'
            "        return None\n"
            '    obs = context.get("obs", {})\n'
            '    launch_agent = obs.get("launch_agent")\n'
            "    if not launch_agent:\n"
            "        return None\n"
            f'    Path("/tmp/obs_hook_evaluator_reached_{tag}.txt").write_text("reached")\n'
            "    result = await launch_agent({\n"
            f'        "prompt": "Write the word APPROVED to /tmp/obs_hook_verdict_{tag}.txt using Bash. '
            f'Then reply with EVAL-DONE.",\n'
            '        "description": "Evaluator Agent",\n'
            '        "fork": "false",\n'
            "    })\n"
            f'    verdict_path = Path("/tmp/obs_hook_verdict_{tag}.txt")\n'
            "    for _ in range(60):\n"
            "        await asyncio.sleep(1)\n"
            "        if verdict_path.exists():\n"
            '            verdict = verdict_path.read_text().strip()\n'
            '            if "APPROVED" in verdict:\n'
            "                return None\n"
            "    return None\n"
        )

        reached_marker = Path(f"/tmp/obs_hook_evaluator_reached_{tag}.txt")
        verdict_file = Path(f"/tmp/obs_hook_verdict_{tag}.txt")
        hooks_json = json.dumps({"PreToolUse": f"{evaluator_hook}::evaluator_guard"})

        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f"Use AgentTask exactly once with fork=false and "
                    f"hooks='{hooks_json}', "
                    f"and prompt 'Call PlaceholderTool with action=evaluate and input=test. "
                    f"Then reply with exactly HOOKAGENT-RESULT-{tag}.' "
                    f"After launching, reply with only HOOKAGENT-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_msg = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline,
                token="task launched",
                timeout=240.0,
            )
            child_thread_id, _ = _extract_topic_link(launch_msg.text)

            # Wait for the child to complete (extended timeout for evaluator pattern)
            child_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=f"HOOKAGENT-RESULT-{tag}",
                timeout=360.0,
            )
            assert f"HOOKAGENT-RESULT-{tag}" in child_reply.text

            # Verify the hook was reached
            assert reached_marker.exists(), (
                "Evaluator hook was never reached — PlaceholderTool call did not trigger hook"
            )

            # Verify the evaluator agent wrote the verdict
            assert verdict_file.exists(), (
                "Evaluator agent did not write verdict — agent spawning from hook may have failed"
            )
            assert "APPROVED" in verdict_file.read_text()

        finally:
            evaluator_hook.unlink(missing_ok=True)
            reached_marker.unlink(missing_ok=True)
            verdict_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 6: Secondary features (inherit_schedules, temperature, env)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestSecondaryFeatures:
    async def test_live_smoke_secondary_features(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Secondary {tag}")

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only SEC-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"SEC-PRIME-{tag}",
            timeout=180.0,
        )

        # --- Phase 1: Env passthrough ---
        env_json = json.dumps({"OBS_TEST_MARKER": f"smoke-{tag}"})
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use AgentTask exactly once with fork=false and env='{env_json}', "
                f"and prompt 'Use Bash to run echo $OBS_TEST_MARKER. "
                f"Reply with exactly SEC-ENV-{tag} followed by a pipe and value=<the echo output>.' "
                f"After launching, reply with only SEC-ENV-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="task launched",
            timeout=240.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_msg.text)

        env_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"SEC-ENV-{tag}",
            timeout=240.0,
        )
        assert f"smoke-{tag}" in env_reply.text, (
            f"Env passthrough failed — expected 'smoke-{tag}' in output: {env_reply.text}"
        )

        # --- Phase 2: inherit_schedules=false ---
        # First create a schedule on the parent
        sched_baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                "Use CronCreate with schedule_mode=interval, interval_seconds=86400, "
                "cron='* * * * *', prompt='noop', description='test-schedule', "
                "reset_session=false, max_runs=1, from='', until='', inherit=none, "
                "run_mode=normal. "
                f"Reply with only SEC-SCHED-CREATED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=sched_baseline,
            token=f"SEC-SCHED-CREATED-{tag}",
            timeout=180.0,
        )

        # Launch child with inherit_schedules=false
        baseline2 = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                "Use AgentTask exactly once with fork=false and inherit_schedules=false, "
                f"and prompt 'Call CronList. Count the schedules. "
                f"Reply with exactly SEC-SCHED-{tag} followed by |count=<number of schedules>.' "
                f"After launching, reply with only SEC-SCHED-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_msg2 = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline2,
            token="task launched",
            timeout=240.0,
        )
        sched_child_thread, _ = _extract_topic_link(launch_msg2.text)

        sched_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=sched_child_thread,
            token=f"SEC-SCHED-{tag}",
            timeout=240.0,
        )
        assert "count=0" in sched_reply.text, (
            f"Schedule should NOT be inherited with inherit_schedules=false: {sched_reply.text}"
        )


# ---------------------------------------------------------------------------
# Test 7: ForkTask retirement
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestForkTaskRetirement:
    async def test_live_smoke_forktask_retirement(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke ForkTask {tag}")

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only FORK-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"FORK-PRIME-{tag}",
            timeout=180.0,
        )

        # --- Phase 1: ForkTask tool should not exist ---
        result = await _launch_agent_and_expect_error(
            live_tg_forum,
            parent_thread_id=parent_thread_id,
            instruction=(
                "This is a deterministic smoke test. "
                f"Try to call a tool named ForkTask with prompt='Reply with FORK-CHILD-{tag}'. "
                "If the tool does not exist or returns an error, "
                f"reply with only FORK-GONE-{tag}. "
                f"If it succeeds, reply with only FORK-EXISTS-{tag}."
            ),
            ok_token=f"FORK-EXISTS-{tag}",
            fail_token=f"FORK-GONE-{tag}",
            timeout=180.0,
        )
        assert f"FORK-GONE-{tag}" in result, (
            f"ForkTask should be retired but appears to still exist: {result}"
        )

        # --- Phase 2: AgentTask with fork=true works as replacement ---
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use AgentTask exactly once with fork=true and prompt "
                f"'Reply with exactly FORK-REPLACE-{tag}.' "
                f"After launching, reply with only FORK-REPLACED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="fork task launched",
            timeout=240.0,
        )
        child_thread_id, _ = _extract_topic_link(launch_msg.text)

        child_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=child_thread_id,
            token=f"FORK-REPLACE-{tag}",
            timeout=240.0,
        )
        assert f"FORK-REPLACE-{tag}" in child_reply.text, (
            "AgentTask fork=true does not work as ForkTask replacement"
        )


# ---------------------------------------------------------------------------
# Test 9 (partial): Regression spot-checks
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
class TestRegressionSpotChecks:
    async def test_live_smoke_regression_basic_fork_and_fresh(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        """Verify basic fork and fresh agent still work after all changes."""
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(f"Smoke Regression {tag}")

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only REG-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"REG-PRIME-{tag}",
            timeout=180.0,
        )

        # --- Fork ---
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use AgentTask exactly once with fork=true and prompt "
                f"'Reply with exactly REG-FORK-{tag}.' "
                f"After launching, reply with only REG-FORK-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="fork task launched",
            timeout=240.0,
        )
        fork_thread, _ = _extract_topic_link(launch_msg.text)
        fork_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=fork_thread,
            token=f"REG-FORK-{tag}",
            timeout=240.0,
        )
        assert f"REG-FORK-{tag}" in fork_reply.text

        # --- Fresh agent ---
        baseline2 = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f"Use AgentTask exactly once with fork=false and prompt "
                f"'Reply with exactly REG-FRESH-{tag}.' "
                f"After launching, reply with only REG-FRESH-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_msg2 = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline2,
            token="task launched",
            timeout=240.0,
        )
        fresh_thread, _ = _extract_topic_link(launch_msg2.text)
        fresh_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=fresh_thread,
            token=f"REG-FRESH-{tag}",
            timeout=240.0,
        )
        assert f"REG-FRESH-{tag}" in fresh_reply.text
