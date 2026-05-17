# Procedures

Procedures are markdown prompt files that teach agents how to behave in a recursive workflow. They are not runtime code: the harness can run any prompt, but procedures make multi-agent work repeatable.

## Included starter project

The repository includes a runnable example project at:

```text
examples/recursive-workflow/
```

Use it as the first `OBS_VAULT_PATH` value:

```bash
OBS_VAULT_PATH=/absolute/path/to/repo/examples/recursive-workflow
```

That directory contains:

- `CLAUDE.md` — minimal entry instructions for agents running in the example project.
- `procedures/router.md` — dispatch-only orchestration.
- `procedures/scope.md` — decomposition and SIMPLE/COMPLEX routing decision.
- `procedures/loop.md` — execute/verify/fix-loop orchestration.
- `procedures/executor.md` — direct execution.
- `procedures/verifier.md` — independent verification.
- `procedures/auditor.md` — assembled-goal audit.
- `procedures/unblock.md` — blocker investigation.
- `procedures/brainstorm.md` — option generation when approach is unclear.
- `hooks/router_guard.py` — optional Router guard that blocks direct write tools.
- `artifacts/` — suggested report location for example agents.

## Choosing Loop or Router

Use Loop for simple tasks that can be handled by one execute/verify/fix cycle. Use Router for complex tasks that need decomposition into subtasks.

### Simple task: launch Loop

```json
{
  "prompt_file": "procedures/loop.md",
  "prompt": "Handle this task: inspect the project and list the top setup blockers.",
  "fork": true
}
```

### Complex task: launch Router

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

Those fields are an `AgentTask` payload, not a shell command. In Telegram mode, the human-facing way to use them is to ask the root agent to launch a Loop or Router with the matching prompt file.

The Router hook is intentionally narrow: it blocks direct file-writing tools so a Router must dispatch implementation work instead of doing it itself.

## Customizing procedures

Copy the example project and edit the copy instead of editing the bundled starter in place:

```bash
cp -R examples/recursive-workflow ~/my-recursive-project
```

A custom project can keep the same flat shape:

```text
my-recursive-project/
  CLAUDE.md
  procedures/
    router.md
    scope.md
    loop.md
    executor.md
    verifier.md
    auditor.md
    unblock.md
    brainstorm.md
  hooks/
    router_guard.py
  artifacts/
```

Good procedure files:

- define one role clearly;
- state what the agent may and may not do;
- require artifact paths for nontrivial work;
- define evidence before claiming completion;
- avoid absolute paths unless they are part of your deployment;
- avoid credentials, personal notes, and private system details.

## Runtime relationship

The harness treats a procedure file as prompt context. The runtime does not need to know that a file is a Router, Loop, Executor, or Verifier; those role names are conventions enforced by the procedure text and by whatever launch pattern the user chooses.
