#!/usr/bin/env python3
"""DECLARED E3 stream-json protocol fixture. NOT obs-cli and NOT a runtime parity proof.

Frame types/shapes come only from protocol-capture/2159-20261021 raw stdin/stdout:
control_request/initialize and interrupt, control_response/success, system/init,
assistant/text and result/success. No provider, tools, hooks or OS-lifecycle model.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

DECLARATION = "EV-C DECLARED E3 STUB; NOT OBS-CLI"
CAPTURE = "/workspace/runtime/e2e/fixtures/protocol-capture/2159-20261021/raw/20261021T194940880915689-600037/"


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if "--version" in sys.argv:
        print("2.1.59 " + DECLARATION)
        return 0
    session = str(uuid.uuid4())
    initialized = False
    for line in sys.stdin:
        data = json.loads(line)
        if data.get("type") == "control_request":
            request = data["request"]
            if request["subtype"] not in {"initialize", "interrupt"}:
                emit({"type": "control_response", "response": {"subtype": "error", "request_id": data["request_id"],
                      "error": "Declared E3 fixture implements initialize and interrupt only; not runtime evidence."}})
                continue
            payload = {"commands": [], "models": [], "account": {}, "pid": os.getpid()} if request["subtype"] == "initialize" else {}
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": data["request_id"], "response": payload}})
        elif data.get("type") == "user":
            if not initialized:
                emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": session,
                      "tools": [], "mcp_servers": [], "model": "E3-CAPTURE-STUB-NOT-OBS-CLI",
                      "permissionMode": "bypassPermissions", "claude_code_version": "2.1.59", "uuid": str(uuid.uuid4())})
                initialized = True
            emit({"type": "assistant", "message": {"id": "stub-" + uuid.uuid4().hex,
                  "type": "message", "role": "assistant", "model": "E3-CAPTURE-STUB-NOT-OBS-CLI",
                  "content": [{"type": "text", "text": DECLARATION}], "stop_reason": "end_turn", "usage": {}},
                  "parent_tool_use_id": None, "session_id": session, "uuid": str(uuid.uuid4())})
            emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 0,
                  "duration_api_ms": 0, "num_turns": 1, "session_id": session,
                  "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}, "result": DECLARATION})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
