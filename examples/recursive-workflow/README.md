# Recursive Workflow Example Project

This directory is a starter project for Recursive Harness Builder. It shows how a project can define a recursive workflow with plain markdown procedures and a small optional hook.

Run the harness against this directory for a first test:

```bash
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow uv run obs-agent
```

## Contents

- `CLAUDE.md` — minimal entry instructions for agents in this example project.
- `procedures/` — flat v1 procedure files: Router, Scope, Loop, Executor, Verifier, Auditor, Unblock, and Brainstorm.
- `hooks/router_guard.py` — optional Router guard that blocks direct file-writing tools.
- `artifacts/` — suggested location for agent reports.

## Launching procedures

Procedure launches use `AgentTask` payload fields. These snippets are not shell commands.

For a simple task that needs execute/verify/fix orchestration, launch Loop:

```json
{
  "prompt_file": "procedures/loop.md",
  "prompt": "Handle this task: inspect the project and list the top setup blockers.",
  "fork": true
}
```

For a complex task that needs decomposition and recursive dispatch, launch Router with the guard hook:

```json
{
  "prompt_file": "procedures/router.md",
  "prompt": "Route this task through the recursive workflow: prepare this project for a new user.",
  "fork": true,
  "hooks": {
    "PreToolUse": "hooks/router_guard.py::check"
  }
}
```

The router guard keeps Routers in an orchestration role. Routers should spawn Loop or Router children rather than editing files directly.

## Customizing

Copy this directory and edit the procedure files for your project. Keep credentials, private paths, and deployment-specific hooks out of public examples.
