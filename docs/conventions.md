# File System Conventions

## Directory Structure

```
T/                                    (Obsidian vault root)
  Agent/                              Agent knowledge (top-level, first-class)
    context.md                        Core context, always loaded
    skills/                           Instruction files for agent behavior
    topics/                           Organic topic files split from context.md
    drafts/                           Temporary artifacts, partitioned by month
      YYYY-MM/
    system/                           System documentation (this folder)
    system.md                         Parent note for system/
  Ж/                                  Journal hierarchy (D/W/M/S/Y) - existing
  Vault/                              Knowledge categories - existing
  Misc/                               Miscellaneous - existing
    Meeting Notes.md                  Parent note for meeting notes
    Meeting Notes/                    Raw transcripts - existing
  Assets/                             Templates, scripts - existing
```

## Naming Conventions

| Convention | Meaning | Examples |
|-----------|---------|---------|
| `_` prefix | System-managed/generated file | `_summaries.md`, `_archive-001.md` |
| No prefix | User-authored or primary content | `decisions.md`, `goals.md` |
| Sibling parent note | Entry point for a directory | `system.md` next to `system/` |
| `YYYY-MM-DD` prefix | Date-stamped content | `2026-02-10 Call with Daniil.md` |
| `YYYY-MM/` subdirectory | Monthly partitioning for high-volume content | `drafts/2026-02/` |

## Parent Note Pattern

Every directory that needs an entry point gets a **sibling parent note** at the same level:

```
folder-name.md     <- parent note (hub, summaries, links)
folder-name/       <- directory with child files
```

`[[folder-name]]` resolves to the parent note. One convention for everything. No `_index.md` files.

Parent notes contain:
- **Conventions**: how files in the directory are structured/named
- **Curated summaries**: one-line summaries, lazy-appended on first touch
- **Archive links**: when summaries overflow, link to `_archive-NNN.md`

Parent notes are **curated, not comprehensive**. Not every child file must be listed. Agent uses filesystem search (grep/glob) for exhaustive queries.

## Universal Document Lifecycle

Every document (except raw immutable content like transcripts):

1. **Single file** - created with initial content
2. **Grows** - content appended via agent interactions
3. **Splits** - when too large, sections become child files in a same-named directory
4. **Parent becomes hub** - retains summaries + links to children
5. **Archive overflow** - older entries in parent move to `_archive-001.md`, `_archive-002.md` etc. (numbered sequentially, cutoff date noted inside the file)

Links to parent notes always work. One extra hop to reach children is acceptable.

## File Bloat Rules

1. **Default: extend existing files**, not create new ones
2. **New files must have a parent** - agent must know which parent note links to this file
3. **Temporary artifacts** go to `Agent/drafts/YYYY-MM/` - monthly partitioned, no indexing needed
4. **Never create new top-level directories** without explicit user approval
5. **Drafts** either get promoted (moved to proper home) or decay into irrelevance

## Underscore Prefix (`_`) Convention

Files prefixed with `_` are system-managed:
- `_archive-001.md` - overflow content from a parent note
- `_summaries.md` - detailed tier-1 summaries (post-MVP)
- These are still regular markdown, still linkable, still follow the universal lifecycle
- They appear first in directory listings (`ls`) for easy identification

## Contextual Wiki Links

All references use: `[[path/to/file|why this is relevant]]`

The description after `|` explains the relationship, not just the file name. Every link should be self-documenting. This applies to:
- Inline references in prose (preferred)
- Items in Reference sections
- Entries in parent notes
