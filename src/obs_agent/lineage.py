"""Canonical lineage/bootstrap helpers for multi-level Telegram agent trees."""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from obs_agent.context_jsonl import find_session_jsonl

_OBS_BOOTSTRAP_RE = re.compile(
    r"(?s)^\s*(<obs-bootstrap\b[^>]*>.*?</obs-bootstrap>)"
)
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class ObsBootstrap:
    """Parsed canonical bootstrap payload."""

    raw_xml: str
    lineage: tuple[str, ...]
    origin: str
    is_fork: bool
    session_id: str | None
    agent_id: str | None
    parent_session_id: str | None
    root_team_key: str | None
    native_agent_name: str | None


def normalize_lineage_name(value: str | None) -> str:
    """Collapse whitespace and trim a lineage node label."""
    normalized = " ".join((value or "").split()).strip()
    return normalized


def slugify_projection_label(value: str | None, *, fallback: str) -> str:
    """Build a filesystem-safe slug for native team/inbox projection."""
    normalized = normalize_lineage_name(value)
    slug = _NON_ALNUM_RE.sub("-", normalized).strip("-").lower()
    return slug or fallback


def lineage_fingerprint(lineage: Sequence[str]) -> str:
    """Return a short stable digest for a lineage."""
    digest = hashlib.sha1("\x1f".join(lineage).encode("utf-8")).hexdigest()
    return digest[:10]


def root_team_key_for_lineage(
    lineage: Sequence[str],
    *,
    timestamp: float | None = None,
) -> str:
    """Project a lineage tree onto a timestamp-based native team key.

    Format: ``YYYY-MM-DD-HH-MM-{slug}``

    *timestamp* defaults to the current UTC time.  Passing an explicit value
    makes the result deterministic (useful for tests and for idempotent
    restores from persisted state).
    """
    if not lineage:
        return "0000-00-00-00-00-root"
    root = normalize_lineage_name(lineage[0])
    slug = slugify_projection_label(root, fallback="root")
    ts = timestamp if timestamp is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    prefix = dt.strftime("%Y-%m-%d-%H-%M")
    return f"{prefix}-{slug}"


def native_agent_name_for_lineage(lineage: Sequence[str]) -> str:
    """Project a lineage member onto one safe native agent name.

    Two-tier naming:
    - **Trunk** (single-element lineage): just the slug, no prefix.
      E.g. ``("My Topic",)`` → ``"my-topic"``
    - **Child** (multi-element lineage): ``{parent_lineage_hash}-{slug}``.
      E.g. ``("Root", "Worker")`` → ``"{hash_of_Root}-worker"``

    The parent hash is the fingerprint of the lineage *up to but not including*
    the leaf, ensuring siblings under the same parent share a prefix while
    same-named children under different parents get unique names.
    """
    if not lineage:
        return "root"
    leaf = normalize_lineage_name(lineage[-1])
    slug = slugify_projection_label(leaf, fallback="node")
    if len(lineage) == 1:
        # Trunk: no hash prefix
        return slug
    # Child: prefix with parent lineage hash
    parent_lineage = tuple(normalize_lineage_name(n) for n in lineage[:-1])
    parent_hash = lineage_fingerprint(parent_lineage)
    return f"{parent_hash}-{slug}"


def build_obs_bootstrap_xml(
    *,
    lineage: Sequence[str],
    origin: str,
    is_fork: bool,
    session_id: str | None,
    agent_id: str | None = None,
    parent_session_id: str | None = None,
    root_team_key: str | None = None,
    native_agent_name: str | None = None,
) -> str:
    """Serialize the canonical OBS bootstrap XML envelope.

    Each ``obs-node`` element carries both a human-readable ``name`` and the
    machine-safe ``agent_name`` that can be used for messaging.
    """
    root = ET.Element("obs-bootstrap", {"version": "1"})
    lineage_el = ET.SubElement(root, "obs-lineage")
    normalized = [normalize_lineage_name(n) for n in lineage]
    for idx, node_name in enumerate(normalized):
        attrs: dict[str, str] = {"name": node_name}
        # Compute the agent_name for the sub-lineage up to and including this node
        sub_lineage = tuple(normalized[: idx + 1])
        attrs["agent_name"] = native_agent_name_for_lineage(sub_lineage)
        ET.SubElement(lineage_el, "obs-node", attrs)

    fork_context = ET.SubElement(root, "fork_context")
    ET.SubElement(fork_context, "origin").text = origin
    ET.SubElement(fork_context, "is_fork").text = "true" if is_fork else "false"
    if agent_id:
        ET.SubElement(fork_context, "agent_id").text = agent_id
    if session_id:
        ET.SubElement(fork_context, "session_id").text = session_id
    if parent_session_id:
        ET.SubElement(fork_context, "parent_session_id").text = parent_session_id

    team_context = ET.SubElement(root, "team_context")
    if root_team_key:
        ET.SubElement(team_context, "root_team_key").text = root_team_key
    if native_agent_name:
        ET.SubElement(team_context, "native_agent_name").text = native_agent_name

    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def extract_obs_bootstrap_xml(text: str | None) -> str | None:
    """Return the bootstrap block when the text begins with one."""
    if not text:
        return None
    match = _OBS_BOOTSTRAP_RE.match(text)
    if match is None:
        return None
    return match.group(1)


def parse_obs_bootstrap_xml(xml_text: str) -> ObsBootstrap:
    """Parse a canonical OBS bootstrap XML block."""
    root = ET.fromstring(xml_text)
    if root.tag != "obs-bootstrap":
        raise ValueError(f"Unexpected root tag: {root.tag}")

    lineage_el = root.find("obs-lineage")
    lineage: list[str] = []
    if lineage_el is not None:
        for node in lineage_el.findall("obs-node"):
            lineage.append(normalize_lineage_name(node.attrib.get("name")))

    fork_context = root.find("fork_context")
    team_context = root.find("team_context")

    def _child_text(parent: ET.Element | None, child_name: str) -> str | None:
        if parent is None:
            return None
        child = parent.find(child_name)
        if child is None or child.text is None:
            return None
        text = child.text.strip()
        return text or None

    is_fork_text = (_child_text(fork_context, "is_fork") or "").lower()
    return ObsBootstrap(
        raw_xml=xml_text,
        lineage=tuple(item for item in lineage if item),
        origin=_child_text(fork_context, "origin") or "unknown",
        is_fork=is_fork_text in {"true", "1", "yes", "on"},
        session_id=_child_text(fork_context, "session_id"),
        agent_id=_child_text(fork_context, "agent_id"),
        parent_session_id=_child_text(fork_context, "parent_session_id"),
        root_team_key=_child_text(team_context, "root_team_key"),
        native_agent_name=_child_text(team_context, "native_agent_name"),
    )


def _message_text_blocks(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                yield text


def _entry_candidate_texts(entry: dict[str, Any]) -> Iterable[str]:
    entry_type = str(entry.get("type") or "").strip().lower()
    if entry_type == "queue-operation":
        content = entry.get("content")
        if isinstance(content, str):
            yield content
        return

    if entry_type == "system":
        content = entry.get("content")
        if isinstance(content, str):
            yield content
        return

    message = entry.get("message")
    if not isinstance(message, dict):
        return
    role = str(message.get("role") or "").strip().lower()
    if role != "user":
        return
    yield from _message_text_blocks(message.get("content"))


def find_latest_obs_bootstrap_in_jsonl(path: Path) -> ObsBootstrap | None:
    """Scan a transcript JSONL for the latest canonical bootstrap."""
    latest_xml: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            for text in _entry_candidate_texts(entry):
                bootstrap_xml = extract_obs_bootstrap_xml(text)
                if bootstrap_xml:
                    latest_xml = bootstrap_xml
    if latest_xml is None:
        return None
    return parse_obs_bootstrap_xml(latest_xml)


def find_latest_obs_bootstrap_for_session(
    *,
    session_id: str | None,
    cwd: str | Path,
) -> ObsBootstrap | None:
    """Resolve a session JSONL and parse its latest bootstrap block."""
    if not session_id:
        return None
    path = find_session_jsonl(session_id=session_id, cwd=Path(cwd))
    if path is None or not path.exists():
        return None
    return find_latest_obs_bootstrap_in_jsonl(path)

