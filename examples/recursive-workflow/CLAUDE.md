# Fund Research Harness

This harness researches investment funds within a single asset class and identifies long-term winners. It does not build customer profiles or personalize recommendations — the goal is to find objectively strong funds based on performance data, risk metrics, and market context.

## Default behavior

For any fund research request, spawn a Router:

```json
{
  "prompt_file": "procedures/router.md",
  "prompt": "Research long-term winning funds for this request: {one-sentence summary}.",
  "fork": true,
  "hooks": {
    "PreToolUse": "hooks/router_guard.py::check"
  }
}
```

For simple single-fund lookups (e.g. "analyze VTI"), spawn a Loop directly:

```json
{
  "prompt_file": "procedures/loop.md",
  "prompt": "Analyze this fund: {ticker or fund name}.",
  "fork": true
}
```

When in doubt, use Router.

## Pipeline pattern

Every fund research request follows this two-phase pipeline. Scope and Router must recognize and apply it:

**Phase 1 — parallel:**
- `market_context`: pull macro and sector data relevant to the asset class
- `screener`: filter the fund universe and return a shortlist of candidates (50 funds)

**Phase 2 — after screener returns:**
- `fund_analyzer`: one per fund on the shortlist, run in parallel

**Final step:**
- Auditor validates the assembled analyses
- Router writes a ranked artifact: which funds are the strongest long-term candidates and why, grounded in the data

## Asset class focus

Each run targets one asset class (e.g. US large-cap equity, emerging market bonds, real estate). The asset class is specified in the user's request or inherited from context. Do not mix asset classes within a single research run.

## Data source

All quantitative data comes from the Alpha Vantage API. The API key is in the environment variable `ALPHA_VANTAGE_API_KEY`. Agents must not use general knowledge as a substitute for fetched data when making quantitative claims.

## Artifacts

All agents write artifacts under `artifacts/`. The Router's final artifact is the deliverable — a ranked list of funds with supporting evidence from screener, market context, and individual analyses.

## Contents

- `CLAUDE.md` — this file. Entry point for all agents.
- `procedures/` — Router, Scope, Loop, Executor, Verifier, Auditor, Unblock, Brainstorm (generic); Screener, FundAnalyzer, MarketContext (domain-specific).
- `hooks/router_guard.py` — prevents Routers from doing implementation work directly.
- `artifacts/` — all agent outputs land here.
