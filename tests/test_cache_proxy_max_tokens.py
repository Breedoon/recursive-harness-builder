"""Per-model output ceiling (ops-599).

The proxy lifts the CLI's default max_tokens (32,000) to each model's measured
maximum and leaves every other value alone. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy
from obs_agent.claude_context import safe_compaction_threshold
from obs_agent.config import MODEL_CONTEXT_WINDOWS


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5-5", 128000),
    ("claude-sonnet-5-5", 128000),
    ("claude-fable-5-2", 128000),
    ("claude-mythos-5-2", 128000),
    ("claude-haiku-5-5", 128000),
    ("claude-opus-4-6", 128000),
    ("claude-haiku-4-5-20251001", 64000),
    ("local-sparks-glm-5.3-flash-uncensored", 64000),
])
def test_default_is_lifted_to_the_model_maximum(model, expected):
    body = {"model": model, "max_tokens": 32000}
    assert cache_proxy.raise_default_max_tokens(body) == expected
    assert body["max_tokens"] == expected


@pytest.mark.parametrize("max_tokens", [1, 3000, 21333, 31999, 64000, None])
def test_other_values_are_left_alone(max_tokens):
    body = {"model": "claude-opus-5-5"}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    assert cache_proxy.raise_default_max_tokens(body) == 0
    assert body.get("max_tokens") == max_tokens


@pytest.mark.parametrize("model", [
    "gpt-6.2-astra", "claude-opus-4-1", "claude-3-7-sonnet", "local-qwen3.8-27b",
    "local-sparks-deepseek-v4.1-flash-1017", "",
])
def test_unknown_models_are_left_alone(model):
    body = {"model": model, "max_tokens": 32000}
    assert cache_proxy.raise_default_max_tokens(body) == 0
    assert body["max_tokens"] == 32000


def test_local_values_keep_the_compaction_target_under_the_server_ceiling():
    """A local server rejects prompt + max_tokens > window, so the target OBS
    computes with the CLI's 32,000 must not move when the proxy raises it."""
    for model, window in MODEL_CONTEXT_WINDOWS.items():
        limit = cache_proxy.model_max_output_tokens(model)
        if not model.startswith("local-") or not limit:
            continue
        for budget in (200_000, 1_000_000, window):
            assumed = safe_compaction_threshold(
                min(budget, 1_000_000), provider_window_tokens=window, max_output_tokens=32000)
            raised = safe_compaction_threshold(
                min(budget, 1_000_000), provider_window_tokens=window, max_output_tokens=limit)
            assert assumed == raised, (model, budget)
