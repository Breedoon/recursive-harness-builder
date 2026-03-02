"""Tests for deterministic eval lane helpers."""

from __future__ import annotations

import asyncio

import pytest

from tests.evals.deterministic import _validate_scenario, run_deterministic
from tests.evals.scenario import EvalScenario, EvalStep


class _FakePlatform:
    def __init__(self, send_outputs: list[str] | None = None, wait_outputs: list[str] | None = None):
        self._send_outputs = list(send_outputs or [])
        self._wait_outputs = list(wait_outputs or [])
        self.wait_timeouts: list[int] = []

    async def send(self, text: str) -> str:
        if self._send_outputs:
            return self._send_outputs.pop(0)
        return ""

    async def send_nowait(self, text: str) -> None:
        return None

    async def read(self) -> str:
        return ""

    async def wait_for_prompt(self, timeout: int = 120) -> str:
        self.wait_timeouts.append(timeout)
        if self._wait_outputs:
            return self._wait_outputs.pop(0)
        raise asyncio.TimeoutError

    async def close(self) -> None:
        return None


def test_validate_basic_chat_passes_with_helpful_response() -> None:
    passed, details = _validate_scenario(
        "basic_chat",
        ["I can help with Obsidian vault tasks and organize notes."],
        "",
    )
    assert passed
    assert details == "ok"


def test_validate_queue_message_fails_without_queue_markers() -> None:
    passed, details = _validate_scenario(
        "queue_message",
        ["Binary search is O(log n). Also 2+2 is 4."],
        "AGENT: Binary search is O(log n). Also 2+2 is 4.",
    )
    assert not passed
    assert "queued" in details


def test_validate_session_context_info_accepts_total_tokens_used() -> None:
    output = (
        "**Session ID**: `6dab2d19-e271-412b-af54-31ead5e23a88`\\n"
        "**Context Window**\\n"
        "- **Used**: ~24,825 tokens\\n"
        "- **Remaining**: ~87.6% of 200k\\n"
    )
    passed, details = _validate_scenario("session_context_info", ["ok", output], "")
    assert passed
    assert details == "ok"


def test_validate_session_context_info_accepts_parenthetical_tokens_of() -> None:
    output = (
        "- **Session ID**: `2f5f809e-8cc6-4674-8dde-8387b2d6ac33`\\n"
        "- **Context used**: ~10% (~19.8k of 200k tokens), **90.1% remaining**\\n"
    )
    passed, details = _validate_scenario("session_context_info", ["ok", output], "")
    assert passed
    assert details == "ok"


def test_validate_session_context_info_rejects_used_exceeding_window() -> None:
    output = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 312000\n"
        "estimated_context_remaining_pct: 0.0%\n"
    )
    passed, details = _validate_scenario("session_context_info", ["ok", output], "")
    assert not passed
    assert "exceeds window" in details


def test_validate_session_context_info_accepts_markdown_table_token_fields() -> None:
    output = (
        "| **Session ID** | `5a98046b-60f1-4983-bac8-8249a6437e56` |\n"
        "| **input_tokens** | 3 |\n"
        "| **output_tokens** | 6 |\n"
        "| **estimated_context_window_tokens** | 200000 |\n"
        "| **estimated_context_used_tokens** | 23314 |\n"
    )
    passed, details = _validate_scenario("session_context_info", ["ok", output], "")
    assert passed
    assert details == "ok"


def test_validate_session_context_non_cumulative_accepts_valid_report() -> None:
    output = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 37000\n"
        "estimated_context_remaining_pct: 81.5%\n"
    )
    passed, details = _validate_scenario("session_context_non_cumulative", ["a", "b", output], "")
    assert passed
    assert details == "ok"


def test_validate_session_context_info_rejects_explicit_tool_failure() -> None:
    output = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 37000\n"
        "context_info failed: timeout\n"
    )
    passed, details = _validate_scenario("session_context_info", ["ok", output], "")
    assert not passed
    assert "introspection tool failure" in details


def test_validate_session_context_tool_use_regression_rejects_sharp_drop() -> None:
    one = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 21000\n"
    )
    three = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 76000\n"
    )
    five = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 32000\n"
    )
    passed, details = _validate_scenario(
        "session_context_tool_use_regression",
        ["READY", one, "DONE_READS", three, "4", five],
        "",
    )
    assert not passed
    assert "dropped sharply" in details


def test_validate_session_context_tool_use_regression_accepts_stable_values() -> None:
    one = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 21000\n"
    )
    three = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 36000\n"
    )
    five = (
        "session_id: 5a98046b-60f1-4983-bac8-8249a6437e56\n"
        "estimated_context_window_tokens: 200000\n"
        "estimated_context_used_tokens: 35000\n"
    )
    passed, details = _validate_scenario(
        "session_context_tool_use_regression",
        ["READY", one, "DONE_READS", three, "4", five],
        "",
    )
    assert passed
    assert details == "ok"


def test_validate_tg_auth_guard_requires_paris() -> None:
    passed, details = _validate_scenario(
        "tg_auth_guard",
        ["Hello there\ncontext: 12k / 200k", "London\ncontext: 13k / 200k"],
        "",
    )
    assert not passed
    assert "Paris" in details


def test_validate_tg_message_split_checks_length_and_coverage() -> None:
    long_text = (
        " ".join(
            ["Babbage Lovelace Turing ENIAC transistor ARPANET World Wide Web smartphone machine learning"] * 60
        )
        + "\ncontext: 24k / 200k"
    )
    passed, details = _validate_scenario("tg_message_split", [long_text], "")
    assert passed
    assert details == "ok"


def test_validate_tg_transport_desync_detects_backlog_leak() -> None:
    passed, details = _validate_scenario(
        "tg_transport_desync_forced_concurrency",
        ["FORCED_STRESS_DONE\ncontext: 28k / 200k", "Read: 2019-12-24.md ... pong\ncontext: 29k / 200k"],
        "",
    )
    assert not passed
    assert "contaminated" in details


def test_validate_tg_queue_while_busy_requires_single_queued_run() -> None:
    passed, details = _validate_scenario(
        "tg_queue_while_busy",
        [
            "skills response only\ncontext: 20k / 200k",
            "4\ncontext: 21k / 200k",
        ],
        "",
    )
    assert not passed
    assert "separate turn" in details


def test_validate_tg_inbound_batching_rejects_queued_delivery() -> None:
    passed, details = _validate_scenario(
        "tg_inbound_batching",
        [
            "BATCH_OK: ALPHA BRAVO CHARLIE\nqueued message delivered\ncontext: 22k / 200k",
        ],
        "",
    )
    assert not passed
    assert "queued into an active run" in details


def test_validate_tg_inbound_batching_accepts_single_batched_turn() -> None:
    passed, details = _validate_scenario(
        "tg_inbound_batching",
        [
            "BATCH_OK: ALPHA BRAVO CHARLIE\nOne sentence covering Babbage, Turing, ARPANET, the Web, and modern ML.\ncontext: 22k / 200k",
        ],
        "",
    )
    assert passed
    assert details == "ok"


def test_validate_immutable_guard_ignores_immutable_word_in_skill_dump() -> None:
    first = (
        "Skill docs mention immutable paths. "
        "The file already exists at .claude/drafts/test-mutable.md with WRITE_OK in place."
    )
    second = "I can't do that. Misc/Meeting Notes is immutable."
    passed, details = _validate_scenario("immutable_guard", [first, second], "")
    assert passed
    assert details == "ok"


@pytest.mark.asyncio
async def test_run_deterministic_reports_missing_validator() -> None:
    scenario = EvalScenario(
        name="Unknown Scenario",
        steps=[EvalStep(action="send", message="hello")],
    )
    platform = _FakePlatform(send_outputs=["hello there"])

    result = await run_deterministic("unknown_case", scenario, platform)

    assert not result.passed
    assert "no deterministic validator" in result.details


@pytest.mark.asyncio
async def test_run_deterministic_uses_fast_default_continuation_timeout() -> None:
    scenario = EvalScenario(
        name="Nowait Scenario",
        steps=[
            EvalStep(action="send_nowait", message="start"),
            EvalStep(action="sleep", wait_seconds=0),
        ],
    )
    platform = _FakePlatform()

    await run_deterministic("unknown_case", scenario, platform)

    assert platform.wait_timeouts == [20]
