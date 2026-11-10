#!/usr/bin/env python3
"""Model-free SDK exercise of the declared E3 protocol stub, NEVER obs-cli evidence."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path

DECLARATION = "EV-C DECLARED E3 STUB; NOT OBS-CLI"


async def exercise(cli: Path, label: str) -> dict:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock
    options = ClaudeAgentOptions(cli_path=str(cli), model="gpt-6.1-sol",
        cwd="/workspace/runtime/git/obs-vault-active", setting_sources=[], mcp_servers={})
    types, texts = [], []
    result = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"Declared protocol fixture only: {label}")
        async for message in client.receive_response():
            types.append(type(message).__name__)
            if isinstance(message, AssistantMessage):
                texts.extend(block.text for block in message.content if isinstance(block, TextBlock))
            if isinstance(message, ResultMessage):
                result = {"subtype": message.subtype, "is_error": message.is_error,
                          "num_turns": message.num_turns, "total_cost_usd": message.total_cost_usd}
        await client.interrupt()
    assert texts == [DECLARATION], texts
    assert result and result["subtype"] == "success" and result["is_error"] is False, result
    return {"label": label, "declaration": DECLARATION, "cli_path": str(cli),
            "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
            "sdk_version": importlib.metadata.version("claude-agent-sdk"), "message_types": types,
            "assistant_text": texts, "result": result, "interrupt_control_acknowledged": True,
            "raw_status": "NEEDS-OBS-CLI", "runtime_provenance": "DECLARED_E3_PROTOCOL_STUB_ONLY",
            "limits": ["No actual replacement binary or OBS runtime-selection path exercised.",
                       "No shape, lifecycle outcome, provider or reference-configuration parity inferred.",
                       "The declared fixture imports no network/provider client; SDK talks to this local process only."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(exercise(args.cli, args.label))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
