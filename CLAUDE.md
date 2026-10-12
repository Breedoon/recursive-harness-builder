@AGENTS.md

**Cache proxy warning (short form):** changes to `src/cache_proxy.py` or any
history save/fork/resume/replay code can silently re-bill every conversation.
Follow docs/cache-proxy-safety.md and run `.venv/bin/python scripts/cache_regression.py`
before committing. Never write the billing placeholder (regex `cch=0{5}`).
