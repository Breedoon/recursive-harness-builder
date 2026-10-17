# This repository's vault project

This repository is the code of the **OBS Platform** project. The project's memory is not in this repo. Its goals, priorities, decisions, architecture, invariants and responsibilities live in Daniel's Obsidian vault. Read them there before changing anything non-trivial.

Vault: `/workspace/runtime/git/obs-vault-active` on Ubuntu (Git-synced to the Mac and phone).

- **Project parent note:** `Projects/Personal Projects/OBS Platform.md`. Start here; it indexes everything below.
- **Goals and priorities:** `Projects/Personal Projects/OBS Platform/Goals and Priorities.md`, including Daniel's override log.
- **Architecture:** `Projects/Personal Projects/OBS Platform/Architecture.md`. The successor direction is in `Projects/Personal Projects/OBS Platform/Architecture Review.md` (North Star).
- **Decisions (ADR):** `Projects/Personal Projects/OBS Platform/CHANGELOG.md` and `Projects/Personal Projects/OBS Platform/Decision Log.md`.
- **Caching invariant:** `Projects/Personal Projects/OBS Platform/Cache Prefix Stability.md`, plus [docs/cache-proxy-safety.md](docs/cache-proxy-safety.md) in this repo.
- **Beads prefix:** `obs`. Many OBS beads also carry the legacy `vault` prefix. Beads live in the vault's `.beads/issues.jsonl`; write through `.beads/bd-safe`.
- **What "a project" means and why this file exists:** `Projects/Personal Projects/Agentic/Agent Operations/Project Data Model.md`, field 8.

Rule: decisions and architecture belong in the vault notes above. Code comments and repo docs explain the code. When your change alters how OBS works, update the vault architecture note and CHANGELOG in the same piece of work.
