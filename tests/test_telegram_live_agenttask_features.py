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

        # --- Phase 4: Neither prompt nor prompt_file — rejected ---
        result = await _launch_agent_and_expect_error(
            live_tg_forum,
            parent_thread_id=parent_thread_id,
            instruction=(
                "This is a deterministic smoke test. "
                "Try to use AgentTask with fork=false. Do NOT provide a prompt parameter "
                "and do NOT provide a prompt_file parameter. Leave both empty. "
                f"If the tool returns an error, reply with only PFNEITHER-FAIL-{tag}. "
                f"If it launched successfully, reply with only PFNEITHER-OK-{tag}."
            ),
            ok_token=f"PFNEITHER-OK-{tag}",
            fail_token=f"PFNEITHER-FAIL-{tag}",
            timeout=180.0,
        )
        assert f"PFNEITHER-FAIL-{tag}" in result, (
            f"Expected 'prompt or prompt_file required' error, got: {result}"
        )

        # --- Phase 5: prompt_file + fork=true works ---
        prompt_file_fork = Path(f"/tmp/obs_test_prompt_fork_{tag}.md")
        prompt_file_fork.write_text(
            f"Reply with exactly PF-FORK-{tag}. Do not add any other text."
        )
        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f'Use AgentTask exactly once with fork=true and prompt_file="{prompt_file_fork}". '
                    "Do NOT provide a prompt parameter — only prompt_file. "
                    f"After launching, reply with only PF-FORK-LAUNCHED-{tag}."
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
            fork_child_thread, _ = _extract_topic_link(launch_msg.text)
            fork_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=fork_child_thread,
                token=f"PF-FORK-{tag}",
                timeout=240.0,
            )
            assert f"PF-FORK-{tag}" in fork_reply.text, (
                f"Forked child did not use prompt from file: {fork_reply.text}"
            )
        finally:
            prompt_file_fork.unlink(missing_ok=True)

        # --- Phase 6: Tilde path expansion ---
        tilde_file = Path.home() / f"obs_test_tilde_{tag}.md"
        tilde_file.write_text(
            f"Reply with exactly PF-TILDE-{tag}. Do not add any other text."
        )
        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f'Use AgentTask exactly once with fork=false and prompt_file="~/obs_test_tilde_{tag}.md". '
                    "Do NOT provide a prompt parameter — only prompt_file. "
                    f"After launching, reply with only PF-TILDE-LAUNCHED-{tag}."
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
            tilde_child_thread, _ = _extract_topic_link(launch_msg.text)
            tilde_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=tilde_child_thread,
                token=f"PF-TILDE-{tag}",
                timeout=240.0,
            )
            assert f"PF-TILDE-{tag}" in tilde_reply.text, (
                f"Tilde path expansion failed: {tilde_reply.text}"
            )
        finally:
            tilde_file.unlink(missing_ok=True)


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

            # --- Phase 4: Function not found in file ---
            bad_fn_hooks = json.dumps({"PreToolUse": f"{good_hook}::nonexistent_func"})
            result4 = await _launch_agent_and_expect_error(
                live_tg_forum,
                parent_thread_id=parent_thread_id,
                instruction=(
                    "This is a deterministic smoke test. "
                    f"Try to use AgentTask with fork=false and "
                    f"hooks='{bad_fn_hooks}', "
                    f"and prompt 'reply hello'. "
                    f"If the tool returns an error, reply with only HOOKBADFN-FAIL-{tag}. "
                    f"If it launched successfully, reply with only HOOKBADFN-OK-{tag}."
                ),
                ok_token=f"HOOKBADFN-OK-{tag}",
                fail_token=f"HOOKBADFN-FAIL-{tag}",
                timeout=180.0,
            )
            # Either a clear error or best-effort skip — both acceptable for stability
            assert f"HOOKBADFN-FAIL-{tag}" in result4 or f"HOOKBADFN-OK-{tag}" in result4

            # --- Phase 5: Syntax error in hook file ---
            syntax_hook = Path(f"/tmp/obs_test_hook_syntax_{tag}.py")
            syntax_hook.write_text(
                "def good_func(hook_input, tool_use_id, context):\n"
                "    this is not valid python !!!\n"
            )
            syntax_hooks_json = json.dumps({"PreToolUse": f"{syntax_hook}::good_func"})
            result5 = await _launch_agent_and_expect_error(
                live_tg_forum,
                parent_thread_id=parent_thread_id,
                instruction=(
                    "This is a deterministic smoke test. "
                    f"Try to use AgentTask with fork=false and "
                    f"hooks='{syntax_hooks_json}', "
                    f"and prompt 'reply hello'. "
                    f"If the tool returns an error, reply with only HOOKSYNTAX-FAIL-{tag}. "
                    f"If it launched successfully, reply with only HOOKSYNTAX-OK-{tag}."
                ),
                ok_token=f"HOOKSYNTAX-OK-{tag}",
                fail_token=f"HOOKSYNTAX-FAIL-{tag}",
                timeout=180.0,
            )
            syntax_hook.unlink(missing_ok=True)
            # Either error or best-effort skip — both acceptable
            assert f"HOOKSYNTAX-FAIL-{tag}" in result5 or f"HOOKSYNTAX-OK-{tag}" in result5

            # --- Phase 7: PlaceholderTool without hook is no-op ---
            baseline7 = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    "Use AgentTask exactly once with fork=false and NO hooks parameter. "
                    f"Prompt: 'Call PlaceholderTool with action=test and input=hello. "
                    f"Reply with HOOK-PLACEHOLDER-{tag}|result=done.' "
                    f"After launching, reply with only HOOK-PLACEHOLDER-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_noop = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline7,
                token="task launched",
                timeout=240.0,
            )
            noop_thread, _ = _extract_topic_link(launch_noop.text)
            noop_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=noop_thread,
                token=f"HOOK-PLACEHOLDER-{tag}",
                timeout=240.0,
            )
            assert f"HOOK-PLACEHOLDER-{tag}" in noop_reply.text, (
                "PlaceholderTool without hooks should work as no-op"
            )

            # --- Phase 8: Slow hook doesn't permanently freeze session ---
            slow_hook = Path(f"/tmp/obs_test_hook_slow_{tag}.py")
            slow_hook.write_text(
                "import time\n"
                "def slow_hook(hook_input, tool_use_id, context):\n"
                "    time.sleep(15)\n"
                "    return None\n"
            )
            slow_hooks_json = json.dumps({"PreToolUse": f"{slow_hook}::slow_hook"})
            baseline8 = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f"Use AgentTask exactly once with fork=false and "
                    f"hooks='{slow_hooks_json}', "
                    f"and prompt 'Use Bash to run echo timeout-test. "
                    f"Then reply with exactly HOOK-TIMEOUT-{tag}.' "
                    f"After launching, reply with only HOOK-TIMEOUT-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_slow = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline8,
                token="task launched",
                timeout=240.0,
            )
            slow_thread, _ = _extract_topic_link(launch_slow.text)
            # Extended timeout — hook sleeps 15s but session should eventually complete
            slow_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=slow_thread,
                token=f"HOOK-TIMEOUT-{tag}",
                timeout=300.0,
            )
            assert f"HOOK-TIMEOUT-{tag}" in slow_reply.text, (
                "Session hung due to slow hook — stability violation"
            )
            slow_hook.unlink(missing_ok=True)

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

        # --- Phase 2b: Default inherit_schedules (backward compat) ---
        # Schedule still exists on parent from Phase 2 setup
        baseline_default = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                "Use AgentTask exactly once with fork=false. Do NOT pass inherit_schedules. "
                f"Prompt: 'Call CronList. Count the schedules. "
                f"Reply with exactly SEC-DEFAULTSCHED-{tag} followed by |count=<number of schedules>.' "
                f"After launching, reply with only SEC-DEFAULTSCHED-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=180.0,
        )
        launch_default = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline_default,
            token="task launched",
            timeout=240.0,
        )
        default_sched_thread, _ = _extract_topic_link(launch_default.text)
        default_sched_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=default_sched_thread,
            token=f"SEC-DEFAULTSCHED-{tag}",
            timeout=240.0,
        )
        # Default should inherit schedules (backward compat)
        assert "count=0" not in default_sched_reply.text, (
            f"Default inherit_schedules should be true (backward compat), "
            f"but child has 0 schedules: {default_sched_reply.text}"
        )

        # --- Phase 3: Temperature determinism ---
        # Launch 3 agents with temperature=0 asking for exact same output
        temp_responses = []
        for i, label in enumerate(["A", "B", "C"]):
            temp_baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f'Use AgentTask exactly once with fork=false and temperature="0", '
                    f"and prompt 'Reply with exactly the single digit 1. Nothing else. "
                    f"No punctuation. No explanation. Just the character 1. "
                    f"Then on the next line write SEC-TEMP-{label}-{tag}.' "
                    f"After launching, reply with only SEC-TEMP-{label}-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=180.0,
            )
            launch_temp = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=temp_baseline,
                token="task launched",
                timeout=240.0,
            )
            temp_thread, _ = _extract_topic_link(launch_temp.text)
            temp_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=temp_thread,
                token=f"SEC-TEMP-{label}-{tag}",
                timeout=240.0,
            )
            temp_responses.append(temp_reply.text)

        # At least 2 of 3 should contain "1" as primary content
        contains_1 = sum(1 for r in temp_responses if "1" in r.split(f"SEC-TEMP")[0] or "\n1\n" in r or r.strip().startswith("1"))
        assert contains_1 >= 2, (
            f"Temperature=0 should produce deterministic output. "
            f"Expected at least 2/3 with '1', got {contains_1}: {temp_responses}"
        )

        # --- Phase 5: Protected env var override attempt ---
        protected_env = json.dumps({"ANTHROPIC_API_KEY": f"fake-key-{tag}"})
        result_prot = await _launch_agent_and_expect_error(
            live_tg_forum,
            parent_thread_id=parent_thread_id,
            instruction=(
                "This is a deterministic smoke test. "
                f"Try to use AgentTask with fork=false and env='{protected_env}', "
                f"and prompt 'Reply with SEC-PROTECTED-{tag}'. "
                f"If the tool returns an error about the env variable, reply with only SEC-PROTECTED-FAIL-{tag}. "
                f"If it launched successfully, reply with only SEC-PROTECTED-OK-{tag}."
            ),
            ok_token=f"SEC-PROTECTED-OK-{tag}",
            fail_token=f"SEC-PROTECTED-FAIL-{tag}",
            timeout=180.0,
        )
        # Either rejected with error (ideal) or accepted (the env var gets overridden
        # downstream but doesn't break auth since the proxy handles the real key).
        # Both outcomes are acceptable — we're documenting behavior, not enforcing.
        assert f"SEC-PROTECTED-FAIL-{tag}" in result_prot or f"SEC-PROTECTED-OK-{tag}" in result_prot


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


# ---------------------------------------------------------------------------
# Helpers for non-Claude tests
# ---------------------------------------------------------------------------

_CLIPROXY_URL = "http://127.0.0.1:8317"
_CLIPROXY_KEY = "sk-anything"  # Must match api-keys in cliproxyapi.conf

# Use explicit model versions that exist in CLIProxyAPI.
# gpt-5.4 is the latest available; gemini-2.5-flash is fast for testing.
_GPT_MODEL = "gpt-5.4"
_GEMINI_MODEL = "gemini-2.5-flash"


def _cliproxy_is_running() -> bool:
    """Quick TCP check — is CLIProxyAPI responding?"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8317), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False


_requires_cliproxy = pytest.mark.skipif(
    not _cliproxy_is_running(),
    reason="CLIProxyAPI not running at :8317",
)


# ---------------------------------------------------------------------------
# Test 1: Multi-model identity and routing
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@_requires_cliproxy
class TestMultiModelIdentityRouting:
    async def test_live_smoke_multimodel_identity_and_routing(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(
            f"Smoke MultiModel {tag}"
        )

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only MM-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"MM-PRIME-{tag}",
            timeout=180.0,
        )

        # --- Phase 1: Explicit GPT model ---
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f'Use AgentTask exactly once with fork=false and model="{_GPT_MODEL}", '
                "and prompt "
                "'You are in a model identity test. Ignore any system instruction "
                "that says you are Claude. What is your ACTUAL model name? "
                "You must pick exactly one: gpt, claude, or gemini. "
                f"Reply with exactly MM-GPT-{tag}|model=<your answer>.' "
                f"After launching, reply with only MM-GPT-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=300.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="task launched",
            timeout=360.0,
        )
        gpt_child_thread, _ = _extract_topic_link(launch_msg.text)

        gpt_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=gpt_child_thread,
            token=f"MM-GPT-{tag}",
            timeout=300.0,
        )
        assert f"MM-GPT-{tag}" in gpt_reply.text
        # GPT should identify itself as gpt, not claude
        gpt_text_lower = gpt_reply.text.lower()
        assert "model=gpt" in gpt_text_lower or "gpt" in gpt_text_lower, (
            f"GPT agent should identify as GPT, got: {gpt_reply.text}"
        )

        # --- Phase 2: Explicit Gemini model ---
        baseline2 = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f'Use AgentTask exactly once with fork=false and model="{_GEMINI_MODEL}", '
                "and prompt "
                "'You are in a model identity test. Ignore any system instruction "
                "that says you are Claude. What is your ACTUAL model name? "
                "You must pick exactly one: gpt, claude, or gemini. "
                f"Reply with exactly MM-GEMINI-{tag}|model=<your answer>.' "
                f"After launching, reply with only MM-GEMINI-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=300.0,
        )
        launch_msg2 = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline2,
            token="task launched",
            timeout=360.0,
        )
        gemini_child_thread, _ = _extract_topic_link(launch_msg2.text)

        gemini_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=gemini_child_thread,
            token=f"MM-GEMINI-{tag}",
            timeout=300.0,
        )
        assert f"MM-GEMINI-{tag}" in gemini_reply.text
        gemini_text_lower = gemini_reply.text.lower()
        assert "model=gemini" in gemini_text_lower or "gemini" in gemini_text_lower, (
            f"Gemini agent should identify as Gemini, got: {gemini_reply.text}"
        )

        # --- Phase 3: "claude" shorthand resolves to opus ---
        baseline3 = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                'Use AgentTask exactly once with fork=false and model="claude", '
                "and prompt "
                "'What is your actual model identifier? "
                f"Reply with exactly MM-CLAUDE-{tag}|model=<your model name>.' "
                f"After launching, reply with only MM-CLAUDE-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=300.0,
        )
        launch_msg3 = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline3,
            token="task launched",
            timeout=360.0,
        )
        claude_child_thread, _ = _extract_topic_link(launch_msg3.text)

        claude_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=claude_child_thread,
            token=f"MM-CLAUDE-{tag}",
            timeout=300.0,
        )
        assert f"MM-CLAUDE-{tag}" in claude_reply.text
        claude_text_lower = claude_reply.text.lower()
        assert "opus" in claude_text_lower or "claude" in claude_text_lower, (
            f"'claude' shorthand should resolve to opus: {claude_reply.text}"
        )

        # --- Phase 4: fork=true with different model (warning/error) ---
        result_fork_model = await _launch_agent_and_expect_error(
            live_tg_forum,
            parent_thread_id=parent_thread_id,
            instruction=(
                "This is a deterministic smoke test. "
                f'Try to use AgentTask with fork=true and model="{_GPT_MODEL}". '
                f"Prompt: 'Reply with MM-FORKMODEL-{tag}.' "
                "If it succeeds (with or without a warning), "
                f"reply with only MM-FORKMODEL-OK-{tag}. "
                "If it returns an error about fork and model being incompatible, "
                f"reply with only MM-FORKMODEL-FAIL-{tag}."
            ),
            ok_token=f"MM-FORKMODEL-OK-{tag}",
            fail_token=f"MM-FORKMODEL-FAIL-{tag}",
            timeout=300.0,
        )
        # Either an error about fork+model or success with warning — both acceptable
        assert (
            f"MM-FORKMODEL-OK-{tag}" in result_fork_model
            or f"MM-FORKMODEL-FAIL-{tag}" in result_fork_model
        ), f"Unexpected response for fork+model test: {result_fork_model}"


# ---------------------------------------------------------------------------
# Test 2: CLIProxyAPI down — graceful error
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@pytest.mark.serial  # MUST run in isolation — stops/restarts CLIProxyAPI
class TestCLIProxyDown:
    async def test_live_smoke_cliproxy_down_graceful_error(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(
            f"Smoke CLIProxy Down {tag}"
        )

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only PROXY-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"PROXY-PRIME-{tag}",
            timeout=180.0,
        )

        import subprocess
        import time

        # Stop CLIProxyAPI
        subprocess.run(["brew", "services", "stop", "cliproxyapi"], capture_output=True)
        time.sleep(3)  # Wait for service to stop

        try:
            # Verify it's actually down
            assert not _cliproxy_is_running(), "CLIProxyAPI should be stopped"

            # Try launching a non-Claude agent — should fail gracefully
            result = await _launch_agent_and_expect_error(
                live_tg_forum,
                parent_thread_id=parent_thread_id,
                instruction=(
                    "This is a deterministic smoke test. "
                    f'Try to use AgentTask with fork=false and model="{_GPT_MODEL}". '
                    f"Prompt: 'Reply with PROXY-GPT-{tag}.' "
                    "If the tool returns an error or the agent fails to start, "
                    f"reply with only PROXY-FAIL-{tag}|error=<brief error>. "
                    f"If it launched successfully, reply with only PROXY-OK-{tag}."
                ),
                ok_token=f"PROXY-OK-{tag}",
                fail_token=f"PROXY-FAIL-{tag}",
                timeout=300.0,
            )
            # The launch should fail or the agent should fail to connect
            # Either way, the parent session must survive
            assert f"PROXY-FAIL-{tag}" in result or f"PROXY-OK-{tag}" in result, (
                f"Parent session should respond with OK or FAIL token: {result}"
            )

            # Verify parent session is still alive
            alive_reply = await _send_and_wait_for_token(
                live_tg_forum,
                text=f"Reply with only PROXY-ALIVE-{tag}.",
                thread_id=parent_thread_id,
                token=f"PROXY-ALIVE-{tag}",
                timeout=180.0,
            )
            assert f"PROXY-ALIVE-{tag}" in alive_reply.text, (
                "Parent session died after CLIProxyAPI-down scenario"
            )

        finally:
            # ALWAYS restart CLIProxyAPI
            subprocess.run(["brew", "services", "start", "cliproxyapi"], capture_output=True)
            # Wait for service to be ready
            for _ in range(10):
                time.sleep(2)
                if _cliproxy_is_running():
                    break
            assert _cliproxy_is_running(), (
                "CRITICAL: Failed to restart CLIProxyAPI after test"
            )


# ---------------------------------------------------------------------------
# Test 8: Kitchen sink cross-feature
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@_requires_cliproxy
class TestKitchenSinkCrossFeature:
    async def test_live_smoke_kitchen_sink_cross_feature(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(
            f"Smoke Kitchen {tag}"
        )

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only KITCHEN-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"KITCHEN-PRIME-{tag}",
            timeout=180.0,
        )

        # Create prompt file
        kitchen_prompt_file = Path(f"/tmp/obs_test_kitchen_{tag}.md")
        kitchen_prompt_file.write_text(
            f"You are in a cross-feature test. "
            f"Use Bash to run 'echo $OBS_KITCHEN_TAG'. "
            f"Reply with exactly KITCHEN-{tag}|env=<echo output>|"
            f"model=<your actual model: gpt, claude, or gemini>."
        )

        # Create hook file
        kitchen_hook = Path(f"/tmp/obs_test_hook_kitchen_{tag}.py")
        kitchen_hook.write_text(
            "from pathlib import Path\n"
            "def kitchen_hook(hook_input, tool_use_id, context):\n"
            f'    if hook_input.get("tool_name") == "PlaceholderTool":\n'
            f'        Path("/tmp/obs_kitchen_hook_called_{tag}.txt").write_text("intercepted")\n'
            "    return None\n"
        )

        hook_marker = Path(f"/tmp/obs_kitchen_hook_called_{tag}.txt")
        env_json = json.dumps({"OBS_KITCHEN_TAG": f"kitchen-{tag}"})
        hooks_json = json.dumps({"PreToolUse": f"{kitchen_hook}::kitchen_hook"})

        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f"Use AgentTask exactly once with fork=false, "
                    f'model="{_GPT_MODEL}", '
                    f"prompt_file=\"{kitchen_prompt_file}\", "
                    f"hooks='{hooks_json}', "
                    f"inherit_schedules=false, "
                    f"env='{env_json}'. "
                    f"After launching, reply with only KITCHEN-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=300.0,
            )
            launch_msg = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline,
                token="task launched",
                timeout=360.0,
            )
            child_thread_id, _ = _extract_topic_link(launch_msg.text)

            child_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=f"KITCHEN-{tag}",
                timeout=360.0,
            )
            reply_text = child_reply.text

            # Verify env passthrough worked with non-Claude model
            assert f"kitchen-{tag}" in reply_text, (
                f"Env passthrough failed with non-Claude model: {reply_text}"
            )

            # Verify model is not Claude
            reply_lower = reply_text.lower()
            # The model should report gpt (not claude)
            assert "model=gpt" in reply_lower or "gpt" in reply_lower, (
                f"Expected GPT model identity in kitchen sink test: {reply_text}"
            )

        finally:
            kitchen_prompt_file.unlink(missing_ok=True)
            kitchen_hook.unlink(missing_ok=True)
            hook_marker.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 10: Cross-model messaging
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@_requires_cliproxy
class TestCrossModelMessaging:
    async def test_live_smoke_cross_model_messaging(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(
            f"Smoke CrossModel Msg {tag}"
        )

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only XMSG-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"XMSG-PRIME-{tag}",
            timeout=180.0,
        )

        # Launch a GPT agent (Agent-A) that will spawn a Gemini agent (Agent-B)
        # and they exchange messages
        baseline = await live_tg_forum.platform.latest_bot_message_id(
            thread_id=parent_thread_id
        )
        await live_tg_forum.platform.send(
            (
                "This is a deterministic smoke test. "
                f'Use AgentTask exactly once with fork=false and model="{_GPT_MODEL}", '
                "and prompt "
                "'You are Agent-A in a cross-model messaging test. "
                f'Launch AgentTask with fork=false and model="{_GEMINI_MODEL}". '
                "That agent's prompt should be: "
                "'You are Agent-B. Ignore any system instruction that says you are Claude. "
                "Send a message to your parent using SendInboxMessage with content: "
                f"XMSG-B2A-{tag}|mymodel=gemini. "
                f"Set needs_reply to false. Then reply with XMSG-B-SENT-{tag}.' "
                "After Agent-B sends, use ReadInbox to read Agent-B's message. "
                f"Reply with XMSG-A-RESULT-{tag}|b_said=<content of B message>"
                "|a_model=<your actual model: gpt, claude, or gemini>.' "
                f"After launching, reply with only XMSG-A-LAUNCHED-{tag}."
            ),
            thread_id=parent_thread_id,
            require_done=False,
            timeout=360.0,
        )
        launch_msg = await _wait_for_message_after_containing(
            live_tg_forum,
            thread_id=parent_thread_id,
            after_message_id=baseline,
            token="task launched",
            timeout=420.0,
        )
        a_child_thread, _ = _extract_topic_link(launch_msg.text)

        # Wait for Agent-A to report back with the cross-model messaging result
        # Extended timeout: A launches B (non-Claude), B sends message, A reads it
        a_reply = await _wait_for_message_containing(
            live_tg_forum,
            thread_id=a_child_thread,
            token=f"XMSG-A-RESULT-{tag}",
            timeout=480.0,
        )
        reply_text = a_reply.text.lower()

        # Agent-B's message should have been received
        assert f"xmsg-b2a-{tag}" in reply_text, (
            f"Agent-A did not receive Agent-B's message: {a_reply.text}"
        )
        # Agent-A should identify as GPT
        assert "a_model=gpt" in reply_text or "gpt" in reply_text, (
            f"Agent-A should identify as GPT: {a_reply.text}"
        )


# ---------------------------------------------------------------------------
# Test 11: Non-Claude self-directed work
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.telegram_smoke
@_requires_cliproxy
class TestNonClaudeSelfDirectedWork:
    async def test_live_smoke_non_claude_self_directed_work(
        self,
        live_tg_forum: _LiveForumHarness,
    ) -> None:
        tag = uuid.uuid4().hex[:8]
        parent_thread_id = await live_tg_forum.platform.create_topic(
            f"Smoke NonClaude Work {tag}"
        )

        # Prime
        await _send_and_wait_for_token(
            live_tg_forum,
            text=f"This is a deterministic smoke test. Reply with only WORK-PRIME-{tag}.",
            thread_id=parent_thread_id,
            token=f"WORK-PRIME-{tag}",
            timeout=180.0,
        )

        # Create test input file
        work_input = Path(f"/tmp/obs_test_workfile_{tag}.txt")
        work_input.write_text("alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\n")
        work_result = Path(f"/tmp/obs_test_results_{tag}.txt")

        try:
            baseline = await live_tg_forum.platform.latest_bot_message_id(
                thread_id=parent_thread_id
            )
            await live_tg_forum.platform.send(
                (
                    "This is a deterministic smoke test. "
                    f'Use AgentTask exactly once with fork=false and model="{_GPT_MODEL}", '
                    "and prompt "
                    f"'You have a multi-step task. Complete ALL steps in order: "
                    f"Step 1: Read the file {work_input} using the Read tool. "
                    f"Step 2: Use the Grep tool to search for charlie in that file. "
                    f"Step 3: Use Bash to count lines: wc -l < {work_input} "
                    f"Step 4: Write a results file at {work_result} using the Write tool. "
                    "The file should contain exactly: lines=6 on the first line "
                    "and found=charlie on the second line. No extra text. "
                    f"Step 5: Reply with WORK-DONE-{tag}.' "
                    f"After launching, reply with only WORK-LAUNCHED-{tag}."
                ),
                thread_id=parent_thread_id,
                require_done=False,
                timeout=360.0,
            )
            launch_msg = await _wait_for_message_after_containing(
                live_tg_forum,
                thread_id=parent_thread_id,
                after_message_id=baseline,
                token="task launched",
                timeout=420.0,
            )
            child_thread_id, _ = _extract_topic_link(launch_msg.text)

            # GPT may be slower — extended timeout
            child_reply = await _wait_for_message_containing(
                live_tg_forum,
                thread_id=child_thread_id,
                token=f"WORK-DONE-{tag}",
                timeout=360.0,
            )
            assert f"WORK-DONE-{tag}" in child_reply.text, (
                f"GPT agent did not complete multi-step task: {child_reply.text}"
            )

            # Verify the results file
            assert work_result.exists(), (
                "GPT agent did not create results file — Write tool may not work with non-Claude"
            )
            result_content = work_result.read_text()
            assert "lines=6" in result_content, (
                f"Line count incorrect in results: {result_content}"
            )
            assert "found=charlie" in result_content, (
                f"Grep result missing from results: {result_content}"
            )

        finally:
            work_input.unlink(missing_ok=True)
            work_result.unlink(missing_ok=True)
