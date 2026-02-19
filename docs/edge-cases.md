# Edge Cases & Future Considerations

Issues explored during the [[Agent/system/sessions/2026-02-11-initial-design|initial design session]] that are not part of the MVP but informed architectural decisions.

## External Data Sources

### The Granularity Problem
Different external sources have wildly different natural granularity:
- A book = 1 reference card (obvious)
- An article = 1 reference card (obvious)
- A text conversation = 1 reference card per conversation (not per message)
- Individual text messages = aggregated, not individual cards
- Emails = per-email or per-thread, depends on significance

**Resolution**: No fixed rule. The agent uses judgment guided by the skill. The principle: "one reference card per thing you'd want to find later." Individual text messages are not things you search for; conversations with specific people are.

### Mutable External Content
GitHub repos, articles, and web content can change after capture.

**Resolution**: Reference cards record point-in-time snapshots. Include capture date. If content is revisited and has changed, update the card or append a new dated section. The URL is stable; the summary is versioned by date within the card.

### Immutable Vault Content
Meeting transcripts should not be modified (no added frontmatter, tags, etc.).

**Resolution**: Indexing happens externally via parent notes and lazy-append summaries. The transcript files remain untouched. Speaker identification and other enrichment happens in a pipeline that produces external metadata, not by modifying the transcript.

## File System Scaling

### Parent Note Overflow
Concern: parent notes for high-volume directories (e.g., meeting notes) could grow to thousands of entries.

**Resolution**: Parent notes are curated, not comprehensive. Older entries move to `_archive-NNN.md` files. The agent uses filesystem search (grep/glob) for exhaustive queries, not the parent note alone. The parent note is for navigation and recent context, not a complete database.

### File Bloat from Agent Activity
Concern: agents tend to create many files. Without discipline, the vault becomes a mess of one-off artifacts.

**Resolution**: Skills enforce rules: (1) extend existing files by default, (2) new files must have a parent, (3) temporary work goes to `Agent/drafts/YYYY-MM/` with monthly partitioning, (4) no new top-level directories without user approval. Drafts are a dump that either get promoted or decay.

### Directory Structure Depth
Concern: as files split into directories, nesting could get deep.

**Resolution**: Not a concern for MVP. The universal lifecycle naturally creates directories only when content justifies them. Skill-level guidance can limit nesting depth if it becomes a problem.

## Template Versioning

### The Migration Problem
When a template changes, all existing files instantiated from the old template need migration.

**Resolution**: Deferred post-MVP. The approach: (1) templates could include a version indicator, (2) a skill assesses differences between template versions, (3) agent proposes a migration procedure for user confirmation, (4) migration is executed with Git providing rollback safety. For now, template changes are infrequent enough to handle manually.

### Template Detection
How to know which files use which template?

**Resolution**: Could be auto-detected via grep (templates have distinctive structure). YAML frontmatter could also declare the template version. Decision deferred - both approaches are viable and the choice depends on how often templates actually change.

## People Tracking

### Speaker Identification
Meeting transcripts currently lack speaker identification. Future pipeline will add it.

**Resolution**: Deferred. When implemented: (1) speaker embeddings linked to person files, (2) uncertainty requires semi-manual verification (similar voices shouldn't be auto-merged), (3) bulk indexing of past meetings would happen as part of this pipeline, not by the LLM.

### Person File Structure
YAML frontmatter with basic fields (name, relationship tier, how met, birthday, etc.) plus free-form notes.

**Resolution**: Deferred post-MVP. Will use existing relationship tier system (S/A/B/C from `Vault/People.md`). Template and structure TBD when feature is implemented.

## Session Management

### Cache Expiry Edge Cases
What if the user starts typing right as cache expires?

**Resolution**: The auto-offboarding skill should trigger well before the cache window ends (e.g., at the 45-minute mark). If a session starts fresh, loading `context.md` provides enough continuity. The OpenClaw reference implementation has similar patterns to study.

### Context Window Limits
Long sessions may hit context limits before cache expires.

**Resolution**: A compaction skill triggers when context usage is high. It summarizes the conversation so far, persists key information to vault files, and continues with a compressed context. Similar to Claude Code's built-in context compression but with vault-aware persistence.

## Embeddings

### Future Semantic Search
Raw grep is limited for imperfectly transcribed content (wrong words, missing terms).

**Resolution**: Embeddings are explicitly deferred. For MVP, the tiered summary system compensates: instead of searching raw transcripts, the agent searches summaries (which use correct terminology even if the transcript had errors). When embeddings are added later, they'll complement the existing system rather than replace it.

## Obsidian-Specific Concerns

### iCloud Storage Constraint
Vault is on iCloud with limited storage.

**Resolution**: Only text/markdown in the vault. Reference cards point to external files rather than copying content. Binary files, large JSONL transcripts, etc. stay outside the vault. `.gitignore` enforces this.

### Obsidian Plugin Dependencies
The agent relies on Obsidian for templated file creation (Templater plugin). What if Obsidian isn't running?

**Resolution**: The agent can still create files directly for non-templated content (which is most things). For templated content (daily/weekly/monthly journal entries), the agent should check if Obsidian is running and either wait or create a placeholder that gets templated later. Edge case for implementation, not a design blocker.
