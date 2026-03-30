# Vault Routing

Design docs, specs, and PRD for OBS Agent live in the Obsidian vault.

**Absolute path:** `/Users/breedoon/Library/Mobile Documents/iCloud~md~obsidian/Documents/T/Projects/Personal Projects/OBS Platform`

## Key Documents

- **Product Vision** — what OBS is and where it's going (orchestration platform, autonomous execution, governance model)
- **Design Principles** — non-negotiable axioms for development decisions (visibility, control, simplicity, separations, security)
- **User Mental Model** — how the user conceptualizes the platform (topic=agent, group=tree, navigability expectations)
- **User Journeys** — scenario walkthroughs with expected behavior, organized by user intent
- **Development Philosophy** — how we build, test, and iterate (eval strategy, spike-before-build, iteration methodology)
- **Feature Inventory** — every capability by domain, with migrated specs and spike findings as sub-notes
- **Architecture** — runtime internals, module map, data flows, subsystem deep-dives
- **Decision Log** — historical decisions with rationale, cross-referencing design principles

## What Stays in This Codebase

- `docs/conventions.md` — code-level conventions (linting, style, naming)
- `docs/feature-audit.md` — raw Phase 1 research output (referenced by vault docs)
- `docs/research/` — raw research outputs (cross-reference reports, etc.)
- `spikes/` — executable spike scripts (findings and reports migrate to vault)
- `tests/` — test code and eval scenarios
- `CLAUDE.md` — agent instructions for working on this codebase

## Migration Status

Specs in `docs/specs/` and plans in `docs/plans/` are being migrated to the vault as sub-notes of the relevant Feature Inventory domains. Until migration is complete, some specs may exist in both locations. The vault version is authoritative.
