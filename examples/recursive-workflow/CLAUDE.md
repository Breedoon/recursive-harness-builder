# Recursive Workflow Starter

This project demonstrates a flat markdown procedure bundle for recursive agent workflows. Procedures are ordinary markdown files. The router guard hook is at `hooks/router_guard.py`; artifacts should be written under `artifacts/`.

## Default behavior

For any complex request — anything that sounds like it needs decomposition, multiple steps, or multiple concerns — spawn a Router and have it implement the entire request:

```json
{
  "prompt_file": "procedures/router.md",
  "prompt": "Handle this request: {one-sentence summary of the user's ask}.",
  "fork": true,
  "hooks": {
    "PreToolUse": "hooks/router_guard.py::check"
  }
}
```

For simple, single-concern tasks that can be handled in one execute/verify cycle, spawn a Loop:

```json
{
  "prompt_file": "procedures/loop.md",
  "prompt": "Handle this task: {one-sentence summary}.",
  "fork": true
}
```

When in doubt, use Router. It will scope the task and decide whether to decompose further or dispatch directly.

After launching a Router, set yourself a 30-minute recurring schedule to nudge it for status and ask it to continue or unblock downstream agents if progress stalls.

## Contents

- `CLAUDE.md` — this file. Entry point for agents.
- `procedures/` — flat v1 procedure files: Router, Scope, Loop, Executor, Verifier, Auditor, Unblock, Brainstorm.
- `hooks/router_guard.py` — Router guard that blocks direct file-writing tools so Routers orchestrate instead of implementing.
- `artifacts/` — where agents write reports and handoff notes.

## Forking agents

Use `AgentTask` to dispatch subagents.

- Use `fork=true` for most subagents. A fork inherits the current conversation, so the prompt should be short and should not repeat context the fork already has.
- Use `fork=false` only for clean-slate reviews, fresh-user tests, or unrelated work where inherited context would bias the result.
- Parallelize independent work instead of serializing it through one agent.
- Give each subagent a clear ownership boundary so overlapping edits are easier to reconcile.

Good fork prompt: `Review the installation guide as a first-time user and report unclear steps.`
Bad fork prompt: a long recap of the whole conversation the fork already inherited.

## Agent lifecycle

Agents go idle when a turn completes; they do not disappear.

- Prefer messaging or resuming an existing agent over launching a replacement.
- Launch a replacement only when the existing agent is unreachable, deleted, or clearly dead.
- If an agent fails with a terminal runtime error, create a replacement and give it enough context to continue from the previous agent's artifacts.

## Messaging and artifacts

- Send results back through the runtime's messaging channel when another agent is waiting on them.
- Write durable handoff notes, plans, and review findings under `artifacts/`.
- Include enough context in artifacts that another agent can continue without reading the full transcript.
- Do not treat artifact files as a substitute for telling the parent agent that work is ready.

## Scheduling

When creating recurring or delayed runs:

- Preserve session continuity unless the user explicitly asks for a stateless run.
- Do not let child agents inherit schedules by default.
- A scheduled watchdog should check for real progress, not just whether a log file changed.
