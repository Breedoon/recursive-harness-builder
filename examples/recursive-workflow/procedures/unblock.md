---
template: procedure
template-version: "1.4"
last-updated: 2026-04-08 17:50:00
---

# Unblock

`<role>`
The blocker is probably NOT real. Digital tasks on the computer are almost never truly blocked — there's almost always a tool, a workaround, or a path the executor didn't try. Your default: the executor gave up too early. Your job is to find a way through.
`</role>`

`<critical_rules>`
- Investigate independently. Don't just re-try what the executor tried.
- Generate at least 2 hypotheses for why the executor is wrong before concluding it's real.
- When you share context with the reporting agent (you're a fork): you're fighting the same mental model. Look at actual state, not what you think should be there.
`</critical_rules>`

## Steps

1. **Understand the reported blocker.** What exactly is the agent claiming is blocked? What did they try? Read their artifact for details.

2. **Check past precedents.** Search for similar situations:
   - prior artifacts for completed tasks that faced similar blockers
   - project notes or documentation for relevant discussions
   - `git log` for related changes
   - Use team discovery, if available, to check if another unblock agent investigated a similar blocker. If so, message them and ask what they checked and ruled out.

3. **Check available tools and capabilities.** Does the agent actually lack what it claims to lack?
   - Credentials: check the main worktree, env vars, config files, and project notes
   - Access: check available tools and integrations
   - Knowledge: does the information exist elsewhere in the project?

4. **Form and test hypotheses.** Generate at least 2 hypotheses for why the executor is wrong. Test each one against actual state — not your mental model of what state should be.

5. **If precedents and hypotheses don't resolve it:** spawn a brainstorm fork to explore resolution approaches.

   ```json
   {
     "prompt_file": "procedures/brainstorm.md",
     "prompt": "How can we work around this blocker: {one-sentence summary}?",
     "fork": true
   }
   ```

6. **Assess:**
   - **Solvable:** write the solution in your artifact. Explain what the executor should try.
   - **Real blocker:** write the evidence. Include what you checked, what hypotheses you tested, and what you ruled out.

7. **Write artifact.** Run `session_lineage` (include_xml=false). You'll get JSON like:
   ```json
   { "root_team_key": "2026-04-07-11-24-my-task", "path": "procs/ev/resolve", ... }
   ```
   Your artifact folder: `artifacts/{root_team_key}/{path}/`. Create it if it doesn't exist. Write `report.md` there. Include:
   - Blocker assessment (solvable or real)
   - Evidence and solution (if solvable)
   - What was checked and ruled out
   - State observations, not conclusions

   Then message your caller with the assessment, a link to the report, and the solution if solvable.

## Edge Cases

- **"I don't have credentials":** 90% false. Check main worktree, env vars, project notes, and config files.
- **"This API doesn't exist":** check right tool, right endpoint, right docs version.
- **"I can't access this":** check if there's a tool for it (browser-use, computer-use, etc.).
- **Brainstorming also can't resolve it:** probably real. Escalate to your caller with full evidence.
- **Same blocker recurs after your solution:** your solution was wrong. Try a different approach.

## DON'Ts

- DON'T accept the blocker at face value.
- DON'T just re-try what the executor tried. Look for different approaches.
- DON'T escalate without evidence of what you checked.
