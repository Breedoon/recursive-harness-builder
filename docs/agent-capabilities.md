# Agent Capabilities

A practical ledger of what AI agents can and cannot do reliably. Use this when designing workflows, procedures, hooks, and verification strategies.

This document is intentionally pragmatic: it describes recurring behavior patterns observed while running multi-agent workflows, not theoretical limits of language models.

---

## Core Strengths

**Reading and understanding files.** Agents reliably parse Markdown, code, configs, and cross-references. They can synthesize across multiple files when the relevant context is available.

**Surgical single-file edits.** When the scope is one file with a clear objective, agents usually perform well.

**Following clear step-by-step procedures.** Agents follow numbered steps with concrete actions: read this file, run this command, spawn this agent, report this artifact. Vague instructions fail; specific ones succeed.

**Writing code in common languages.** Agents are generally strong at Python, TypeScript, JavaScript, and other well-documented ecosystems.

**Explaining reasoning when asked.** Agents can explain why they made a decision, but they often do not provide useful explanations unless prompted.

**Creative brainstorming when explicitly prompted.** Agents can propose useful out-of-the-box ideas, especially when several agents brainstorm independently and their ideas are synthesized.

**Re-deriving missed requirements on re-read.** When pointed back to a spec and asked what they missed, agents often find gaps in their own prior work.

**Parallel investigation.** Multiple agents can investigate different parts of a problem simultaneously. This is especially useful for search, audit, review, and synthesis work.

**Fresh perspective per agent.** A newly spawned agent is less anchored on a prior failed approach than the agent that tried it first.

**Patient exhaustive search.** Agents can systematically check many files, repos, or documents when instructed to do so.

**Cross-referencing at scale.** Agents can compare many documents and find patterns, contradictions, or gaps across them.

---

## Core Weaknesses

**Premature victory declaration.** Agents often say work is complete before it is actually complete.

- *Practical implication:* Do not trust completion claims without external verification: tests, live behavior, independent review, or user confirmation.

**Default certainty bias.** Agents present inferences as established facts.

- *Practical implication:* Require source citations or explicit uncertainty markers for factual claims.

**Dropping side constraints.** When given a primary task plus several secondary constraints, agents tend to complete the main task and forget some side constraints.

- *Practical implication:* Use hooks or separate review agents for formatting, citation, safety, changelog, and cleanup requirements.

**Weak system-wide architecture judgment.** Agents often make reasonable local edits but miss broader architectural consequences.

- *Practical implication:* Separate implementation from architecture review. For important changes, ask another agent to critique design choices before merging.

**Poor recall.** Agents are good at judging relevance once they find something, but bad at knowing what else might exist.

- *Practical implication:* Use broad search, multiple search agents, and explicit “check prior work” steps. Store knowledge with enough indexing that future agents can find it.

**Not checking precedent by default.** Agents do not naturally ask “has this been tried before?”

- *Practical implication:* Add procedure steps that check git history, issue history, prior artifacts, or project notes before making major decisions.

**Declaring things impossible too easily.** “I cannot figure this out” often becomes “this cannot be done.”

- *Practical implication:* Before accepting impossibility, spawn an unblock or adversarial agent whose job is to find a missed path.

**Testing their own work inadequately.** Agents tend to write tests that validate their implementation rather than the original requirement.

- *Practical implication:* Use a separate agent to write tests from the requirements, not from the implementation.

**Coordination is cognitively expensive.** An agent managing several sub-agents is already doing a full job.

- *Practical implication:* Coordinators should coordinate. Executors should execute. Verifiers should verify. Avoid combining those roles in one agent when stakes are high.

**Naive trust of other agents.** Agents often accept another agent’s “done” report without enough verification.

- *Practical implication:* Treat agent reports as claims, not evidence. Verify artifacts directly.

**Lost details during summarization.** Important constraints mentioned in passing may disappear from summaries and plans.

- *Practical implication:* Preserve raw user requests and cross-check plans against the original request before execution.

**Unreliable improvisation under ambiguity.** Agents can improvise, but guesses compound when they lack context about expected patterns.

- *Practical implication:* Procedures should handle predictable branches explicitly and tell agents when to ask, fork, or escalate.

**Scope underestimation.** Agents often assume a task is simpler than it is.

- *Practical implication:* Add scoping steps that ask about edge cases, external dependencies, risk areas, performance constraints, and verification strategy.

**Defaulting to direct work instead of delegation.** Even when a task should be decomposed, agents often try to solve it in their own context.

- *Practical implication:* Use router procedures and hooks to structurally encourage or enforce delegation for complex tasks.

**Stopping before deep information.** Agents may stop after one or two hops even when the answer is several references away.

- *Practical implication:* Tell agents exactly how deep to follow links, references, logs, or history.

**Cannot meaningfully self-evaluate quality.** Agents are poor judges of whether their own output is “good,” “substantive,” or “complete.”

- *Practical implication:* Replace subjective self-checks with concrete checks, or delegate quality judgment to a separate verifier.

**Agentic orchestration is not native behavior.** Agents do not automatically know how to spawn sub-agents, manage hierarchies, or write effective procedures.

- *Practical implication:* Procedure-writing and orchestration need explicit instructions, testing, and human review.

---

## Context and Session Behavior

**High context degrades reliability.** Agents with very large contexts are more likely to miss details, repeat themselves, or make inconsistent decisions.

- *Practical implication:* Prefer short-lived agents. Summarize and persist results outside the agent before contexts become too large.

**Compaction loses nuance.** Compacted context may preserve facts but lose uncertainty, source quality, or caveats.

- *Practical implication:* Persist important reasoning and evidence to files before compaction.

**Agents cache stale knowledge.** Once an agent has read a file, it may not re-read it after changes.

- *Practical implication:* Use fresh agents for review, or explicitly tell agents to re-read files that may have changed.

**Ephemeral agents work well.** Single-purpose agents avoid stale assumptions and context buildup.

- *Practical implication:* Prefer many short-lived agents over one long-running generalist.

**Focused executor model.** Agents do best with one clear objective.

- *Practical implication:* Split side concerns into separate agents or hooks.

---

## Tool-Specific Observations

**Git operations are reliable when paths are explicit.** Agents can inspect history, compare branches, commit, and merge well when working directory ambiguity is removed.

- *Practical implication:* Use absolute paths in automation and prompts when multiple repositories may be present.

**Agent spawning must be explicit.** Agents do not reliably decide to spawn sub-agents unless the procedure tells them when and how.

- *Practical implication:* Include concrete AgentTask payload examples in procedures.

**Hooks are most effective at the moment of action.** Specific, contextual reminders work better than generic rules read at the start.

- *Practical implication:* Use hooks for structural constraints such as “router may not write files directly,” risky-tool approval, or source-citation enforcement.

---

## Workflow Patterns

**Adversarial sub-agents improve quality.** A second agent tasked with finding problems catches issues the first agent misses.

- *Practical implication:* Phrase verifier tasks as “find problems” rather than “confirm this is good.”

**Minimum issue counts can become ceilings.** If asked for “at least three issues,” agents often find exactly three.

- *Practical implication:* Ask for all issues, optionally with a minimum, and use multiple specialist reviewers for important work.

**Agents can re-derive requirements from specs.** When work is incomplete, pointing an agent back to the source spec often works better than listing every missing item yourself.

- *Practical implication:* Use “re-read the requirements and identify what you missed” as a repair pattern.

**Multi-agent brainstorming with synthesis works.** Independent proposals from several agents can produce better decisions than one agent’s judgment.

- *Practical implication:* For major design choices, collect independent proposals before choosing a direction.

**Procedure updates should not overfit one failure.** Agents may patch a procedure for one incident in a way that hurts generality.

- *Practical implication:* Update procedures based on repeated patterns, not every individual failure.

---

## Verification and Trust

**Agents should never be the sole source of truth.** External evidence matters more than agent self-report.

- *Practical implication:* Every workflow should include an external verification step appropriate to the task.

**High-stakes plus hard-to-verify work should escalate.** Risk and verifiability determine autonomy.

| Risk level | Easy to verify | Hard to verify |
| --- | --- | --- |
| Low | Agent can decide autonomously | Agent can decide and flag uncertainty |
| Medium | Agent can decide with adversarial review | Escalate with a recommendation |
| High | Agent can decide only with strong external verification | Escalate to the user |

**Delayed feedback is difficult.** Strategic decisions and latent bugs cannot be validated immediately.

- *Practical implication:* Use more upfront verification when feedback will arrive late.

**More independent agents can improve reliability.** Consensus across independent agents is often more trustworthy than one agent’s confidence.

- *Practical implication:* Spend extra agent work on important decisions, safety boundaries, and hard-to-verify claims.

---

## Procedure Design Implications

Every procedure instruction should pass this gate: **would the outcome be meaningfully different without this instruction?** If not, remove it.

Useful instructions fall into three levels:

- **Level 1: simple nudge.** The agent can do this naturally; one line is enough.
- **Level 2: guided execution.** The agent can do it, but needs examples, edge cases, and failure handling.
- **Level 3: delegate.** The agent cannot reliably do this itself, such as evaluating its own output quality. Spawn a separate agent.

A common mistake is writing a Level 1 instruction for a Level 3 problem. “Verify your output is substantive” sounds like a rule, but it usually does not change behavior. If quality judgment matters, use a verifier.

---

## How to Use This Document

When designing a workflow or procedure, ask:

1. Is the core task an agent strength?
2. Does the task involve a known weakness?
3. What external verification is required?
4. Should this be one agent, or several focused agents?
5. Are side constraints better handled by a hook or separate reviewer?
6. What should happen if the agent gets stuck?

Use this document as a design checklist, not as a guarantee. Capabilities depend on model, context quality, tools, and verification strategy.
