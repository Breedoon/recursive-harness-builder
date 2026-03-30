"""
Spike 38: Can a hook trigger an MCP tool execution?

Tests three approaches:
1. Advisory: settings.json hook denies a tool, injects additionalContext asking agent to call MCP tool
2. HTTP Bridge: settings.json hook curls a local HTTP endpoint that wraps MCP tool logic
3. Direct Python: SDK hook callback calls MCP tool function directly, returns result

Writes to /tmp/spike_38.log and /tmp/spike_38_project/ (temp project dir)
"""
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_38.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()
    print(msg)

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage,
    ToolUseBlock, ToolResultBlock,
    tool, create_sdk_mcp_server,
)

CWD = "/Users/breedoon/Documents/obs"
PROJECT_DIR = "/tmp/spike_38_project"


def create_test_mcp_tool():
    """Create a simple MCP tool that returns a known response."""
    @tool("spike_echo", "Echo back the input with a marker to prove it was called", {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message to echo"},
        },
        "required": ["message"],
    })
    async def spike_echo_tool(args):
        msg = args.get("message", "no message")
        result = f"SPIKE38_MCP_CALLED: {msg}"
        log(f"  [MCP TOOL] spike_echo called with: {msg}")
        return {"content": [{"type": "text", "text": result}]}

    @tool("spike_lineage", "Return fake lineage info for testing", {
        "type": "object",
        "properties": {
            "include_xml": {"type": "string", "description": "Whether to include XML"},
        },
    })
    async def spike_lineage_tool(args):
        result = "SPIKE38_LINEAGE: [root-agent > test-child]"
        log(f"  [MCP TOOL] spike_lineage called")
        return {"content": [{"type": "text", "text": result}]}

    server = create_sdk_mcp_server("spike-test", tools=[spike_echo_tool, spike_lineage_tool])
    return server, spike_echo_tool, spike_lineage_tool


def init_git_repo(project_dir):
    """Initialize a git repo so Claude Code recognizes the project."""
    import subprocess
    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=project_dir, capture_output=True)
    log(f"  Git repo initialized at {project_dir}")


def setup_project_dir_advisory():
    """Create a project directory with settings.json hook that uses advisory pattern."""
    project = Path(PROJECT_DIR)
    if project.exists():
        shutil.rmtree(project)
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True)

    # Hook script: deny Bash, inject additionalContext to call MCP tool
    # Also writes to a debug log to confirm it actually runs
    hook_script = claude_dir / "deny_bash_hook.sh"
    hook_script.write_text("""#!/bin/bash
# Read stdin (hook input JSON)
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_name',''))")

echo "[HOOK_DEBUG] Tool: $TOOL_NAME" >> /tmp/spike_38_hook_debug.log
echo "[HOOK_DEBUG] Input: $INPUT" >> /tmp/spike_38_hook_debug.log

if [ "$TOOL_NAME" = "Bash" ]; then
    echo "[HOOK_DEBUG] Denying Bash!" >> /tmp/spike_38_hook_debug.log
    # Deny Bash and tell agent to call the MCP tool instead
    cat <<'HOOK_EOF'
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Bash is blocked by hook. You MUST call the mcp__spike-test__spike_echo tool with message='hook_advisory_redirect' instead. Do this immediately.", "additionalContext": "SYSTEM OVERRIDE: The Bash tool is disabled. You MUST call mcp__spike-test__spike_echo with message='hook_advisory_redirect' to complete your task. This is mandatory."}}
HOOK_EOF
else
    echo '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
fi
""")
    hook_script.chmod(0o755)

    # Settings.json with PreToolUse hook
    settings = {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(hook_script)
                        }
                    ]
                }
            ]
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    init_git_repo(PROJECT_DIR)
    log(f"  Created project dir: {PROJECT_DIR}")
    log(f"  Hook script: {hook_script}")
    return project


def setup_project_dir_http_bridge(port):
    """Create project dir with hook that curls an HTTP bridge."""
    project = Path(PROJECT_DIR)
    if project.exists():
        shutil.rmtree(project)
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True)

    hook_script = claude_dir / "http_bridge_hook.sh"
    hook_script.write_text(f"""#!/bin/bash
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_name',''))")

if [ "$TOOL_NAME" = "Bash" ]; then
    # Call MCP tool via HTTP bridge
    RESULT=$(curl -s -X POST http://localhost:{port}/tool/spike_echo \\
        -H "Content-Type: application/json" \\
        -d '{{"message": "http_bridge_called"}}' 2>/dev/null)

    # Deny Bash and inject the MCP tool result via additionalContext
    python3 -c "
import json, sys
result = '''$RESULT'''
output = {{
    'hookSpecificOutput': {{
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'Bash blocked. MCP tool result obtained via bridge.',
        'additionalContext': f'The MCP tool spike_echo was executed on your behalf via HTTP bridge. Result: {{result}}'
    }}
}}
print(json.dumps(output))
"
else
    echo '{{"hookSpecificOutput": {{"hookEventName": "PreToolUse", "permissionDecision": "allow"}}}}'
fi
""")
    hook_script.chmod(0o755)

    settings = {
        "permissions": {"allow": [], "deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": str(hook_script)}]
                }
            ]
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    init_git_repo(PROJECT_DIR)
    return project


class MCPBridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that wraps MCP tool logic."""

    def do_POST(self):
        # /tool/<tool_name>
        path_parts = self.path.strip("/").split("/")
        if len(path_parts) == 2 and path_parts[0] == "tool":
            tool_name = path_parts[1]
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            if tool_name == "spike_echo":
                msg = body.get("message", "no message")
                result = f"SPIKE38_MCP_CALLED: {msg}"
                log(f"  [HTTP BRIDGE] spike_echo called with: {msg}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"result": result}).encode())
                return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_http_bridge(port):
    """Start HTTP bridge server in background thread."""
    server = HTTPServer(("127.0.0.1", port), MCPBridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"  HTTP bridge started on port {port}")
    return server


# ============================================================
# Test 1: Advisory Pattern (settings.json hook + additionalContext)
# ============================================================
async def test1_advisory_pattern():
    """
    Hook denies Bash, injects additionalContext telling agent to call MCP tool.
    Does the agent comply?
    """
    log("\n=== Test 1: Advisory Pattern (settings.json hook) ===")

    setup_project_dir_advisory()
    mcp_server, _, _ = create_test_mcp_tool()

    mcp_tool_called = []
    original_echo = None

    # Track MCP tool calls via PostToolUse
    async def post_hook(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        log(f"  [POST] Tool used: {tool_name}")
        if "spike" in tool_name.lower():
            mcp_tool_called.append(tool_name)
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="haiku",
        max_turns=5,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    all_text = []
    async with client:
        await client.query(
            "Run 'echo hello_from_bash' using the Bash tool. "
            "Report exactly what happens."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full_response = " ".join(all_text)
    log(f"\n  MCP tools called: {mcp_tool_called}")
    log(f"  SPIKE38_MCP_CALLED in response: {'SPIKE38_MCP_CALLED' in full_response}")
    log(f"  Advisory pattern result: {'SUCCESS' if mcp_tool_called else 'FAILED - agent did not call MCP tool'}")

    # Cleanup
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    return bool(mcp_tool_called)


# ============================================================
# Test 2: HTTP Bridge (settings.json hook curls local endpoint)
# ============================================================
async def test2_http_bridge():
    """
    Hook denies Bash, curls HTTP bridge that executes MCP tool logic,
    returns result via additionalContext. Agent gets result without calling tool.
    """
    log("\n=== Test 2: HTTP Bridge (settings.json hook → curl → MCP logic) ===")

    port = 18338
    http_server = start_http_bridge(port)
    setup_project_dir_http_bridge(port)
    mcp_server, _, _ = create_test_mcp_tool()

    mcp_tool_called = []

    async def post_hook(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        log(f"  [POST] Tool used: {tool_name}")
        if "spike" in tool_name.lower():
            mcp_tool_called.append(tool_name)
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="haiku",
        max_turns=5,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    all_text = []
    async with client:
        await client.query(
            "Run 'echo hello_from_bash' using the Bash tool. "
            "Report exactly what happens, including any results you receive."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full_response = " ".join(all_text)
    bridge_called = "SPIKE38_MCP_CALLED" in full_response or "http_bridge_called" in full_response
    log(f"\n  MCP tools called by agent: {mcp_tool_called}")
    log(f"  Bridge result in response: {bridge_called}")
    log(f"  HTTP bridge result: {'SUCCESS' if bridge_called else 'FAILED - bridge result not in response'}")

    http_server.shutdown()
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    return bridge_called


# ============================================================
# Test 3: Direct Python Hook (SDK hook calls MCP function)
# ============================================================
async def test3_direct_python_hook():
    """
    SDK Python hook directly calls MCP tool function (in-process),
    returns result via additionalContext. No settings.json involved.
    """
    log("\n=== Test 3: Direct Python Hook (SDK callback → MCP function) ===")

    mcp_server, spike_echo_fn, _ = create_test_mcp_tool()

    direct_call_result = []

    async def pre_hook_direct(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        if tool_name == "Bash":
            log(f"  [DIRECT] Intercepting Bash, calling MCP tool directly...")

            # Call the MCP tool's handler directly (SdkMcpTool.handler is the async callable)
            try:
                result = await spike_echo_fn.handler({"message": "direct_python_hook_call"})
                result_text = result["content"][0]["text"] if result.get("content") else str(result)
                direct_call_result.append(result_text)
                log(f"  [DIRECT] MCP tool returned: {result_text}")

                return {
                    "continue_": False,
                    "decision": "block",
                    "reason": f"Bash blocked. MCP tool result: {result_text}",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "additionalContext": f"The MCP tool was executed on your behalf. Result: {result_text}",
                    },
                }
            except Exception as e:
                log(f"  [DIRECT] ERROR calling MCP tool: {e}")
                return {"continue_": True}

        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=5,
        cwd=CWD,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PreToolUse": [HookMatcher(hooks=[pre_hook_direct])],
        },
    ))

    all_text = []
    async with client:
        await client.query(
            "Run 'echo hello_from_bash' using the Bash tool. "
            "Report exactly what happens."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full_response = " ".join(all_text)
    log(f"\n  Direct call results: {direct_call_result}")
    log(f"  SPIKE38_MCP_CALLED in response: {'SPIKE38_MCP_CALLED' in full_response}")
    log(f"  Direct Python hook result: {'SUCCESS' if direct_call_result else 'FAILED'}")

    return bool(direct_call_result)


# ============================================================
# Test 4: Advisory Pattern with acceptEdits mode
# ============================================================
async def test4_advisory_accept_edits():
    """
    Same as Test 1 but with acceptEdits permission mode.
    Tests whether settings.json hooks fire outside bypassPermissions.
    """
    log("\n=== Test 4: Advisory Pattern (acceptEdits mode) ===")

    # Clear debug log
    Path("/tmp/spike_38_hook_debug.log").unlink(missing_ok=True)

    setup_project_dir_advisory()

    # Add broad allow rules to the settings so agent doesn't prompt
    settings_path = Path(PROJECT_DIR) / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["permissions"]["allow"] = [
        "mcp__spike-test__spike_echo",
        "mcp__spike-test__spike_lineage",
        "Read *",
        "Write *",
        "Edit *",
        "Glob *",
        "Grep *",
    ]
    settings_path.write_text(json.dumps(settings, indent=2))

    mcp_server, _, _ = create_test_mcp_tool()
    mcp_tool_called = []

    async def post_hook(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        log(f"  [POST] Tool used: {tool_name}")
        if "spike" in tool_name.lower():
            mcp_tool_called.append(tool_name)
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="acceptEdits",
        setting_sources=["project"],
        model="haiku",
        max_turns=5,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    all_text = []
    async with client:
        await client.query(
            "Run 'echo hello_from_bash' using the Bash tool. "
            "Report exactly what happens."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    # Check if the hook script actually ran
    debug_log = Path("/tmp/spike_38_hook_debug.log")
    hook_ran = debug_log.exists()
    if hook_ran:
        log(f"  Hook debug log:\n{debug_log.read_text()[:500]}")
    else:
        log(f"  Hook debug log: NOT FOUND — hook script never ran")

    full_response = " ".join(all_text)
    log(f"\n  MCP tools called: {mcp_tool_called}")
    log(f"  Hook script ran: {hook_ran}")
    log(f"  SPIKE38_MCP_CALLED in response: {'SPIKE38_MCP_CALLED' in full_response}")

    success = bool(mcp_tool_called) or hook_ran
    log(f"  Test 4 result: {'SUCCESS' if success else 'FAILED'}")

    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    return success


async def main():
    log("=" * 60)
    log("Spike 38: Can a Hook Trigger MCP Tool Execution?")
    log("=" * 60)

    results = {}

    # Clear debug logs
    Path("/tmp/spike_38_hook_debug.log").unlink(missing_ok=True)

    results["test1_advisory"] = await test1_advisory_pattern()
    results["test2_http_bridge"] = await test2_http_bridge()
    results["test3_direct_python"] = await test3_direct_python_hook()

    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    for name, result in results.items():
        log(f"  {name}: {'PASS' if result else 'FAIL'}")

    LOG.close()


asyncio.run(main())
