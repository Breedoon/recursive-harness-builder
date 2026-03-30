"""
Spike 39: Config-driven hook → MCP tool execution

OBS section in settings.json defines tool intercepts.
SDK PreToolUse hook reads the config and either:
  - "advisory": denies tool, tells agent to call MCP tool (proper JSONL)
  - "direct": denies tool, calls .handler() silently, injects result as context

Config format in settings.json:
{
  "obs": {
    "tool_intercepts": [
      {
        "on": "PreToolUse",
        "match": "Bash",          // tool_name to intercept
        "mode": "advisory",       // or "direct"
        "tool": "mcp__server__tool_name",
        "input": {"key": "value"},
        "context": "Template with {result} placeholder"
      }
    ]
  }
}
"""
import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_39.log", "w")
def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()
    print(msg)

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    TextBlock, AssistantMessage, ResultMessage,
    ToolUseBlock,
    tool, create_sdk_mcp_server, SdkMcpTool,
)

CWD = "/Users/breedoon/Documents/obs"
PROJECT_DIR = "/tmp/spike_39_project"


# ============================================================
# MCP Tool Registry
# ============================================================

def create_test_tools():
    """Create test MCP tools and return (server, tool_registry)."""
    @tool("spike_echo", "Echo back input with a marker", {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message to echo"},
        },
        "required": ["message"],
    })
    async def spike_echo(args):
        msg = args.get("message", "no message")
        result = f"SPIKE39_ECHO: {msg}"
        log(f"  [MCP] spike_echo called: {msg}")
        return {"content": [{"type": "text", "text": result}]}

    @tool("spike_action", "Simulate an action (like AgentTask)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to do"},
            "display_name": {"type": "string", "description": "Name"},
        },
        "required": ["prompt"],
    })
    async def spike_action(args):
        prompt = args.get("prompt", "")
        name = args.get("display_name", "unnamed")
        result = f"SPIKE39_ACTION: agent '{name}' launched with prompt: {prompt}"
        log(f"  [MCP] spike_action called: name={name}, prompt={prompt[:80]}")
        return {"content": [{"type": "text", "text": result}]}

    server = create_sdk_mcp_server("spike-test", tools=[spike_echo, spike_action])
    registry = {
        "mcp__spike-test__spike_echo": spike_echo,
        "mcp__spike-test__spike_action": spike_action,
    }
    return server, registry


# ============================================================
# Config-Driven Hook Engine
# ============================================================

class ConfigDrivenHookEngine:
    """Reads OBS tool_intercepts from settings.json and executes them."""

    def __init__(self, project_dir: str, tool_registry: dict[str, SdkMcpTool]):
        self.project_dir = project_dir
        self.tool_registry = tool_registry
        self.intercepts = self._load_intercepts()
        self.execution_log = []  # Track what happened

    def _load_intercepts(self):
        """Load tool_intercepts from settings.json obs section."""
        settings_path = Path(self.project_dir) / ".claude" / "settings.json"
        if not settings_path.exists():
            return []
        settings = json.loads(settings_path.read_text())
        obs = settings.get("obs", {})
        intercepts = obs.get("tool_intercepts", [])
        log(f"  [ENGINE] Loaded {len(intercepts)} intercept rules")
        for i, rule in enumerate(intercepts):
            log(f"    [{i}] match={rule.get('match')} mode={rule.get('mode')} → {rule.get('tool')}")
        return intercepts

    def _find_matching_rule(self, tool_name: str, event: str = "PreToolUse"):
        """Find first matching intercept rule for this tool call."""
        for rule in self.intercepts:
            if rule.get("on", "PreToolUse") != event:
                continue
            match = rule.get("match", "")
            if match == tool_name or match == "*":
                return rule
        return None

    async def pre_tool_use(self, hook_input, tool_use_id, context):
        """PreToolUse hook that applies config-driven intercepts."""
        if not isinstance(hook_input, dict):
            return {"continue_": True}

        tool_name = hook_input.get("tool_name", "")
        rule = self._find_matching_rule(tool_name)

        if not rule:
            return {"continue_": True}

        mode = rule.get("mode", "advisory")
        target_tool = rule.get("tool", "")
        target_input = rule.get("input", {})
        context_template = rule.get("context", "")

        log(f"  [ENGINE] Intercepting {tool_name} → mode={mode}, tool={target_tool}")

        if mode == "advisory":
            return self._handle_advisory(tool_name, target_tool, target_input, context_template)
        elif mode == "direct":
            return await self._handle_direct(tool_name, target_tool, target_input, context_template)
        else:
            log(f"  [ENGINE] Unknown mode: {mode}")
            return {"continue_": True}

    def _handle_advisory(self, original_tool, target_tool, target_input, context_template):
        """Deny the tool and tell the agent to call the target tool instead."""
        input_json = json.dumps(target_input)
        reason = (
            f"{original_tool} is intercepted by OBS hook config. "
            f"You MUST call {target_tool} with input: {input_json}. "
            f"Do this immediately."
        )
        additional = context_template.replace("{tool}", target_tool).replace(
            "{input}", input_json
        ) if context_template else (
            f"SYSTEM: {original_tool} intercepted. Execute {target_tool} "
            f"with parameters: {input_json}"
        )

        self.execution_log.append({
            "mode": "advisory",
            "original": original_tool,
            "target": target_tool,
            "input": target_input,
        })

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
                "additionalContext": additional,
            },
        }

    async def _handle_direct(self, original_tool, target_tool, target_input, context_template):
        """Deny the tool and execute the target tool directly via handler."""
        tool_ref = self.tool_registry.get(target_tool)
        if not tool_ref:
            log(f"  [ENGINE] Tool not found in registry: {target_tool}")
            return {"continue_": True}

        try:
            result = await tool_ref.handler(target_input)
            result_text = result["content"][0]["text"] if result.get("content") else str(result)
            log(f"  [ENGINE] Direct execution result: {result_text[:200]}")
        except Exception as e:
            log(f"  [ENGINE] Direct execution error: {e}")
            result_text = f"Error executing {target_tool}: {e}"

        additional = context_template.replace("{tool}", target_tool).replace(
            "{result}", result_text
        ) if context_template else (
            f"Tool {original_tool} was intercepted. "
            f"{target_tool} was executed automatically. Result: {result_text}"
        )

        self.execution_log.append({
            "mode": "direct",
            "original": original_tool,
            "target": target_tool,
            "input": target_input,
            "result": result_text,
        })

        return {
            "continue_": False,
            "decision": "block",
            "reason": f"{original_tool} intercepted. {target_tool} result: {result_text}",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": additional,
            },
        }


# ============================================================
# Project Setup
# ============================================================

def setup_project(intercepts):
    """Create project dir with OBS config in settings.json."""
    project = Path(PROJECT_DIR)
    if project.exists():
        shutil.rmtree(project)
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True)

    settings = {
        "permissions": {"allow": [], "deny": []},
        "obs": {
            "tool_intercepts": intercepts
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))

    # Git init for project recognition
    import subprocess
    subprocess.run(["git", "init"], cwd=PROJECT_DIR, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                    cwd=PROJECT_DIR, capture_output=True)

    log(f"  Project created at {PROJECT_DIR}")
    log(f"  Config: {json.dumps(settings['obs'], indent=2)}")


# ============================================================
# Test 1: Advisory mode — agent calls MCP tool (proper JSONL)
# ============================================================

async def test1_advisory_config():
    """Config says: when Bash is used, tell agent to call spike_echo instead."""
    log("\n=== Test 1: Config-Driven Advisory Mode ===")

    setup_project([
        {
            "on": "PreToolUse",
            "match": "Bash",
            "mode": "advisory",
            "tool": "mcp__spike-test__spike_echo",
            "input": {"message": "config_advisory_intercept"},
            "context": "SYSTEM: {tool} must be called with input {input}. Do it now."
        }
    ])

    mcp_server, registry = create_test_tools()
    engine = ConfigDrivenHookEngine(PROJECT_DIR, registry)
    tools_called = []

    async def post_hook(hook_input, tool_use_id, context):
        name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        log(f"  [POST] {name}")
        if "spike" in name:
            tools_called.append(name)
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="haiku",
        max_turns=5,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PreToolUse": [HookMatcher(hooks=[engine.pre_tool_use])],
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    all_text = []
    async with client:
        await client.query("Run 'echo test' using Bash. Report what happens.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:200]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:150]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full = " ".join(all_text)
    success = "mcp__spike-test__spike_echo" in tools_called
    log(f"\n  Tools called: {tools_called}")
    log(f"  Engine log: {engine.execution_log}")
    log(f"  SPIKE39 in response: {'SPIKE39' in full}")
    log(f"  Test 1 (advisory): {'PASS' if success else 'FAIL'}")
    return success


# ============================================================
# Test 2: Direct mode — SDK calls handler (invisible to agent)
# ============================================================

async def test2_direct_config():
    """Config says: when Bash is used, silently execute spike_action."""
    log("\n=== Test 2: Config-Driven Direct Mode ===")

    setup_project([
        {
            "on": "PreToolUse",
            "match": "Bash",
            "mode": "direct",
            "tool": "mcp__spike-test__spike_action",
            "input": {
                "prompt": "Research the user's question",
                "display_name": "auto-researcher"
            },
            "context": "An agent was launched on your behalf: {result}"
        }
    ])

    mcp_server, registry = create_test_tools()
    engine = ConfigDrivenHookEngine(PROJECT_DIR, registry)

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="haiku",
        max_turns=5,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PreToolUse": [HookMatcher(hooks=[engine.pre_tool_use])],
        },
    ))

    all_text = []
    async with client:
        await client.query("Run 'echo test' using Bash. Report what happens.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:200]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:150]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full = " ".join(all_text)
    direct_executed = any(e["mode"] == "direct" for e in engine.execution_log)
    result_in_text = "SPIKE39_ACTION" in full
    log(f"\n  Engine log: {engine.execution_log}")
    log(f"  Direct executed: {direct_executed}")
    log(f"  Result in response: {result_in_text}")
    log(f"  Test 2 (direct): {'PASS' if direct_executed and result_in_text else 'FAIL'}")
    return direct_executed and result_in_text


# ============================================================
# Test 3: Mixed — multiple intercept rules
# ============================================================

async def test3_mixed_rules():
    """Multiple rules: Bash→advisory echo, Grep→direct action."""
    log("\n=== Test 3: Mixed Rules (advisory + direct) ===")

    setup_project([
        {
            "on": "PreToolUse",
            "match": "Bash",
            "mode": "advisory",
            "tool": "mcp__spike-test__spike_echo",
            "input": {"message": "bash_intercepted"},
        },
        {
            "on": "PreToolUse",
            "match": "Grep",
            "mode": "direct",
            "tool": "mcp__spike-test__spike_action",
            "input": {"prompt": "grep replacement", "display_name": "grep-agent"},
            "context": "Grep intercepted. Agent result: {result}"
        },
    ])

    mcp_server, registry = create_test_tools()
    engine = ConfigDrivenHookEngine(PROJECT_DIR, registry)
    tools_called = []

    async def post_hook(hook_input, tool_use_id, context):
        name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else "?"
        if "spike" in name:
            tools_called.append(name)
        return {"continue_": True}

    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="haiku",
        max_turns=8,
        cwd=PROJECT_DIR,
        mcp_servers={"spike-test": mcp_server},
        hooks={
            "PreToolUse": [HookMatcher(hooks=[engine.pre_tool_use])],
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        },
    ))

    all_text = []
    async with client:
        await client.query(
            "Do two things: "
            "1. Run 'echo test' using Bash. "
            "2. Search for 'hello' in the current directory using Grep. "
            "Report what happens for each."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        all_text.append(b.text)
                        log(f"  TEXT: {b.text[:200]}")
                    elif isinstance(b, ToolUseBlock):
                        log(f"  TOOL_USE: {b.name} / {str(b.input)[:150]}")
            elif isinstance(msg, ResultMessage):
                log(f"  DONE: cost=${msg.total_cost_usd:.4f}")

    full = " ".join(all_text)
    advisory_fired = any(e["mode"] == "advisory" for e in engine.execution_log)
    direct_fired = any(e["mode"] == "direct" for e in engine.execution_log)
    log(f"\n  Tools called by agent: {tools_called}")
    log(f"  Engine log: {engine.execution_log}")
    log(f"  Advisory fired: {advisory_fired}, Direct fired: {direct_fired}")
    success = advisory_fired and direct_fired
    log(f"  Test 3 (mixed): {'PASS' if success else 'FAIL'}")
    return success


# ============================================================
# Main
# ============================================================

async def main():
    log("=" * 60)
    log("Spike 39: Config-Driven Hook → MCP Tool Execution")
    log("=" * 60)

    results = {}
    results["test1_advisory"] = await test1_advisory_config()
    results["test2_direct"] = await test2_direct_config()
    results["test3_mixed"] = await test3_mixed_rules()

    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    for name, result in results.items():
        log(f"  {name}: {'PASS' if result else 'FAIL'}")

    # Cleanup
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    LOG.close()


asyncio.run(main())
