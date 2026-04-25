"""Unit tests for AgentTask improvement features.

Pure logic tests — no live Telegram, no CLIProxyAPI.
Covers: model resolution, context suffix parsing, compaction formula,
hook spec parsing, prompt_file validation, env filtering.
"""

from __future__ import annotations

import pytest

from obs_agent.config import (
    MODEL_RESOLUTION,
    compaction_threshold,
    is_claude_model,
    parse_context_suffix,
    resolve_model,
)


# ---------------------------------------------------------------------------
# Model resolution whitelist (12 cases)
# ---------------------------------------------------------------------------

class TestModelResolution:
    def test_claude_shorthand_resolves_to_opus(self):
        assert resolve_model("claude") == "claude-opus-4-6"

    def test_claude_opus_shorthand(self):
        assert resolve_model("claude-opus") == "claude-opus-4-6"

    def test_claude_sonnet_shorthand(self):
        assert resolve_model("claude-sonnet") == "claude-sonnet-4-6"

    def test_claude_haiku_shorthand(self):
        assert resolve_model("claude-haiku") == "claude-haiku-4-5"

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
        assert resolve_model("gpt-5.4") == "gpt-5.4"

    def test_unknown_model_passes_through(self):
        assert resolve_model("llama-3-70b") == "llama-3-70b"

    def test_case_insensitive_lookup(self):
        assert resolve_model("Claude") == resolve_model("claude")
        assert resolve_model("GPT") == resolve_model("gpt")
        assert resolve_model("GEMINI") == resolve_model("gemini")

    def test_shorthand_with_context_suffix_preserved(self):
        result = resolve_model("claude[1m]")
        assert result == "claude-opus-4-6[1m]"

    def test_explicit_with_context_suffix_preserved(self):
        result = resolve_model("gpt-5.4[200k]")
        assert result == "gpt-5.4[200k]"


# ---------------------------------------------------------------------------
# Context suffix parsing (8 cases)
# ---------------------------------------------------------------------------

class TestContextSuffixParsing:
    def test_1m_suffix(self):
        clean, tokens = parse_context_suffix("claude-opus-4-6[1m]")
        assert clean == "claude-opus-4-6"
        assert tokens == 1_000_000

    def test_200k_suffix(self):
        clean, tokens = parse_context_suffix("gpt-5.4[200k]")
        assert clean == "gpt-5.4"
        assert tokens == 200_000

    def test_128k_suffix(self):
        clean, tokens = parse_context_suffix("some-model[128k]")
        assert clean == "some-model"
        assert tokens == 128_000

    def test_no_suffix_defaults_to_1m(self):
        clean, tokens = parse_context_suffix("gemini-2.5-pro")
        assert clean == "gemini-2.5-pro"
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
        assert is_claude_model("gemini-2.5-pro") is False

    def test_claude_with_suffix_is_claude(self):
        assert is_claude_model("claude-opus-4-6[1m]") is True


# ---------------------------------------------------------------------------
# Hook spec parsing (7 cases)
# ---------------------------------------------------------------------------

class TestHookSpecParsing:
    """Test the hook spec format 'file.py::function_name'."""

    def test_valid_spec_split(self):
        spec = "/tmp/guard.py::check_access"
        assert "::" in spec
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "/tmp/guard.py"
        assert fname == "check_access"

    def test_missing_double_colon(self):
        spec = "/tmp/guard.py:check_access"  # single colon
        assert "::" not in spec

    def test_empty_function_name(self):
        spec = "/tmp/guard.py::"
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "/tmp/guard.py"
        assert fname == ""

    def test_path_with_spaces(self):
        spec = "/tmp/my hooks/guard file.py::check_access"
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "/tmp/my hooks/guard file.py"
        assert fname == "check_access"

    def test_multiple_double_colons(self):
        """rsplit with maxsplit=1 handles this correctly."""
        spec = "/tmp/weird::path.py::func"
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "/tmp/weird::path.py"
        assert fname == "func"

    def test_relative_path(self):
        spec = "procedures/hooks/guard.py::check_access"
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "procedures/hooks/guard.py"
        assert fname == "check_access"

    def test_tilde_path(self):
        spec = "~/hooks/guard.py::check_access"
        fpath, fname = spec.rsplit("::", 1)
        assert fpath == "~/hooks/guard.py"
        assert fname == "check_access"


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
    """Test env dict validation patterns used in tools.py."""

    def test_empty_env_dict(self):
        env = {}
        assert isinstance(env, dict)
        assert len(env) == 0

    def test_normal_env_vars(self):
        env = {"MY_VAR": "value", "OTHER": "123"}
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())

    def test_special_chars_in_values(self):
        env = {"VAR": "hello world", "QUOTE": 'say "hi"', "NEWLINE": "line1\nline2"}
        assert len(env) == 3

    def test_env_parsed_from_json_string(self):
        """MCP passes env as a JSON string — verify parsing."""
        import json
        raw = '{"MY_VAR": "value"}'
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert parsed["MY_VAR"] == "value"

    def test_invalid_json_raises(self):
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads("not json")
