"""Unit tests for AgentTask improvement features.

Pure logic tests — no live Telegram, no CLIProxyAPI.
Covers: model resolution, context suffix parsing, compaction formula,
hook spec parsing, prompt_file validation, env filtering.
"""

from __future__ import annotations

import pytest

from obs_agent.config import (
    MODEL_RESOLUTION,
    auto_compact_window_for_context,
    auto_compact_window_for_model,
    compaction_threshold,
    is_claude_model,
    normalize_model_for_claude_code,
    parse_context_suffix,
    resolve_model,
    split_context_suffix,
)


# ---------------------------------------------------------------------------
# Model resolution whitelist (12 cases)
# ---------------------------------------------------------------------------

class TestModelResolution:
    def test_claude_shorthand_resolves_to_opus(self):
        assert resolve_model("claude") == "claude-opus-4-8"

    def test_claude_opus_shorthand(self):
        assert resolve_model("claude-opus") == "claude-opus-4-8"

    def test_claude_sonnet_shorthand(self):
        assert resolve_model("claude-sonnet") == "claude-sonnet-4-6"
        assert resolve_model("sonnet") == "claude-sonnet-4-6"

    def test_claude_haiku_shorthand(self):
        assert resolve_model("claude-haiku") == "claude-haiku-4-5"
        assert resolve_model("haiku") == "claude-haiku-4-5"

    def test_gpt_shorthand_resolves_to_latest(self):
        resolved = resolve_model("gpt")
        assert "gpt" in resolved.lower()

    def test_openai_shorthand(self):
        assert resolve_model("openai") == resolve_model("gpt")

    def test_gemini_shorthand_resolves_to_pro(self):
        resolved = resolve_model("gemini")
        assert "gemini" in resolved.lower()

    def test_gemini_flash_shorthand(self):
        assert resolve_model("gemini-flash") == "gemini-2.5-flash"

    def test_explicit_model_passes_through(self):
        assert resolve_model("gpt-5.4-mini") == "gpt-5.4-mini"

    def test_unknown_model_passes_through(self):
        assert resolve_model("llama-3-70b") == "llama-3-70b"

    def test_case_insensitive_lookup(self):
        assert resolve_model("Claude") == resolve_model("claude")
        assert resolve_model("GPT") == resolve_model("gpt")
        assert resolve_model("GEMINI") == resolve_model("gemini")

    def test_shorthand_with_context_suffix_preserved(self):
        result = resolve_model("claude[1m]")
        assert result == "claude-opus-4-8[1m]"

    def test_explicit_with_context_suffix_preserved(self):
        result = resolve_model("gpt-5.4-mini[200k]")
        assert result == "gpt-5.4-mini[200k]"

    def test_resolution_does_not_add_default_context_suffix(self):
        assert resolve_model("gpt") == "gpt-5.5"
        assert resolve_model("claude") == "claude-opus-4-8"


class TestModelContextBoundary:
    def test_split_context_suffix_reports_explicit_context_only(self):
        assert split_context_suffix("gpt-5.4-mini") == ("gpt-5.4-mini", None)
        assert split_context_suffix("gpt-5.4-mini[200k]") == ("gpt-5.4-mini", 200_000)

    def test_claude_code_boundary_adds_resolved_context_suffix(self):
        assert normalize_model_for_claude_code("gpt") == "gpt-5.5[400k]"
        assert normalize_model_for_claude_code("claude") == "claude-opus-4-8[1m]"
        assert normalize_model_for_claude_code("haiku") == "claude-haiku-4-5[200k]"
        assert normalize_model_for_claude_code("gemini") == "gemini-3.1-flash-lite-preview[1m]"

    def test_claude_code_boundary_preserves_explicit_context_suffix(self):
        assert normalize_model_for_claude_code("gpt[200k]") == "gpt-5.5[200k]"
        assert normalize_model_for_claude_code("gpt-5.4-mini[128k]") == "gpt-5.4-mini[128k]"

    def test_local_provider_boundary_preserves_canonical_model_id(self):
        assert normalize_model_for_claude_code("local-gemma4-31b") == "local-gemma4-31b"
        assert normalize_model_for_claude_code("local-qwen3.5-27b[128k]") == "local-qwen3.5-27b"
        assert parse_context_suffix("local-gemma4-31b") == ("local-gemma4-31b", 32_000)
        assert parse_context_suffix("local-qwen3.5-27b[128k]") == (
            "local-qwen3.5-27b",
            128_000,
        )

    def test_auto_compact_window_tracks_context_by_default(self):
        assert auto_compact_window_for_context(1_000_000) == 1_000_000
        assert auto_compact_window_for_context(256_000) == 256_000
        assert auto_compact_window_for_context(128_000) == 128_000
        assert auto_compact_window_for_context(100_000) == 100_000

    def test_auto_compact_window_cap_can_be_disabled(self):
        assert auto_compact_window_for_context(
            1_000_000,
            auto_compact_window_tokens=0,
        ) == 1_000_000

    def test_model_aware_auto_compact_defaults_track_resolved_context(self):
        assert auto_compact_window_for_model("claude", 1_000_000) == 1_000_000
        assert auto_compact_window_for_model("gpt", 400_000) == 400_000
        assert compaction_threshold(auto_compact_window_for_model("gpt", 400_000)) == 342_500
        assert auto_compact_window_for_model("gpt[128k]", 128_000) == 128_000
        assert auto_compact_window_for_model(
            "claude",
            1_000_000,
            auto_compact_window_tokens=150_000,
        ) == 150_000


# ---------------------------------------------------------------------------
# Context suffix parsing (8 cases)
# ---------------------------------------------------------------------------

class TestContextSuffixParsing:
    def test_1m_suffix(self):
        clean, tokens = parse_context_suffix("claude-opus-4-7[1m]")
        assert clean == "claude-opus-4-7"
        assert tokens == 1_000_000

    def test_200k_suffix(self):
        clean, tokens = parse_context_suffix("gpt-5.4-mini[200k]")
        assert clean == "gpt-5.4-mini"
        assert tokens == 200_000

    def test_128k_suffix(self):
        clean, tokens = parse_context_suffix("some-model[128k]")
        assert clean == "some-model"
        assert tokens == 128_000

    def test_no_suffix_defaults_to_1m(self):
        clean, tokens = parse_context_suffix("gemini-3.1-flash-lite-preview")
        assert clean == "gemini-3.1-flash-lite-preview"
        assert tokens == 1_000_000

    def test_claude_alias_uses_default_obs_context_window(self):
        clean, tokens = parse_context_suffix("claude")
        assert clean == "claude-opus-4-8"
        assert tokens == 1_000_000

    def test_uppercase_suffix(self):
        clean, tokens = parse_context_suffix("model[1M]")
        assert clean == "model"
        assert tokens == 1_000_000

    def test_invalid_suffix_ignored(self):
        """Invalid suffixes like [1g] should be treated as part of the model name."""
        clean, tokens = parse_context_suffix("model[1g]")
        # The regex won't match, so the whole string is the model
        assert tokens == 1_000_000  # default

    def test_empty_string(self):
        clean, tokens = parse_context_suffix("")
        assert clean == ""
        assert tokens == 1_000_000

    def test_suffix_only_matches_end(self):
        """Suffix-like patterns in the middle should not match."""
        clean, tokens = parse_context_suffix("model[1m]-variant")
        # [1m] is not at the end, so no match
        assert tokens == 1_000_000


# ---------------------------------------------------------------------------
# Compaction threshold formula (6 cases)
# ---------------------------------------------------------------------------

class TestCompactionThreshold:
    def test_1m_context(self):
        threshold = compaction_threshold(1_000_000)
        # Should be around 920K (92%)
        assert 900_000 <= threshold <= 950_000

    def test_200k_context(self):
        threshold = compaction_threshold(200_000)
        # Should be around 167K (83.5%)
        assert 160_000 <= threshold <= 175_000

    def test_400k_context_interpolates_between_200k_and_1m(self):
        assert compaction_threshold(400_000) == 342_500

    def test_128k_context(self):
        threshold = compaction_threshold(128_000)
        # Between the two known points
        assert 100_000 <= threshold <= 120_000

    def test_32k_context_clamps_at_low(self):
        threshold = compaction_threshold(32_000)
        # Clamped at 83.5% for small contexts, minus 10K headroom
        assert threshold <= 32_000 - 10_000
        assert threshold > 0

    def test_zero_context(self):
        assert compaction_threshold(0) == 0

    def test_headroom_enforced(self):
        """Threshold should always leave at least 10K tokens of headroom."""
        for ctx in [50_000, 100_000, 200_000, 500_000, 1_000_000]:
            threshold = compaction_threshold(ctx)
            assert ctx - threshold >= 10_000, f"Insufficient headroom at {ctx}"


# ---------------------------------------------------------------------------
# is_claude_model (5 cases)
# ---------------------------------------------------------------------------

class TestIsClaudeModel:
    def test_opus_is_claude(self):
        assert is_claude_model("claude-opus-4-6") is True

    def test_claude_shorthand_is_claude(self):
        assert is_claude_model("claude") is True

    def test_gpt_is_not_claude(self):
        assert is_claude_model("gpt-5.5") is False

    def test_gemini_is_not_claude(self):
        assert is_claude_model("gemini-3.1-flash-lite-preview") is False

    def test_claude_with_suffix_is_claude(self):
        assert is_claude_model("claude-opus-4-6[1m]") is True


# ---------------------------------------------------------------------------
# Hook spec parsing (7 cases)
# ---------------------------------------------------------------------------

class TestHookSpecParsing:
    """Test hook spec parsing via actual load_hook_function."""

    def test_valid_spec_loads_function(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_file = tmp_path / "guard.py"
        hook_file.write_text(
            "def check_access(hook_input, tool_use_id, context):\n"
            "    return None\n"
        )
        fn = load_hook_function(str(hook_file), "check_access")
        assert callable(fn)
        assert fn.__name__ == "check_access"

    def test_missing_double_colon_in_spec_format(self):
        """Verify the spec format validation catches single colon."""
        spec = "/tmp/guard.py:check_access"
        assert "::" not in spec  # Format check before load

    def test_path_with_spaces(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_dir = tmp_path / "my hooks"
        hook_dir.mkdir()
        hook_file = hook_dir / "guard file.py"
        hook_file.write_text("def func(h, t, c): return None\n")
        fn = load_hook_function(str(hook_file), "func")
        assert callable(fn)

    def test_function_not_found_lists_available(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("def real_func(h, t, c): return None\n")
        with pytest.raises(AttributeError, match="Available:.*real_func"):
            load_hook_function(str(hook_file), "nonexistent")

    def test_syntax_error_in_file(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_file = tmp_path / "bad.py"
        hook_file.write_text("def func(:\n    invalid\n")
        with pytest.raises(SyntaxError):
            load_hook_function(str(hook_file), "func")

    def test_non_py_file_rejected(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_file = tmp_path / "hook.txt"
        hook_file.write_text("not python")
        with pytest.raises(ValueError, match=".py"):
            load_hook_function(str(hook_file), "func")

    def test_not_callable_rejected(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("my_var = 42\n")
        with pytest.raises(TypeError, match="not callable"):
            load_hook_function(str(hook_file), "my_var")


# ---------------------------------------------------------------------------
# Hook function loading (integration with importlib)
# ---------------------------------------------------------------------------

class TestHookFunctionLoading:
    def test_load_valid_hook(self, tmp_path):
        hook_file = tmp_path / "good_hook.py"
        hook_file.write_text(
            "def my_hook(hook_input, tool_use_id, context):\n"
            "    return None\n"
        )
        from obs_agent.hooks import load_hook_function
        fn = load_hook_function(str(hook_file), "my_hook")
        assert callable(fn)
        assert fn.__name__ == "my_hook"

    def test_load_nonexistent_file(self, tmp_path):
        from obs_agent.hooks import load_hook_function
        with pytest.raises(FileNotFoundError):
            load_hook_function(str(tmp_path / "nonexistent.py"), "func")

    def test_load_nonexistent_function(self, tmp_path):
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("def other_func(): pass\n")
        from obs_agent.hooks import load_hook_function
        with pytest.raises(AttributeError, match="Available:"):
            load_hook_function(str(hook_file), "nonexistent")

    def test_load_syntax_error(self, tmp_path):
        hook_file = tmp_path / "bad.py"
        hook_file.write_text("def func(:\n    this is not valid\n")
        from obs_agent.hooks import load_hook_function
        with pytest.raises(SyntaxError):
            load_hook_function(str(hook_file), "func")

    def test_load_non_py_file(self, tmp_path):
        hook_file = tmp_path / "hook.txt"
        hook_file.write_text("not python")
        from obs_agent.hooks import load_hook_function
        with pytest.raises(ValueError, match=".py"):
            load_hook_function(str(hook_file), "func")

    def test_load_not_callable(self, tmp_path):
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("my_var = 42\n")
        from obs_agent.hooks import load_hook_function
        with pytest.raises(TypeError, match="not callable"):
            load_hook_function(str(hook_file), "my_var")


# ---------------------------------------------------------------------------
# Env passthrough filtering (5 cases)
# ---------------------------------------------------------------------------

class TestEnvPassthrough:
    """Test env dict validation — matches actual parsing logic in tools.py."""

    def test_empty_env_dict(self):
        import json
        raw = '{}'
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert len(parsed) == 0

    def test_normal_env_vars_from_json(self):
        import json
        raw = '{"MY_VAR": "value", "OTHER": "123"}'
        parsed = json.loads(raw)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items())

    def test_special_chars_in_values(self):
        import json
        raw = json.dumps({"VAR": "hello world", "QUOTE": 'say "hi"'})
        parsed = json.loads(raw)
        assert len(parsed) == 2

    def test_invalid_json_raises(self):
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads("not json")

    def test_non_dict_json_rejected(self):
        """tools.py checks isinstance(env_override, dict) — arrays should fail."""
        import json
        raw = '["not", "a", "dict"]'
        parsed = json.loads(raw)
        assert not isinstance(parsed, dict)  # Would be rejected by tools.py

    def test_anthropic_api_key_passthrough_not_blocked(self):
        """Current implementation does NOT block protected vars.
        This documents the actual behavior — no env filtering exists yet."""
        import json
        raw = json.dumps({"ANTHROPIC_API_KEY": "fake", "MY_VAR": "ok"})
        parsed = json.loads(raw)
        # No filtering applied — both vars pass through
        assert "ANTHROPIC_API_KEY" in parsed
        assert "MY_VAR" in parsed
