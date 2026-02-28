"""Tests for Claude CLI context probe parsing."""

from obs_agent.context_probe import parse_context_markdown


def test_parse_context_markdown_parses_scaled_tokens() -> None:
    md = """
## Context Usage

**Model:** claude-opus-4-6
**Tokens:** 37k / 200k (18%)
"""
    probe = parse_context_markdown(md)
    assert probe is not None
    assert probe.used_tokens == 37_000
    assert probe.window_tokens == 200_000
    assert probe.used_pct == 18.0


def test_parse_context_markdown_parses_decimal_scale() -> None:
    md = "**Tokens:** 30.4k / 200k (15.2%)"
    probe = parse_context_markdown(md)
    assert probe is not None
    assert probe.used_tokens == 30_400
    assert probe.window_tokens == 200_000
    assert probe.used_pct == 15.2


def test_parse_context_markdown_returns_none_when_line_missing() -> None:
    md = "No context tokens found here."
    assert parse_context_markdown(md) is None

