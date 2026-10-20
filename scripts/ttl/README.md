# Claude cache-TTL tooling (vault-h6yt)

Moved here from obs-15 E8's run artifacts by obs-15 R-E (re-check 4 P5s-5/N6-6) so the delayed checks
vault-h6yt.1-.4 and future cache work have a durable, versioned home. Background:
`docs/cache-proxy-safety.md` mechanism 11 and the vault note
`Projects/Personal Projects/OBS Platform/Cache Prefix Stability/2026-10-20 Claude TTL Latch Fix.md`.

- `repro_ttl.py` + `capture_server.py` - deterministic real-CLI repro of the CLI's per-process TTL latch.
  Runs the bundled CLI in an isolated temp HOME with a FAKE OAuth credential against a local capture
  server (never the real proxy or the real token), with ~/.claude.json copied and the
  `tengu_prompt_cache_1h_config` GrowthBook feature present or removed. Expected: present -> 1h,
  removed -> 5m, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 -> 5m. Read-only on the real ~/.claude.json.
- `ttl_census.py [YYYY-MM-DD ...]` - census of system-breakpoint TTLs per proxy epoch and session for
  Opus/Sonnet requests in the cache-proxy request log (default: today, UTC).
- `epoch_pin.py <proxy_pid> [YYYY-MM-DD]` - incoming (pre) vs forwarded (wire) TTLs for one proxy epoch;
  shows the pin's `upgraded` / `kept-5m` branches firing (pre 5m, wire 1h = upgrade applied).

Delayed-check census command (what vault-h6yt.1-.4 run):
`python3 scripts/ttl/ttl_census.py <day>` then `python3 scripts/ttl/epoch_pin.py <current proxy pid> <day>`,
plus `python3 scripts/cache_tally.py --since <last check> --breaks-out`.
