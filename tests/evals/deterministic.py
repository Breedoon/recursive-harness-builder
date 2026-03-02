"""Deterministic eval runner for low-ambiguity scenarios.

This module executes scenario steps via a Platform and validates outcomes with
explicit assertions (no LLM judge), intended for plumbing/surgical behaviors.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from tests.evals.platform import Platform
from tests.evals.scenario import EvalScenario

# Small continuation windows keep deterministic lane fast. Scenarios can
# override via frontmatter `continuation_timeouts` when legitimately needed.
_DEFAULT_CONTINUATION_TIMEOUTS = [20, 5]


@dataclass
class DeterministicResult:
    scenario: str
    passed: bool
    details: str
    transcript: str


def _has_error(text: str) -> bool:
    t = text.lower()
    return any(
        token in t
        for token in [
            "traceback",
            "exception",
            "error:",
            "permission denied",
        ]
    )


def _contains_any(text: str, items: list[str]) -> bool:
    t = text.lower()
    return any(item.lower() in t for item in items)


def _has_completion_marker(text: str) -> bool:
    lower = text.lower()
    return "(done)" in lower or "context:" in lower


async def _run_steps(scenario: EvalScenario, platform: Platform) -> tuple[list[str], str]:
    outputs: list[str] = []
    transcript_parts: list[str] = []
    saw_nowait = False

    for step in scenario.steps:
        if step.action == "send":
            transcript_parts.append(f"USER: {step.message}")
            output = await platform.send(step.message)
            outputs.append(output)
            transcript_parts.append(f"AGENT: {output}")
        elif step.action == "send_nowait":
            saw_nowait = True
            transcript_parts.append(f"USER (nowait): {step.message}")
            await platform.send_nowait(step.message)
        elif step.action == "sleep":
            transcript_parts.append(f"[sleep {step.wait_seconds}s]")
            await asyncio.sleep(step.wait_seconds)

    # Collect continuation outputs for concurrent scenarios.
    if saw_nowait:
        timeouts = scenario.continuation_timeouts or _DEFAULT_CONTINUATION_TIMEOUTS
        for i, timeout in enumerate(timeouts):
            try:
                output = await platform.wait_for_prompt(timeout=timeout)
            except Exception:
                break
            if not output:
                break
            if output.strip().lower().startswith("(timeout:"):
                break
            outputs.append(output)
            label = "AGENT (final)" if i == 0 else f"AGENT (continuation {i})"
            transcript_parts.append(f"{label}: {output}")

    return outputs, "\n\n".join(transcript_parts)


def _extract_int(label: str, text: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*:?\s*(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Markdown-table shape, e.g. "| **input_tokens** | 3 |"
    table_match = re.search(
        rf"{re.escape(label)}[^0-9]{{0,20}}(\d+)",
        text,
        re.IGNORECASE,
    )
    if table_match:
        return int(table_match.group(1))
    return None


def _extract_token_count(kind: str, text: str) -> int | None:
    """Extract token count for `input`/`output` from common response shapes."""
    # Structured: input_tokens: 123
    structured = _extract_int(f"{kind}_tokens", text)
    if structured is not None:
        return structured
    # Natural language: 123 input tokens
    natural = re.search(rf"(\d+)\s+{re.escape(kind)}\s+tokens", text, re.IGNORECASE)
    if natural:
        return int(natural.group(1))
    return None


def _extract_total_tokens_used(text: str) -> int | None:
    """Extract total token usage from generic phrasing.

    Accepts:
    - 'Used: ~24,825 tokens'
    - '24.8k tokens used out of 200k'
    """

    def _parse_amount(raw: str, suffix: str) -> int:
        normalized = raw.replace(",", "")
        value = float(normalized)
        if suffix.lower() == "k":
            value *= 1_000
        elif suffix.lower() == "m":
            value *= 1_000_000
        return int(value)

    patterns = [
        r"used[^0-9]*(\d+(?:[.,]\d+)?)\s*([kKmM]?)\s+tokens",
        r"(\d+(?:[.,]\d+)?)\s*([kKmM]?)\s+tokens\s+used",
        r"\(\s*[~≈]?\s*(\d+(?:[.,]\d+)?)\s*([kKmM]?)\s+tokens\s+of\b",
        r"\(\s*[~≈]?\s*(\d+(?:[.,]\d+)?)\s*([kKmM]?)\s+of\s+\d+(?:[.,]\d+)?\s*[kKmM]?\s+tokens\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_amount(match.group(1), match.group(2))
    return None


def _extract_total_tokens_window(text: str) -> int | None:
    """Extract configured/mentioned context window token count.

    Accepts:
    - 'estimated_context_window_tokens: 200000'
    - '... of 200k tokens'
    - '... out of 200000 window'
    """

    structured = _extract_int("estimated_context_window_tokens", text)
    if structured is not None:
        return structured

    def _parse_amount(raw: str, suffix: str) -> int:
        normalized = raw.replace(",", "")
        value = float(normalized)
        if suffix.lower() == "k":
            value *= 1_000
        elif suffix.lower() == "m":
            value *= 1_000_000
        return int(value)

    patterns = [
        r"\bof\s+(\d+(?:[.,]\d+)?)\s*([kKmM]?)\s+tokens\b",
        r"\bout of\s+(\d+(?:[.,]\d+)?)\s*([kKmM]?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_amount(match.group(1), match.group(2))
    return None


def _validate_context_report(text: str) -> tuple[bool, str]:
    low = text.lower()
    has_uuid = re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        low,
    )
    has_named_session = re.search(r"session[\s\W_]{0,40}\bid\b", low) is not None
    if not has_uuid and not has_named_session:
        return False, "session_id missing"

    input_tokens = _extract_token_count("input", text)
    output_tokens = _extract_token_count("output", text)
    total_used_tokens = _extract_total_tokens_used(text)
    structured_used = _extract_int("estimated_context_used_tokens", text)
    if (
        (input_tokens or 0) <= 0
        and (output_tokens or 0) <= 0
        and (total_used_tokens or 0) <= 0
        and (structured_used or 0) <= 0
    ):
        return False, "token usage appears zero"

    used_tokens = structured_used if structured_used is not None else total_used_tokens
    window_tokens = _extract_total_tokens_window(text)
    if (
        used_tokens is not None
        and window_tokens is not None
        and used_tokens > window_tokens
    ):
        return False, "context used exceeds window estimate (likely cumulative bug)"
    if _contains_any(text, ["100.0%", "100%"]):
        return False, "context remaining appears stuck at 100%"
    if _contains_any(
        text,
        [
            "context_info failed",
            "session_info failed",
            "introspection tool failure",
            "unable to retrieve context",
            "tool invocation failed",
        ],
    ):
        return False, "response reports introspection tool failure"
    return True, "ok"


def _validate_scenario(scenario_id: str, outputs: list[str], transcript: str) -> tuple[bool, str]:
    full = "\n".join(outputs)
    lower = full.lower()

    if scenario_id == "basic_chat":
        if not full.strip():
            return False, "empty response"
        if _has_error(full):
            return False, "response contains error markers"
        if not _contains_any(full, ["help", "assist", "vault", "obsidian"]):
            return False, "response missing expected assistant/vault/help context"
        return True, "ok"

    if scenario_id == "tool_visibility":
        skills = [
            "file-conventions",
            "update-context",
            "manage-summaries",
            "create-reference",
            "session-offboard",
            "vault-search",
            "git-commit",
        ]
        seen = sum(1 for s in skills if s in lower)
        if seen < 2:
            return False, f"expected >=2 known skills, found {seen}"
        if _contains_any(full, ["unknown command", "not found", "error"]):
            return False, "response indicates command/file error"
        return True, "ok"

    if scenario_id == "vault_file_access":
        if _contains_any(full, ["not found", "could not read", "cannot read"]):
            return False, "response indicates file-read failure"
        if not _contains_any(full, ["obs agent", "vault", "assistant", "claude.md"]):
            return False, "response not clearly grounded in CLAUDE.md content"
        return True, "ok"

    if scenario_id == "vault_write":
        if "eval_write_ok" not in lower:
            return False, "missing EVAL_WRITE_OK confirmation"
        if _contains_any(full, ["not found", "could not be read", "permission denied"]):
            return False, "write/read-back flow reported file failure"
        return True, "ok"

    if scenario_id == "session_continuity":
        if len(outputs) < 2:
            return False, "expected two outputs"
        if "pineapple" not in outputs[1].lower():
            return False, "second response did not recall PINEAPPLE"
        return True, "ok"

    if scenario_id == "skills_awareness":
        skills = ["file-conventions", "update-context", "manage-summaries", "create-reference"]
        seen = sum(1 for s in skills if s in lower)
        if seen < 3:
            return False, f"expected >=3 core skills, found {seen}"
        return True, "ok"

    if scenario_id == "fork_tool":
        if len(outputs) < 2:
            return False, "expected two outputs"
        second = outputs[1].lower()
        if not ("2 + 2" in second or "2+2" in second):
            return False, "fork response missing quoted arithmetic question"
        if "4" not in second:
            return False, "fork response missing answer 4"
        if _contains_any(second, ["error", "cannot fork"]):
            return False, "fork path reported error"
        return True, "ok"

    if scenario_id == "immutable_guard":
        if len(outputs) < 2:
            return False, "expected two outputs"
        first = outputs[0].lower()
        second = outputs[1].lower()
        if not _contains_any(first, ["write_ok", "already exists", "created", "written", "in place"]):
            return False, "mutable control write did not clearly succeed"
        if _contains_any(first, ["can't do that", "cannot do that", "not allowed to do that"]):
            return False, "mutable control write was explicitly refused"
        if not _contains_any(second, ["blocked", "immutable", "cannot", "not allowed", "permission"]):
            return False, "immutable write was not clearly blocked"
        return True, "ok"

    if scenario_id == "queue_message":
        if "(queued)" not in transcript:
            return False, "missing queued acknowledgement"
        if "(queued:" not in transcript:
            return False, "missing queued delivery notice"
        if "binary search" not in lower:
            return False, "missing binary search response content"
        if not ("2+2" in lower or " 4" in lower or "\n4\n" in lower):
            return False, "missing queued follow-up response (2+2)"
        return True, "ok"

    if scenario_id == "interrupt":
        if "(interrupting...)" not in transcript.lower():
            return False, "missing interrupt acknowledgement"
        # If the complete 200-item task finished, interruption likely failed.
        if "interrupt_test_200" in lower:
            return False, "task appears fully completed; expected interruption"
        return True, "ok"

    if scenario_id == "session_context_info":
        if len(outputs) < 2:
            return False, "expected two outputs"
        return _validate_context_report(outputs[1])

    if scenario_id == "session_context_non_cumulative":
        if len(outputs) < 3:
            return False, "expected three outputs"
        return _validate_context_report(outputs[-1])

    if scenario_id == "session_context_tool_use_regression":
        if len(outputs) < 6:
            return False, "expected six outputs"
        if "ready" not in outputs[0].lower():
            return False, "missing READY warm-up response"

        first = outputs[1]
        third = outputs[3]
        fifth = outputs[5]

        for label, text in [("first", first), ("third", third), ("fifth", fifth)]:
            ok, details = _validate_context_report(text)
            if not ok:
                return False, f"{label} context snapshot invalid: {details}"

        used_1 = _extract_int("estimated_context_used_tokens", first) or _extract_total_tokens_used(first)
        used_3 = _extract_int("estimated_context_used_tokens", third) or _extract_total_tokens_used(third)
        used_5 = _extract_int("estimated_context_used_tokens", fifth) or _extract_total_tokens_used(fifth)
        if used_1 is None or used_3 is None or used_5 is None:
            return False, "could not parse used tokens from one or more snapshots"

        # Guard against the known regression: tool-heavy turn inflates to a large value,
        # then a plain text turn reports a sharp drop without reset/compaction.
        if used_5 + 5_000 < used_3:
            return False, "context used dropped sharply after plain-text follow-up (likely cumulative/aggregation bug)"
        return True, "ok"

    if scenario_id == "tg_auth_guard":
        if len(outputs) < 2:
            return False, "expected two outputs"
        first = outputs[0].lower()
        second = outputs[1].lower()
        if _contains_any(first, ["unauthorized", "not allowed", "forbidden", "rejected"]):
            return False, "authorized user appears blocked in first turn"
        if _contains_any(second, ["unauthorized", "not allowed", "forbidden", "rejected"]):
            return False, "authorized user appears blocked in second turn"
        if "paris" not in second:
            return False, "second response missing expected one-word answer Paris"
        if _has_error("\n".join(outputs)):
            return False, "response contains error markers"
        return True, "ok"

    if scenario_id == "tg_background_auto_delivery":
        if len(outputs) < 2:
            return False, "expected warm-up + background delivery outputs"
        first = outputs[0].lower()
        full = "\n".join(outputs).lower()
        if "ready" not in first:
            return False, "warm-up response missing READY"
        if "fork_launched" not in full:
            return False, "missing immediate FORK_LAUNCHED confirmation"
        if not _has_completion_marker(full):
            return False, "missing completion marker"
        if not _contains_any(
            full,
            [
                "claude.md",
                "active threads",
                "recent decisions",
                "current focus",
                "obs agent",
            ],
        ):
            return False, "missing concrete CLAUDE-grounded background output"
        if _has_error(full):
            return False, "response contains error markers"
        return True, "ok"

    if scenario_id == "tg_tool_visibility":
        full = "\n".join(outputs)
        lower = full.lower()
        if not full.strip():
            return False, "empty response"
        if not _has_completion_marker(lower):
            return False, "missing completion marker"
        if _has_error(full):
            return False, "response contains error markers"
        skills = ["file-conventions", "update-context", "manage-summaries", "create-reference"]
        if sum(1 for s in skills if s in lower) < 2:
            return False, "expected at least two visible skill names"
        if not _contains_any(lower, ["read", "grep", "glob", "bash", "thinking", "tool"]):
            return False, "missing visible tool/status indicators"
        return True, "ok"

    if scenario_id == "tg_html_format":
        full = "\n".join(outputs)
        lower = full.lower()
        if not _has_completion_marker(lower):
            return False, "missing completion marker"
        if "bubble" not in lower:
            return False, "response missing bubble sort context"
        if "def " not in lower:
            return False, "response missing python function definition"
        if _contains_any(full, ["&lt;", "&gt;", "&amp;"]):
            return False, "escaped HTML entities leaked in output"
        if re.search(r"</?(b|ol|li|code|pre)\b", full, re.IGNORECASE):
            return False, "raw HTML tags visible in output"
        if _has_error(full):
            return False, "response contains error markers"
        return True, "ok"

    if scenario_id == "tg_queue_while_busy":
        full = "\n".join(outputs)
        lower = full.lower()
        if len(outputs) != 1:
            return False, "busy follow-up became a separate turn instead of one queued run"
        if not _has_completion_marker(lower):
            return False, "missing completion marker"
        if not re.search(r"\b4\b", lower):
            return False, "missing queued 2+2 response"
        skills = ["file-conventions", "update-context", "manage-summaries", "create-reference"]
        if sum(1 for s in skills if s in lower) < 2:
            return False, "missing substantive skills response"
        if "queued message delivered" not in lower:
            return False, "missing visible queued-message delivery evidence"
        if _has_error(full):
            return False, "response contains error markers"
        return True, "ok"

    if scenario_id == "tg_inbound_batching":
        full = "\n".join(outputs)
        lower = full.lower()
        if len(outputs) != 1:
            return False, "batched inbound messages became multiple completed turns"
        if not _has_completion_marker(lower):
            return False, "missing completion marker"
        if "batch_ok:" not in lower:
            return False, "missing final batch confirmation"
        for token in ["alpha", "bravo", "charlie"]:
            if token not in lower:
                return False, f"missing {token} from combined batch response"
        if "queued message delivered" in lower:
            return False, "messages were queued into an active run instead of held as one inbound batch"
        if _has_error(full):
            return False, "response contains error markers"
        return True, "ok"

    if scenario_id == "tg_message_split":
        full = "\n".join(outputs)
        lower = full.lower()
        if not _has_completion_marker(lower):
            return False, "missing completion marker"
        if len(full) < 3500:
            return False, "output too short; message-splitting stress likely not exercised"
        milestones = [
            "babbage",
            "lovelace",
            "turing",
            "eniac",
            "transistor",
            "arpanet",
            "personal computer",
            "world wide web",
            "smartphone",
            "machine learning",
        ]
        covered = sum(1 for m in milestones if m in lower)
        if covered < 7:
            return False, f"covered only {covered}/10 expected milestones"
        if _contains_any(lower, ["(timeout:", "session reset", "traceback", "exception"]):
            return False, "transport/runtime markers present in output"
        if re.search(r"</?(b|ol|li|code|pre)\b", full, re.IGNORECASE):
            return False, "raw HTML tags visible in output"
        return True, "ok"

    if scenario_id == "tg_transport_desync_forced_concurrency":
        if len(outputs) < 2:
            return False, "expected two outputs (stress turn + ping turn)"
        first = outputs[0]
        second = outputs[1]
        first_low = first.lower()
        second_low = second.lower()
        if "forced_stress_done" not in first_low:
            return False, "first turn missing FORCED_STRESS_DONE marker"
        if not _has_completion_marker(first_low):
            return False, "first turn missing completion marker"
        if not _contains_any(second_low, ["pong", "ping"]):
            return False, "second turn missing direct ping/pong response"
        if _contains_any(second_low, ["read:", "mcp__obs-agent__read", "2019-12-24", "2020-02-21"]):
            return False, "second turn appears contaminated by stale turn-1 read backlog"
        if not _has_completion_marker(second_low):
            return False, "second turn missing completion marker"
        return True, "ok"

    return False, f"no deterministic validator implemented for {scenario_id}"


async def run_deterministic(
    scenario_id: str,
    scenario: EvalScenario,
    platform: Platform,
) -> DeterministicResult:
    outputs, transcript = await _run_steps(scenario, platform)
    passed, details = _validate_scenario(scenario_id, outputs, transcript)
    return DeterministicResult(
        scenario=scenario.name,
        passed=passed,
        details=details,
        transcript=transcript,
    )
