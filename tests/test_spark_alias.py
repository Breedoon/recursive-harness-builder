"""Spark model aliases: backend selection by model name, forks, and the spawn preflight."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from obs_agent.config import OBSConfig
from obs_agent.session import SessionManager
from obs_agent.spark import (
    DEFAULT_BASE_URL,
    SparkUnavailableError,
    check_spark_available,
    is_spark_model,
    read_spark_key,
    spark_backend_env,
    spark_provider_window,
)

QWEN = "local-sparks-qwen3.8-flash-next-abliterated"
GLM = "local-sparks-glm-5.3-flash-uncensored"
KEY = "spark-key-for-tests"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "spark-api-key"
    path.write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("OBS_SPARK_LLM_KEY_FILE", str(path))
    monkeypatch.delenv("OBS_SPARK_LLM_BASE_URL", raising=False)
    return path


@pytest.fixture
def config(tmp_path):
    return OBSConfig(vault_path=tmp_path)


def _options(config, model, *, overrides=None, proxy=True):
    manager = SessionManager(config=config)
    manager.model_override = model
    if overrides is not None:
        manager.set_sdk_env_overrides(overrides)
    with patch("obs_agent.cache_proxy_lifecycle.should_use_proxy", return_value=proxy):
        return manager, manager.create_options()


class TestNamePredicates:
    @pytest.mark.parametrize("model", [QWEN, GLM, QWEN + "[1m]", GLM.upper()])
    def test_spark_names(self, model):
        assert is_spark_model(model)

    @pytest.mark.parametrize("model", ["local-qwen3.8-27b", "qwen", "claude-sonnet-5-5", "", None])
    def test_other_names(self, model):
        assert not is_spark_model(model)

    def test_window_follows_an_explicit_larger_suffix(self):
        assert spark_provider_window(262_000, 262_000) == 262_000
        assert spark_provider_window(262_000, 100_000) == 262_000
        assert spark_provider_window(262_000, 1_000_000) == 1_000_000


class TestBackendEnv:
    def test_fills_backend_credential_and_small_model(self, key_file):
        env = spark_backend_env(QWEN + "[262k]", {})
        assert env == {
            "ANTHROPIC_BASE_URL": DEFAULT_BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": KEY,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": QWEN,
            "ANTHROPIC_SMALL_FAST_MODEL": QWEN,
        }

    def test_explicit_values_win_and_key_is_not_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OBS_SPARK_LLM_KEY_FILE", str(tmp_path / "missing"))
        existing = {
            "ANTHROPIC_BASE_URL": "http://192.168.1.241:8000",
            "ANTHROPIC_API_KEY": "explicit",
            "ANTHROPIC_SMALL_FAST_MODEL": "other",
        }
        env = spark_backend_env(GLM, existing)
        assert env == {"ANTHROPIC_DEFAULT_HAIKU_MODEL": GLM}

    def test_missing_key_file_fails_clearly_without_leaking(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OBS_SPARK_LLM_KEY_FILE", str(tmp_path / "missing"))
        with pytest.raises(SparkUnavailableError, match="unreadable"):
            read_spark_key()

    def test_empty_key_file_fails(self, monkeypatch, tmp_path):
        empty = tmp_path / "empty"
        empty.write_text("\n", encoding="utf-8")
        monkeypatch.setenv("OBS_SPARK_LLM_KEY_FILE", str(empty))
        with pytest.raises(SparkUnavailableError, match="empty"):
            read_spark_key()

    def test_base_url_override(self, key_file, monkeypatch):
        monkeypatch.setenv("OBS_SPARK_LLM_BASE_URL", "http://127.0.0.1:9999/")
        assert spark_backend_env(QWEN, {})["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"


class TestSessionOptions:
    def test_alias_configures_the_whole_backend_without_env(self, config, key_file, monkeypatch):
        monkeypatch.setenv("OBS_LOCAL_LLM_BASE_URL", "http://3090-gate:8080")
        monkeypatch.setenv("OBS_LOCAL_LLM_AUTH_TOKEN", "3090-gate-token")
        from obs_agent.config import resolve_model

        manager, options = _options(config, resolve_model("qwen-fn"))

        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == KEY  # never the 3090 gate credential
        assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == QWEN
        assert options.env["ANTHROPIC_SMALL_FAST_MODEL"] == QWEN
        assert "ANTHROPIC_API_KEY" not in options.env
        # Default window = the Sparks' native 262K (not the 1M unknown-model default).
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "262000"
        # Same CLI selector as the explicit-env route (Claude Code strips [1m]
        # before sending; the percentage below carries the real 262K target).
        assert options.model == QWEN + "[1m]"
        assert manager.hook_state.effective_model == QWEN + "[262k]"
        # The key is derived per build; it never enters persisted env overrides.
        assert manager.hook_state.sdk_env_overrides == {}

    def test_fork_with_inherited_model_and_no_env_reaches_the_sparks(self, config, key_file):
        """The Runbook's measured failure: a fork inherits the model name, not the env."""
        parent, parent_options = _options(config, QWEN + "[262k]")
        child = SessionManager(config=config)
        child.model_override = parent.hook_state.effective_model  # what a fork inherits
        with patch("obs_agent.cache_proxy_lifecycle.should_use_proxy", return_value=True):
            child_options = child.create_options()

        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
            assert child_options.env[key] == parent_options.env[key]
        assert child_options.model == parent_options.model

    def test_glm_native_default_builds_session_options(self, config, key_file):
        manager, options = _options(config, GLM)
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1048576"
        assert manager.hook_state.effective_model == GLM
        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL

    def test_glm_4bpw_native_default_builds_session_options(self, config, key_file):
        model = "local-sparks-glm-5.3-flash-exl3-4bpw"
        manager, options = _options(config, model)
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1048576"
        assert manager.hook_state.effective_model == model
        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
        assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == model

    def test_glm_4bpw_explicit_context_override_is_preserved(self, config, key_file):
        model = "local-sparks-glm-5.3-flash-exl3-4bpw"
        manager, options = _options(config, model + "[200k]")
        assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "200000"
        assert manager.hook_state.effective_model == model + "[200k]"
        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL

    def test_glm_alias_uses_glm_served_name(self, config, key_file):
        _manager, options = _options(config, GLM + "[262k]")
        assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == GLM
        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL

    def test_explicit_env_route_is_unchanged(self, config, key_file):
        explicit = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:28931",
            "ANTHROPIC_AUTH_TOKEN": "explicit-token",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": QWEN,
            "ANTHROPIC_SMALL_FAST_MODEL": QWEN,
        }
        _m, via_env = _options(config, QWEN + "[262k]", overrides=explicit)
        _m, via_name = _options(config, QWEN + "[262k]")
        assert via_env.env["ANTHROPIC_AUTH_TOKEN"] == "explicit-token"
        assert via_env.model == via_name.model
        assert via_env.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == via_name.env[
            "OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"
        ]

    def test_direct_debug_route_env_still_wins(self, config, key_file):
        _m, options = _options(
            config, QWEN + "[262k]",
            overrides={"ANTHROPIC_BASE_URL": "http://192.168.1.241:8000"},
        )
        assert options.env["ANTHROPIC_BASE_URL"] == "http://192.168.1.241:8000"
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == KEY

    def test_1m_suffix_raises_the_provider_window_so_compaction_is_not_capped_at_262k(
        self, config, key_file,
    ):
        _m, default = _options(config, QWEN + "[262k]")
        _m, big = _options(config, QWEN + "[1m]")
        assert big.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "1000000"
        assert big.model.endswith("[1m]")
        default_pct = float(default.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"])
        big_pct = float(big.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"])
        # 1M profile compacts near 967K of a 1M window; the default profile near
        # 217K (262K window - 32K max output - 13K buffer), i.e. ~22% of the CLI's 1M selector.
        assert big_pct > 95
        assert 20 < default_pct < 25

    def test_3090_alias_is_untouched(self, config, key_file, monkeypatch):
        monkeypatch.setenv("OBS_LOCAL_LLM_BASE_URL", "http://3090-gate:8080")
        monkeypatch.setenv("OBS_LOCAL_LLM_AUTH_TOKEN", "3090-gate-token")
        from obs_agent.config import resolve_model

        _m, options = _options(config, resolve_model("qwen-27b"))
        assert options.env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{config.cache_proxy_port}"
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == "3090-gate-token"
        assert "ANTHROPIC_SMALL_FAST_MODEL" not in options.env

    def test_missing_key_fails_the_build_with_a_clear_error(self, config, monkeypatch, tmp_path):
        monkeypatch.setenv("OBS_SPARK_LLM_KEY_FILE", str(tmp_path / "missing"))
        manager = SessionManager(config=config)
        manager.model_override = QWEN + "[262k]"
        with pytest.raises(SparkUnavailableError):
            manager.create_options()


class _ModelsHandler(BaseHTTPRequestHandler):
    payload: dict = {}
    status = 200
    seen_auth: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        body = json.dumps(type(self).payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def models_server(key_file, monkeypatch):
    handler = type("Handler", (_ModelsHandler,), {"payload": {}, "status": 200, "seen_auth": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OBS_SPARK_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield handler
    finally:
        server.shutdown()
        server.server_close()


class TestPreflight:
    def test_served_model_within_window_passes(self, models_server):
        models_server.payload = {"data": [{"id": QWEN, "max_model_len": 262_144}]}
        check_spark_available(QWEN, 262_000)
        assert models_server.seen_auth == [f"Bearer {KEY}"]

    def test_model_not_being_served_fails_with_switch_hint(self, models_server):
        models_server.payload = {"data": [{"id": QWEN, "max_model_len": 262_144}]}
        with pytest.raises(SparkUnavailableError) as raised:
            check_spark_available(GLM, 262_000)
        message = str(raised.value)
        assert GLM in message and QWEN in message
        assert "spark-model switch" in message
        assert KEY not in message

    def test_window_larger_than_the_serving_profile_fails(self, models_server):
        models_server.payload = {"data": [{"id": QWEN, "max_model_len": 262_144}]}
        with pytest.raises(SparkUnavailableError, match="qwen-1m"):
            check_spark_available(QWEN, 1_000_000)

    def test_1m_profile_accepts_1m(self, models_server):
        models_server.payload = {"data": [{"id": QWEN, "max_model_len": 1_048_576}]}
        check_spark_available(QWEN, 1_000_000)

    def test_unreachable_endpoint_fails_open(self, key_file, monkeypatch):
        monkeypatch.setenv("OBS_SPARK_LLM_BASE_URL", "http://127.0.0.1:1")
        check_spark_available(QWEN, 262_000, timeout=1.0)

    def test_gateway_error_fails_open(self, models_server):
        models_server.status = 502
        check_spark_available(QWEN, 262_000)

    def test_preflight_can_be_disabled_and_ignores_other_models(self, models_server, monkeypatch):
        models_server.payload = {"data": [{"id": "something-else"}]}
        check_spark_available("local-qwen3.8-27b", 262_000)
        check_spark_available("claude-sonnet-5-5", 1_000_000)
        monkeypatch.setenv("OBS_SPARK_PREFLIGHT", "0")
        check_spark_available(QWEN, 262_000)
        assert models_server.seen_auth == []
