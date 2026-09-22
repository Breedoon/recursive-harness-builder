"""Pure regressions for requested context sizes and the compaction target.

These tests require neither a provider nor a Claude executable.
"""

import pytest

from obs_agent.config import (
    compaction_threshold,
    normalize_model_for_claude_code,
    parse_context_suffix,
    resolve_model,
    split_context_suffix,
)


@pytest.mark.parametrize("window, expected", [
    (100_000, 67_000), (128_000, 95_000), (200_000, 167_000),
    (201_000, 168_000), (333_000, 300_000), (400_000, 367_000),
    (999_000, 966_000), (1_000_000, 967_000),
])
def test_compaction_target_is_linear_in_tokens(window, expected):
    assert compaction_threshold(window) == expected


@pytest.mark.parametrize("window", [-1, 0, 1, 9_000, 10_000, 33_000])
def test_no_negative_compaction_threshold(window):
    assert compaction_threshold(window) == 0


@pytest.mark.parametrize("model", ["gpt[400k] ", "  gpt[400K]\n", "gpt[400k]\t"])
def test_whitespace_does_not_erase_context_suffix(model):
    assert split_context_suffix(model) == ("gpt", 400_000)
    assert parse_context_suffix(model) == ("gpt-6-sol", 400_000)


@pytest.mark.parametrize("model", ["gpt[0k]", "gpt[0m]", "[400k]", "gpt[400k][1m]"])
def test_invalid_context_identity_is_rejected(model):
    with pytest.raises(ValueError):
        resolve_model(model)


def test_obs_identity_retains_arbitrary_context_for_inheritance():
    assert normalize_model_for_claude_code("gpt[400k]") == "gpt-6-sol[400k]"
