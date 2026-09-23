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
# The Qwen chat template writes an effort sentence into the system head for
# xhigh (and for low while thinking is on). On this hybrid GDN model vLLM can
# only resume from the previous prompt's end state, so a changed head recomputes
# the whole conversation, and a sentence placed anywhere outside the history
# blocks reuse on every turn (measured 2026-09-23). low (thinking off) and
# medium render identical prompts, so every level above low maps to medium and
# /effort changes never cost a reprefill.
QWEN_EFFORT_MAPPING = {
    "low": "low",
    "medium": "medium",
    "high": "medium",
    "xhigh": "medium",
    "max": "medium",
}
# Claude models the bundled Claude Code (2.1.59) does not recognize as
# effort-capable: it sends no output_config for them, so /effort was a no-op.
# All five levels are accepted by the API (verified live 2026-09-23 on
# claude-opus-5-5).
CLAUDE_BODY_EFFORT_PREFIXES = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-7", "claude-opus-4-8",
)
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
    if clean_model.startswith("local-qwen"):
        output_config = body.get("output_config", {})
        if not isinstance(output_config, dict):
            raise ValueError("CLAUDE_CODE_EXTRA_BODY.output_config must be an object")
        body["output_config"] = {
            **output_config, "effort": QWEN_EFFORT_MAPPING[effort]
        }
        if effort == "low":
            chat_template_kwargs = body.get("chat_template_kwargs", {})
            if not isinstance(chat_template_kwargs, dict):
                raise ValueError("CLAUDE_CODE_EXTRA_BODY.chat_template_kwargs must be an object")
            body["chat_template_kwargs"] = {
                **chat_template_kwargs,
                "enable_thinking": chat_template_kwargs.get("enable_thinking", False),
            }
        result[EXTRA_BODY_ENV] = json.dumps(body, separators=(",", ":"))
        return result
    if clean_model.startswith(CLAUDE_BODY_EFFORT_PREFIXES):
        # Only output_config: the CLI's thinking block (enabled + budget) is
        # left alone, because changing thinking parameters invalidates the
        # message cache and effort already governs thinking depth under it.
        # The cache proxy strips the field from side requests to models that
        # reject it (Haiku).
        if effort == "high" and "output_config" not in body:
            # high is the API default. An explicit value is cached separately
            # from an omitted one once thinking is in the history (measured
            # 2026-09-23), so omit it: default sessions keep today's request
            # bytes and the deploy costs them no cache miss.
            return result
        output_config = body.get("output_config", {})
        if not isinstance(output_config, dict):
            raise ValueError("CLAUDE_CODE_EXTRA_BODY.output_config must be an object")
        body["output_config"] = {**output_config, "effort": effort}
        result[EXTRA_BODY_ENV] = json.dumps(body, separators=(",", ":"))
        return result
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
