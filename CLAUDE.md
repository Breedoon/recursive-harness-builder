# OBS Agent

Personal AI assistant backed by an Obsidian vault. Uses the Claude Agent SDK for Python.

## Paths

| What | Path |
|------|------|
| **Runtime code** (this dir) | `/Users/breedoon/Documents/obs/` |
| **Obsidian vault** | `/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T` |
| **Vault shorthand** | `T/` in all vault references below |

## Vault Documentation (READ THESE)

All design docs, skills, and the implementation plan live in the vault — not here.

| Document | Vault Path | What It Contains |
|----------|-----------|-----------------|
| **Implementation plan** | `T/Agent/system/implementation-plan.md` | Architecture, component specs, 12 TDD steps, team tracks, verification checklist |
| **Architecture** | `T/Agent/system/architecture.md` | Full system design |
| **Design decisions** | `T/Agent/system/decisions.md` | 25 decisions (D001-D025) with rationale |
| **File conventions** | `T/Agent/skills/file-conventions/SKILL.md` | Vault directory map, all 11 rules, templates |
| **Session offboard** | `T/Agent/skills/session-offboard/SKILL.md` | Memory extraction procedure |
| **Update context** | `T/Agent/skills/update-context/SKILL.md` | How to persist to context.md |
| **All skills** | `T/Agent/skills.md` | Parent note listing all 11 skills |
| **OpenClaw research** | `T/Agent/system/research/openclaw.md` | Memory, prompts, session management patterns |
| **claude-mem research** | `T/Agent/system/research/claude-mem.md` | Observer pattern, knowledge extraction |
| **Claude SDK research** | `T/Agent/system/research/claude-sdk.md` | Hooks, sessions, forking, MCP tools API |
| **Context** | `T/Agent/context.md` | Agent's current brain state |

## Implementation

**Start here**: Read `T/Agent/system/implementation-plan.md`. It has everything:
- Architecture diagram and data flows
- Component specifications with code sketches
- 12 implementation steps in TDD order (Step 0 is the project scaffold)
- Team execution tracks (A: core infra, B: agent behavior, C: server+client, D: polish)
- Verification checklist

**Key SDK reference** (`claude-agent-sdk` v0.1.33):
- `query()` for simple unidirectional use, `ClaudeSDKClient` for interactive sessions
- Hooks: PreToolUse, PostToolUse, UserPromptSubmit, Stop, PreCompact
- Forking: `ClaudeAgentOptions(resume=session_id, fork_session=True)`
- MCP tools: `@tool` decorator + `create_sdk_mcp_server()`
- Subagents cannot spawn sub-subagents (hence fork-based approach)

**Project structure** (to be created in Step 0):
```
/Users/breedoon/Documents/obs/
├── CLAUDE.md                    # This file
├── pyproject.toml               # Dependencies, entry points
├── src/obs_agent/               # Runtime Python code
│   ├── config.py                # Paths, constants
│   ├── prompt.py                # System prompt builder (reads vault)
│   ├── hooks.py                 # SDK hooks (offboard, guard, compact)
│   ├── session.py               # Session lifecycle
│   ├── daemon.py                # FastAPI server
│   └── cli.py                   # CLI client
└── tests/                       # Unit + E2E tests
```

## Vault Rules (for any vault file operations)

- **Extend before creating** — add to existing files by default
- **Parent note pattern** — `folder-name.md` sibling to `folder-name/`
- **Contextual wiki links** — `[[path|why relevant]]`, never bare links
- **Immutable files** — never edit `Misc/Meeting Notes/` transcripts
- **Templates** — structural templates live in `Assets/Templates/`, reference don't inline
- Full reference: `T/Agent/skills/file-conventions/SKILL.md`

## Git

Two repos, don't confuse them:
- **This dir** (`/Users/breedoon/Documents/obs/`) — runtime code, normal git workflow
- **Vault** (`T/`) — has its own git, Obsidian Git auto-commits every 15 min. Agent makes explicit descriptive commits after meaningful operations.
