"""Which model a fork child runs on (Daniel 2026-10-15, session_source fix).

CACHE-PREFIX WARNING: a fork only reuses the source's prompt cache when it runs on the source's
own model and provider. Running it on any other model silently re-bills the whole copied context.

Rules (general, not a reflection special case):

* The *source* of a fork is the session whose history is copied: the explicit ``session_source``
  when given, otherwise the launcher's own head.
* No ``model`` (or ``inherit`` / ``source``) -> the child runs on the source session's own model
  and effort. Before this fix an explicit ``session_source`` silently ran on the *launcher's*
  model (a GLM watcher forking an Opus transcript ran on GLM: total cache miss).
* An explicit ``model`` is honoured when it is in the source's model family.
* An explicit model from another family (Claude / GPT / local) is refused loudly: the copied
  transcript is in the source provider's format and its cache lives with that provider.
* A source whose model cannot be determined is refused loudly; never a silent fallback.

Source model lookup: the live route that owns the source session (keeps context suffix and
effort), else the source JSONL's last real assistant ``message.model`` with the model's
configured default effort.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from obs_agent.cache_readiness import model_family

_TAIL = 2 * 1024 * 1024
INHERIT_WORDS = {"", "inherit", "source"}


class ForkModelError(ValueError):
    pass


@dataclass(frozen=True)
class SourceModel:
    model: str
    effort: str | None
    origin: str  # "route" | "jsonl" | "launcher"


def jsonl_model(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as fh:
            if size > _TAIL:
                fh.seek(size - _TAIL)
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"assistant"' not in line or '"model"' not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return None


def choose(requested: str | None, source: SourceModel, *, source_label: str) -> tuple[str, bool]:
    """(model to run, whether it is the source's own model). Raises ForkModelError."""
    raw = (requested or "").strip()
    if raw.lower() in INHERIT_WORDS:
        return source.model, True
    source_family = model_family(source.model)
    requested_family = model_family(raw)
    if source_family and requested_family and source_family != requested_family:
        raise ForkModelError(
            f"cannot fork {source_label} (model {source.model}, family {source_family}) onto "
            f"model {raw} (family {requested_family}): the copied history and its prompt cache "
            "belong to the source provider. Omit model (the fork then runs on the source's model), "
            "or use fork=false for a fresh session on another model."
        )
    return raw, raw == source.model
