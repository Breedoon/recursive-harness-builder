"""Live long-session compaction regression tests.

These tests use real Claude Code / SDK calls. They are intentionally opt-in
because they spend live model tokens, but they exercise the failure mode where
OBS passed a too-large auto-compact window and sessions died with
``Prompt is too long`` before compaction.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obs_agent.config import OBSConfig
from obs_agent.context_jsonl import load_jsonl_usage_snapshot
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent
from obs_agent.session import SessionManager
from tests.live_test_vault import ensure_live_test_vault

pytestmark = [pytest.mark.live, pytest.mark.asyncio, pytest.mark.real_get_client]


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_LONG_CONTEXT_LIVE") != "1",
    reason="set OBS_RUN_LONG_CONTEXT_LIVE=1 to spend live tokens on compaction",
)
async def test_haiku_long_session_auto_compacts_and_remains_usable(tmp_path):
    await _run_haiku_compaction_probe(
        tmp_path=tmp_path,
        auto_compact_window_tokens=50_000,
        padding_repetitions=1200,
        max_turns=7,
        min_peak_tokens=25_000,
        drop_ratio=0.75,
        token_prefix="COMPACT",
    )


@pytest.mark.skipif(
    os.environ.get("OBS_RUN_LONG_CONTEXT_200K_LIVE") != "1",
    reason="set OBS_RUN_LONG_CONTEXT_200K_LIVE=1 to spend live tokens on a 200k compaction probe",
)
async def test_haiku_200k_auto_compacts_and_remains_usable(tmp_path):
    await _run_haiku_compaction_probe(
        tmp_path=tmp_path,
        auto_compact_window_tokens=200_000,
        padding_repetitions=1450,
        max_turns=11,
        min_peak_tokens=145_000,
        drop_ratio=0.80,
        token_prefix="CLAUDE200K",
    )


async def _run_haiku_compaction_probe(
    *,
    tmp_path: Path,
    auto_compact_window_tokens: int,
    padding_repetitions: int,
    max_turns: int,
    min_peak_tokens: int,
    drop_ratio: float,
    token_prefix: str,
) -> None:
    _load_dotenv()
    vault = ensure_live_test_vault(tmp_path / "vault")
    config = OBSConfig(vault_path=vault, model="haiku", cache_proxy_enabled=False)
    config.auto_compact_window_tokens = auto_compact_window_tokens

    hook_state = HookState()
    session_manager = SessionManager(config=config, hook_state=hook_state)
    usage_totals: list[int] = []
    outputs: list[str] = []
    padding = "Synthetic long-context compaction test padding. " * padding_repetitions

    try:
        options = session_manager.create_options()
        assert options.model == "claude-haiku-4-5"
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
        assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(auto_compact_window_tokens)
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in options.env

        for turn in range(1, max_turns + 1):
            runner = ConversationRunner(session_manager, hook_state, config)
            output_parts: list[str] = []
            prompt = (
                f"Turn {turn}. Store this padding, then reply with exactly "
                f"{token_prefix}-OK-{turn}.\n\n{padding}"
            )
            async for event in runner.run(prompt):
                if isinstance(event, TextEvent):
                    output_parts.append(event.text)
                elif isinstance(event, DoneEvent):
                    break

            output = "".join(output_parts)
            outputs.append(output)
            assert "Prompt is too long" not in output

            snapshot = load_jsonl_usage_snapshot(
                session_id=session_manager.session_id,
                cwd=vault,
            )
            if snapshot is None:
                continue
            usage_totals.append(snapshot.latest_context_triplet_tokens)

            if (
                len(usage_totals) >= 3
                and max(usage_totals[:-1]) > min_peak_tokens
                and usage_totals[-1] < max(usage_totals[:-1]) * drop_ratio
            ):
                followup_runner = ConversationRunner(session_manager, hook_state, config)
                followup_parts: list[str] = []
                async for event in followup_runner.run(
                    f"After compaction, reply with exactly {token_prefix}-STILL-USABLE."
                ):
                    if isinstance(event, TextEvent):
                        followup_parts.append(event.text)
                    elif isinstance(event, DoneEvent):
                        break
                followup = "".join(followup_parts)
                assert f"{token_prefix}-STILL-USABLE" in followup
                assert "Prompt is too long" not in followup
                return

        raise AssertionError(
            f"no compaction signal observed; usage_totals={usage_totals}, "
            f"last_outputs={outputs[-2:]}"
        )
    finally:
        await session_manager.disconnect()
