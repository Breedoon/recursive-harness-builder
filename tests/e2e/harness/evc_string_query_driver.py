#!/usr/bin/env python3
"""Real pinned-SDK string query component; declared policy-off, not a normal OBS route."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import shlex
import time
from pathlib import Path


async def exercise(root: Path):
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock, query
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from obs_agent.config import OBSConfig
    from obs_agent.session import SessionManager
    from tests.e2e.harness.evc_fixture import record, snapshot

    manager = SessionManager(config=OBSConfig.from_env())
    manager.model_override = "gpt-6.1-sol"
    manager.effort_override = "low"
    manager.set_sdk_env_overrides({"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"})
    base_options = manager._build_options()
    assert base_options.model.startswith("gpt-6.1-sol"), "never allow a default Claude model"
    options = dataclasses.replace(base_options, hooks={})
    command = shlex.join(["python3", "./evc_fixture.py", "hold", "--root", str(root)])
    prompt = ("Isolated lifecycle component only; no exploration, registration, AgentTask, messaging or other tools. "
              f"Call Bash exactly once with command `{command}`, timeout120000 and run_in_background=true. "
              "When it returns, reply exactly COMPONENT_DONE.")
    transport = SubprocessCLITransport(prompt=prompt, options=options)
    proof = {"input_type": "str", "model": options.model, "component_query_started_epoch": time.time(),
             "normal_OBS_entry": "UNAVAILABLE: pinned ForkRunner.run is the only production string-query definition; static callers absent",
             "declared_policy": "P6 decision(b): SDK PreToolUse callbacks OFF; existing project settings retained",
             "sdk_hook_names": list(options.hooks or {}), "sdk_mcp_names": sorted(options.mcp_servers),
             "component_not_reference_parity": "CLI parent is this driver, not OBS daemon; normal option factory with explicit Sol/env0 and SDK hooks removed; RC void-rule judgment external",
             "EOF_time": "not directly instrumented; unmodified SDK internal/client.py133-134 writes str then end_input before yielding messages; epoch below is actual QUERY END"}
    messages, calls = [], []
    result = None
    record(root, "string_query_begin", model=options.model, input_type="str", SDK_hooks_off=True)
    async for message in query(prompt=prompt, options=options, transport=transport):
        messages.append(type(message).__name__)
        if "target_cli_pid" not in proof:
            pid = transport._process.pid
            proof["target_cli_pid"] = pid
            proof["query_first_message_epoch"] = time.time()
            proof["configuration_before_turn"] = snapshot(root)
            record(root, "string_query_first_message", cli_pid=pid)
        if isinstance(message, SystemMessage) and message.subtype == "init":
            proof["session_id"] = message.data.get("session_id")
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    assert block.name == "Bash", "component must not delegate or contact anything"
                    calls.append({"id": block.id, "name": block.name,
                                  "input": {key: block.input.get(key) for key in ("command", "timeout", "run_in_background")}})
        if isinstance(message, ResultMessage):
            result = {"subtype": message.subtype, "is_error": message.is_error, "num_turns": message.num_turns}
    proof["component_query_end_epoch"] = time.time()
    record(root, "string_query_end", cli_pid=proof.get("target_cli_pid"))
    proof.update(message_types=messages, actual_tool_calls=calls, result=result)
    assert len(calls) == 1 and calls[0]["input"]["run_in_background"] is True, calls
    assert result and result["is_error"] is False, result
    proof["pinned_cli_sha256"] = hashlib.sha256(Path(transport._cli_path).read_bytes()).hexdigest()
    assert proof["pinned_cli_sha256"] == "7a4a653982b07e0a8157f8d3b2c2f8e442520ab07b2fa2e692ba054dbba210c9"
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(exercise(args.root))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
