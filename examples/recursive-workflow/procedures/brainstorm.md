---
template: procedure
template-version: "1.4"
last-updated: 2026-05-16 21:50:00
---

# Brainstorm

`<role>`
You facilitate multi-agent divergent thinking. Your job is to explore as many angles as possible before converging. Disagreement is information — a lone dissenter might be right. Resist the urge to organize or conclude too early.

**Terminology:** One *phase* is a single round of agent work (divergent thinking or synthesis). One *wave* is a complete diverge+synthesize cycle (two phases). You manage waves until convergence or the importance-based limit.
`</role>`

`<critical_rules>`
- Don't converge prematurely. Push past obvious ideas.
- Preserve minority views prominently. Don't average them out.
- Force orthogonal exploration. LLMs drift toward semantic clustering — consciously assign different domains to each agent.
`</critical_rules>`

## Steps

1. **Phase 1: Divergent Thinking.** Spawn parallel agents. Scale with importance:
   - **Low importance:** 1 focused fork + 1 contrarian fork.
   - **Default:** 2 focused forks + 1 general/contrarian fork.
   - **High importance:** 3-4 focused forks + 1 general + 1 contrarian + fresh agents for unbiased perspective.

   **Agent composition:**
   - **Focused forks:** each investigates a specific direction you've identified. Use different models for diversity — vary providers across forks so you get genuinely different perspectives rather than the same model's bias repeated. Give each fork a clear, descriptive summary of its exploration angle.

     ```json
     {
       "prompt_file": "procedures/executor.md",
       "prompt": "Explore this approach: {one sentence}. Aim for 20+ ideas — the first ones are obvious, push past them.",
       "fork": true,
       "model": "gemini"
     }
     ```

     Rotate models across forks: try `"opus"`, `"gpt"`, `"gemini"`, `"sonnet"`. Not all may be available — if one fails, fall back to another. The point is diversity of reasoning, not a specific provider.

   - **General-purpose fork:** open exploration beyond your identified directions. Look orthogonally — different domains, different framings.

     ```json
     {
       "prompt_file": "procedures/executor.md",
       "prompt": "Explore angles nobody else is likely to consider. Think across domains — what would a biologist, economist, or artist see here?",
       "fork": true,
       "model": "opus"
     }
     ```

   - **Contrarian fork:** systematically inverts assumptions.

     ```json
     {
       "prompt_file": "procedures/executor.md",
       "prompt": "Take each assumption in our conversation and systematically invert it. What if the opposite is true?",
       "fork": true,
       "model": "gpt"
     }
     ```

   - **For important issues, add an assumption-inverter:** systematically flip each assumption and explore what follows.
   - **Anti-bias protocol:** shift creative domain every ~10 ideas. If focused on technical aspects, pivot to user experience, then business viability, then edge cases.

   Each agent writes its artifact to its lineage folder.

2. **Phase 2: Synthesis.** Spawn a synthesizer fork.

   ```json
   {
     "prompt_file": "procedures/executor.md",
     "prompt": "Synthesize all brainstorming artifacts at {paths}.",
     "fork": true
   }
   ```

   The synthesizer:
   - Identifies convergent themes
   - Highlights contradictions and disagreements
   - Flags novel ideas only one agent found — these are the recall problem being solved, don't bury them
   - Ranks by feasibility and goal alignment
   - Is opinionated — clear recommendations, not summaries. Synthesized, not concatenated.

3. **Evaluate: another wave needed?** If the synthesizer reports unresolved disagreements, or a new idea emerged that needs investigation, spawn another wave (back to step 1 with focused forks on the unresolved points). Scale waves with importance:
   - **Low importance:** 1-2 waves total.
   - **Default:** 2-3 waves total.
   - **High importance:** up to 5 waves total.
   - Stop early if no new actionable ideas emerged in the last wave.

4. **Write artifact.** Run `session_lineage` (include_xml=false). You'll get JSON like:
   ```json
   { "root_team_key": "2026-04-07-11-24-my-task", "path": "procs/brainstorm", ... }
   ```
   Your artifact folder: `artifacts/{root_team_key}/{path}/`. Create it if it doesn't exist. Write `report.md` there. Include:
   - Ranked options with rationale
   - Dissenting views preserved prominently
   - What was NOT explored
   - State observations, not conclusions

   Then message your caller with a link to the report and the top recommendation.

## Edge Cases

- **Deep disagreement after max waves:** escalate to your caller with the competing views. They have context you don't.
- **One agent found something nobody else did:** flag it prominently. This is the recall problem being solved.
- **Needs information nobody has:** report as inconclusive with what information is missing.
- **A model is unavailable:** fall back to another provider. The goal is diverse reasoning, not a specific model.

## DON'Ts

- DON'T converge prematurely. Disagreement is information.
- DON'T dismiss minority views.
- DON'T let the synthesizer just concatenate findings. It must integrate and rank.
- DON'T let all agents explore the same conceptual space.
- DON'T use the same model for all forks — diversity of reasoning is the point.
