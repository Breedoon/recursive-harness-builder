#!/usr/bin/env bash
# Recording wrapper for the SDK <-> CLI stream-json protocol (obs-22 E3).
#
# Give this script as ClaudeAgentOptions(cli_path=...). It records exactly what
# the Python SDK writes to the CLI's stdin and what the CLI writes to stdout,
# then behaves like the real binary (same argv, exit status, stderr).
#
#   OBS_CLI_TEE_REAL  the real binary (default: the pinned 2.1.59 reference copy)
#   OBS_CLI_TEE_DIR   capture root; one sub-directory per CLI invocation
#
# Files per invocation: argv.txt (one arg per line), env-names.txt (variable
# NAMES only, never values), stdin.ndjson, stdout.ndjson, exit.txt.
# The streams can contain conversation text; read them through
# scripts/redact.sh, and never commit them to a repository.
set -u
REAL="${OBS_CLI_TEE_REAL:-/workspace/runtime/reference/claude-code-src/pinned-2.1.59/claude}"
ROOT="${OBS_CLI_TEE_DIR:-/workspace/runtime/e2e/fixtures/protocol-capture/raw}"
if [ "${1:-}" = "-v" ] || [ "${1:-}" = "--version" ]; then
  exec "$REAL" "$@"
fi
D="$ROOT/$(date -u +%Y%m%dT%H%M%S%N)-$$"
mkdir -p "$D"
printf '%s\n' "$@" > "$D/argv.txt"
env | cut -d= -f1 | sort > "$D/env-names.txt"
tee "$D/stdin.ndjson" | "$REAL" "$@" | tee "$D/stdout.ndjson"
rc=${PIPESTATUS[1]}
echo "$rc" > "$D/exit.txt"
exit "$rc"
