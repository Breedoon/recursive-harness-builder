"""Launch-only HTTP decoder attachment for the pinned Bun-based CLI.

CACHE-PREFIX WARNING: preload, worker and external decoder bytes are one release identity.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex

DECODER_NODE = Path("/workspace/runtime/security-obs21/node/node-v22.23.4-linux-x64/bin/node")
DECODER_SHA256 = "94efd062ed066c1a0b332e18249859c905e920eeada5df55d24c99b919629a90"
PRELOAD = Path(__file__).with_name("bun_transport_preload.cjs")


def decoder_identity() -> dict:
    """Hash actual external bytes, not merely the expected hash in the source."""
    # INVARIANT: admission binds the same external binary that the preload verifies.
    # Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
    try:
        with DECODER_NODE.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        digest = None
    return {"path": str(DECODER_NODE), "sha256": digest, "expected_sha256": DECODER_SHA256}


def launch_environment(environment: dict[str, str]) -> dict[str, str]:
    # INVARIANT: attach before CLI captures fetch/zlib; every process creation uses
    # this same env-only path, never prompt, settings, tool or MCP description bytes.
    # Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
    options = shlex.split(environment.get("BUN_OPTIONS", os.environ.get("BUN_OPTIONS", "")))
    attachment = ["--preload", str(PRELOAD)]
    if options[:2] != attachment:
        options = attachment + options
    return {
        "BUN_OPTIONS": shlex.join(options),
        "OBS_BUN_DECODER_NODE": str(DECODER_NODE),
        "OBS_BUN_DECODER_SHA256": DECODER_SHA256,
    }
