# Skill Review for Runtime Integration

Review of all 12 skills for compatibility with the OBS Agent runtime code.

## Summary

All skills are well-written and consistent in format. A few observations relevant to the runtime implementation:

## Observations by Skill

### file-conventions (core)
- **Status**: Ready. No issues.
- **Note**: The vault directory map is duplicated in both this skill and CLAUDE.md. The system prompt builder (Step 2) should include it only once — from this skill, since it's the authoritative source.

### update-context (core)
- **Status**: Ready. No issues.
- **Runtime note**: The memory extraction fork (Step 8) effectively implements this skill's procedure programmatically. The fork should follow these steps explicitly.

### manage-summaries (core)
- **Status**: Ready. No issues.
- **Runtime note**: This is a background behavior — the agent does it while doing other work. The skill classifier fork should NOT classify this as a needed skill; it's always-on behavior that the system prompt should reference.

### create-reference (core)
- **Status**: Ready. No issues.
- **Runtime note**: References `[[Assets/Templates/reference-card|reference card template]]` but the template must exist in the vault for the agent to use it. Verify template exists during E2E tests.

### split-document (operational)
- **Status**: Ready. No issues.

### session-offboard (operational)
- **Status**: Ready. Minor observation.
- **Note**: Step 4 says "Git commit via Bash" — the agent needs Bash tool access in the fork. The memory extraction fork (Step 8) needs to be configured with tool access to write files and run git commands.

### vault-search (operational)
- **Status**: Ready. No issues.
- **Runtime note**: The vault search fork (Step 7) implements this skill's strategies. The fork prompt should reference these search strategies explicitly.

### git-commit (operational)
- **Status**: Ready. No issues.
- **Note**: The vault path is hardcoded in Step 1 (`/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T`). The runtime should use config.py's VAULT_PATH constant instead. The skill itself is fine — it's documentation for the agent, not code.

### process-meeting (operational)
- **Status**: Ready. No issues.
- **Runtime note**: The PreToolUse hook (Step 4) must enforce immutability of `Misc/Meeting Notes/` files. This skill assumes that protection exists.

### ingest-content (operational)
- **Status**: Ready. No issues.

### daily-planning (operational)
- **Status**: Ready. Minor observation.
- **Note**: This skill mentions creating journal entries through Obsidian (Templater). The runtime agent cannot invoke Obsidian's Templater — it should create placeholder files and note they need Templater processing, as the skill already suggests.

### proactive-behavior (operational) — NEW
- **Status**: Just written. Consistent with existing skill format.
- **Runtime note**: Per D024, core proactive instructions belong in the system prompt. This skill is loaded on demand for complex interactions. The system prompt builder (Step 2) should include a brief "be proactive, connect dots, be resourceful" line, and this skill provides the detailed patterns.

## Action Items for Runtime

1. **System prompt builder** should include a brief proactive-behavior reference (from D024) without loading the full skill.
2. **Skill classifier fork** should recognize when proactive-behavior skill is useful (planning, open-ended, topic with vault history).
3. **PreToolUse hook** must guard `Misc/Meeting Notes/` as immutable (process-meeting depends on this).
4. **Memory extraction fork** should follow update-context and session-offboard procedures explicitly.
5. **Verify** `Assets/Templates/reference-card.md` exists in the vault (create-reference depends on it).
