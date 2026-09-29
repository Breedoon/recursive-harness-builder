"""DGX Sparks backend selection by model name.

The Spark pair serves one model at a time through its own cache proxy
(``127.0.0.1:28931``, supervisor program ``spark-cache-proxy``). A model whose
served name starts with ``local-sparks-`` therefore needs a different backend
from the 3090 ``local-*`` models. Selecting the backend from the *model name*
(rather than from a per-launch ``env``) is what lets forks, resumes and
Telegram restores reach the right endpoint: children inherit the model name,
never the env overrides.

The API key is read from a private file at session-build time. It is never
stored in the repo, in persisted session state, in logs, or in error text.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Mapping

logger = logging.getLogger("obs_agent.spark")

SPARK_MODEL_PREFIX = "local-sparks-"
DEFAULT_BASE_URL = "http://127.0.0.1:28931"
DEFAULT_KEY_FILE = "/workspace/runtime/secrets/spark-api-key"

BASE_URL_ENV = "OBS_SPARK_LLM_BASE_URL"
KEY_FILE_ENV = "OBS_SPARK_LLM_KEY_FILE"
PREFLIGHT_DISABLE_ENV = "OBS_SPARK_PREFLIGHT"  # "0" disables the spawn-time check

_AUTH_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class SparkUnavailableError(RuntimeError):
    """The requested Spark model cannot be used right now (clear, actionable text)."""


def is_spark_model(model: str | None) -> bool:
    """True if *model* (any context suffix allowed) is a Spark served name."""
    return bool(model) and str(model).strip().lower().startswith(SPARK_MODEL_PREFIX)


def _served_name(model: str) -> str:
    return str(model).split("[", 1)[0].strip()


def spark_base_url(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return (environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/")


def read_spark_key(environ: Mapping[str, str] | None = None) -> str:
    """Read the Spark API key from its private file (never from the repo)."""
    environ = os.environ if environ is None else environ
    path = (environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE).strip()
    try:
        with open(path, encoding="utf-8") as handle:
            key = handle.readline().strip()
    except OSError as exc:
        raise SparkUnavailableError(
            f"Spark API key file {path} is unreadable ({exc.strerror or type(exc).__name__}); "
            f"Spark models need it (override the path with {KEY_FILE_ENV})"
        ) from None
    if not key:
        raise SparkUnavailableError(f"Spark API key file {path} is empty")
    return key


def spark_backend_env(
    model: str,
    existing_env: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Env entries that select the Spark backend for *model*.

    Only keys absent from *existing_env* are returned, so an explicit per-session
    value (AgentTask ``env``, e.g. ``spark-env qwen direct``) always wins and
    sessions launched the old env-only way behave exactly as before. The key
    file is read only when no credential is already present.
    """
    served = _served_name(model)
    result: dict[str, str] = {}
    if "ANTHROPIC_BASE_URL" not in existing_env:
        result["ANTHROPIC_BASE_URL"] = spark_base_url(environ)
    if not any(key in existing_env for key in _AUTH_KEYS):
        result["ANTHROPIC_AUTH_TOKEN"] = read_spark_key(environ)
    # Claude Code's small/fast side requests default to a Haiku name the Spark
    # does not serve; point them (and only them) at the served model.
    for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
        if key not in existing_env:
            result[key] = served
    return result


def spark_provider_window(table_window: int, context_tokens: int) -> int:
    """Provider window used for the compaction ceiling of a Spark model.

    The table value is the default profile's real window (262,144 native). An
    explicit larger suffix (``[1m]``) declares the ``qwen-1m`` profile, so the
    budget itself is the window; ``check_spark_available`` verifies that at spawn.
    """
    return max(table_window, context_tokens)


def check_spark_available(
    model: str,
    context_tokens: int,
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float = 4.0,
) -> None:
    """Fail clearly if the Spark endpoint serves a different model or a smaller window.

    Called when an AgentTask child is created (never for resumes/forks of a
    running session). Read-only ``GET /v1/models`` through the Spark cache proxy.
    Fails *open* when the endpoint cannot be reached or answers unexpectedly
    (mid-switch, down): that case surfaces as the ordinary connection error.
    """
    environ = os.environ if environ is None else environ
    if (environ.get(PREFLIGHT_DISABLE_ENV) or "").strip() == "0" or not is_spark_model(model):
        return
    served = _served_name(model)
    request = urllib.request.Request(
        spark_base_url(environ) + "/v1/models",
        headers={"Authorization": f"Bearer {read_spark_key(environ)}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        cards = [card for card in payload.get("data", []) if isinstance(card, dict)]
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as exc:
        logger.warning("Spark preflight skipped (endpoint not answering /v1/models): %s", type(exc).__name__)
        return
    ids = [str(card.get("id")) for card in cards if card.get("id")]
    if not ids:
        return
    match = next((card for card in cards if card.get("id") == served), None)
    if match is None:
        raise SparkUnavailableError(
            f"{served} is not the model the Sparks are serving right now "
            f"(serving: {', '.join(ids)}). The pair hosts one model at a time: "
            "switch with `spark-model switch qwen|qwen-1m|glm` (take /data/runtime/spark.lock first; "
            "about 8 min for Qwen, 15 for GLM) or pick the served model."
        )
    window = match.get("max_model_len")
    if isinstance(window, int) and 0 < window < context_tokens:
        raise SparkUnavailableError(
            f"{served} is serving a {window:,}-token window but {context_tokens:,} tokens were requested. "
            "Use a smaller suffix (default 262k) or switch to the ~1M profile: "
            "`spark-model switch qwen-1m` (Qwen only)."
        )
