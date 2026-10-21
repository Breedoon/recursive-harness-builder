#!/usr/bin/env bash
# Positive control: the pinned 2.1.59 behind ascii_forwarder.py, i.e. a CLI
# whose request bodies are in the required ensure_ascii compact form.
REAL="${OBS_ASCII_CONTROL_REAL:-/workspace/runtime/reference/claude-code-src/pinned-2.1.59/claude}"
if [ "${1:-}" = "-v" ] || [ "${1:-}" = "--version" ]; then exec "$REAL" "$@"; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PF="$(mktemp)"
# the forwarder must not hold the SDK's stdout/stderr pipes open (the SDK
# waits for EOF on them), and must die with this wrapper
/workspace/obs/.venv/bin/python "$HERE/ascii_forwarder.py" "$PF" "${ANTHROPIC_BASE_URL:?}" \
  </dev/null >/dev/null 2>&1 &
FWD=$!
trap 'kill "$FWD" 2>/dev/null' EXIT TERM INT
for _ in $(seq 100); do [ -s "$PF" ] && break; sleep 0.05; done
export ANTHROPIC_BASE_URL="http://127.0.0.1:$(cat "$PF")"
rm -f "$PF"
"$REAL" "$@"
rc=$?
kill "$FWD" 2>/dev/null
exit $rc
