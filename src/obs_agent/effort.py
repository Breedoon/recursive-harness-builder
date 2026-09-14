"""Effort policy and the Claude Code -> CLIProxyAPI request boundary.

No process-global environment mutation: independent sessions may use different
levels. Provider capabilities remain authoritative; xhigh and max are distinct.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from obs_agent.config import MODEL_EFFORT_LEVELS, is_claude_model, resolve_model_context

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
EXTRA_BODY_ENV = "CLAUDE_CODE_EXTRA_BODY"
EFFORT_USAGE = "/effort [low|medium|high|xhigh|max|auto]"


def normalize_effort(value: str) -> str:
    """Validate before storing or launching. auto selects the model default."""
    if not isinstance(value, str):
        raise ValueError("Effort must be a string: low, medium, high, xhigh, max, or auto")
    value = value.strip().lower()
    if value not in (*EFFORT_LEVELS, "auto"):
        raise ValueError("Effort must be low, medium, high, xhigh, max, or auto")
    return value


def resolve_effort(
    model: str,
    *,
    override: str | None = None,
    configured: str | None = None,
    model_defaults: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    session_env: Mapping[str, str] | None = None,
) -> str:
    """Session selection > session env > OBS setting > shell env > model default.

    An explicit auto bypasses lower-priority overrides and uses the model's
    configured default, falling back to provider auto for unlisted models.
    """
    clean_model = resolve_model_context(model).model.lower()
    default = (model_defaults or {}).get(
        clean_model, MODEL_EFFORT_LEVELS.get(clean_model, "auto")
    )
    selected = next((value for value in (
        override, (session_env or {}).get(EFFORT_ENV), configured,
        (environ or {}).get(EFFORT_ENV),
    ) if value is not None and value != ""), "auto")
    selected = normalize_effort(selected)
    return normalize_effort(default) if selected == "auto" else selected


def parse_extra_body(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("CLAUDE_CODE_EXTRA_BODY must be a valid JSON object") from exc
    if not isinstance(body, dict):
        raise ValueError("CLAUDE_CODE_EXTRA_BODY must be a JSON object")
    return body


def build_effort_env(
    model: str, effort: str, environ: Mapping[str, str]
) -> dict[str, str]:
    """Set native effort and bridge hosted non-Claude models via extra-body JSON.

    CLIProxyAPI's Claude decoder reads output_config.effort with adaptive
    thinking, then translates it to reasoning_effort (Chat Completions) or
    reasoning.effort (Responses/Codex). A native env var alone is insufficient
    for model IDs Claude Code does not recognize as effort-capable.
    """
    effort = normalize_effort(effort)
    result = {EFFORT_ENV: effort}
    body = parse_extra_body(environ.get(EXTRA_BODY_ENV))
    if effort == "auto":
        # No harness-injected fields to remove: generated bodies are never saved
        # back to session_env. Respect an operator's raw provider configuration.
        return result
    clean_model = resolve_model_context(model).model.lower()
    is_proxy_model = not is_claude_model(clean_model) and not clean_model.startswith("local-")
    thinking = body.get("thinking", {})
    if not isinstance(thinking, dict):
        raise ValueError("CLAUDE_CODE_EXTRA_BODY.thinking must be an object")
    disabled = (
        thinking.get("type") == "disabled"
        or "temperature" in body
        or str(environ.get("MAX_THINKING_TOKENS", "")).strip() == "0"
        or str(environ.get("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING", "")).lower() in ("1", "true")
    )
    if is_proxy_model and not disabled:
        thinking = dict(thinking)
        thinking.pop("budget_tokens", None)
        thinking["type"] = "adaptive"
        body["thinking"] = thinking
    # Do not re-enable disabled thinking or change temperature behavior. Native
    # Claude effort can also control non-thinking output; the CLI handles that.
    if (is_proxy_model and not disabled) or (
        not is_proxy_model and "output_config" in body
    ):
        output_config = body.get("output_config", {})
        if not isinstance(output_config, dict):
            raise ValueError("CLAUDE_CODE_EXTRA_BODY.output_config must be an object")
        body["output_config"] = {**output_config, "effort": effort}
        result[EXTRA_BODY_ENV] = json.dumps(body, separators=(",", ":"))
    return result


def child_effort_override(
    requested: str | None, *, parent_effort: str, model: str | None,
    session_env: Mapping[str, str] | None = None,
) -> str | None:
    """Same-model children inherit; an explicitly selected model gets its default.

    Resolve env-provided selections into durable per-session metadata as well,
    because general SDK environment overrides are not persisted by Telegram.
    """
    if requested is not None:
        if isinstance(requested, str) and requested.strip().lower() == "inherit":
            return parent_effort
        return normalize_effort(requested)
    if value := (session_env or {}).get(EFFORT_ENV):
        return normalize_effort(value)
    return None if model else parent_effort
