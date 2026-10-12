#!/bin/sh
# Redacting filter for anything you read while working on the cache proxy.
#
#   <command> | scripts/redact.sh
#   e.g. tr '\0' '\n' < /proc/<pid>/environ | scripts/redact.sh
#        tail -50 /workspace/runtime/logs/obs-telegram-prod.stderr.log | scripts/redact.sh
#
# Hides, in this order:
#   1. the Claude Code billing placeholder (pattern cch=0{5}) -> cch=<PH>
#      (an agent that has it in context stops caching: docs/cache-proxy-safety.md)
#   2. KEY=VALUE lines/fields whose KEY looks secret (TOKEN, KEY, SECRET,
#      PASSWORD, PASSWD, SESSION, AUTH, COOKIE, CREDENTIAL, PAT, HASH) -> KEY=<redacted>
#   3. "Bearer <token>" and JSON fields named like the keys above -> <redacted>
#   4. well-known token shapes: sk-..., ghp_/gho_/github_pat_..., xox?-...,
#      Telegram bot tokens (digits:35+ chars), AWS AKIA...
#   5. JWTs (eyJ...x.y.z) and any unbroken base64/hex-like run of 80+
#      characters without '/' or '.' (session strings, encoded cookies)
#      -> <redacted-long>; file paths survive because they contain '/'
# It is a best-effort filter, not a guarantee: still never paste raw env,
# credential files or unfiltered logs into prompts, notes or messages.
LC_ALL=C exec sed -E \
  -e 's/cch=0{5}/cch=<PH>/g' \
  -e 's/([A-Za-z0-9_]*(TOKEN|KEY|SECRET|PASSWORD|PASSWD|SESSION|AUTH|COOKIE|CREDENTIAL|_PAT|HASH)[A-Za-z0-9_]*)=[^[:space:]"'"'"',;]+/\1=<redacted>/Ig' \
  -e 's/(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+\/=-]+/\1 <redacted>/Ig' \
  -e 's/("[A-Za-z0-9_-]*(token|key|secret|password|session|auth|cookie|credential)[A-Za-z0-9_-]*"[[:space:]]*:[[:space:]]*)"[^"]*"/\1"<redacted>"/Ig' \
  -e 's/sk-[A-Za-z0-9_-]{16,}/sk-<redacted>/g' \
  -e 's/(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/<redacted-github>/g' \
  -e 's/github_pat_[A-Za-z0-9_]{20,}/<redacted-github>/g' \
  -e 's/xox[abpr]-[A-Za-z0-9-]{10,}/<redacted-slack>/g' \
  -e 's/[0-9]{6,12}:[A-Za-z0-9_-]{30,}/<redacted-bot-token>/g' \
  -e 's/AKIA[0-9A-Z]{16}/<redacted-aws>/g' \
  -e 's/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/<redacted-jwt>/g' \
  -e 's/[A-Za-z0-9+_=-]{80,}/<redacted-long>/g'
