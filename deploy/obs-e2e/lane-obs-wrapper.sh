#!/bin/bash
# Lane-only counterpart of deploy/obs-telegram-prod-wrapper.sh.
set -euo pipefail
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  printf 'Usage: lane-obs-wrapper.sh\n'
  printf 'Start lane OBS using OBS_E2E_CODE_DIR and OBS_CACHE_PROXY_PORT.\n'
  exit 0
fi
cd "${OBS_E2E_CODE_DIR:?OBS_E2E_CODE_DIR is required}"
: "${OBS_CACHE_PROXY_PORT:?OBS_CACHE_PROXY_PORT is required}"
ulimit -n 65536 || true
if [ ! -x /workspace/obs/.venv/bin/python ]; then
  echo "OBS venv missing at /workspace/obs/.venv/bin/python" >&2
  exit 2
fi
cleanup_cache_proxy() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$OBS_CACHE_PROXY_PORT" -sTCP:LISTEN 2>/dev/null | xargs -r kill -TERM 2>/dev/null || true
  fi
}
cleanup_cache_proxy
KILLSWITCH_SENTINEL="${OBS_KILLSWITCH_SENTINEL:-/workspace/runtime/state/obs-telegram-prod.killswitch}"
export OBS_KILLSWITCH_SENTINEL="$KILLSWITCH_SENTINEL"
MAINTENANCE_EXIT_WAIT_SECONDS="${OBS_WRAPPER_MAINTENANCE_EXIT_WAIT_SECONDS:-30}"
/workspace/obs/.venv/bin/python -m obs_agent.telegram_main --prod &
child=$!
watchdog=

request_resuming_restart() {
  # Maintenance restart preserves mid-turn agents; a hung daemon falls back to
  # its periodic crash snapshot when the bounded wait ends in SIGKILL.
  kill -USR1 "$child" 2>/dev/null || true
  local waited=0
  while kill -0 "$child" 2>/dev/null; do
    if [ "$waited" -ge "$MAINTENANCE_EXIT_WAIT_SECONDS" ]; then
      echo "telegram_main still running ${MAINTENANCE_EXIT_WAIT_SECONDS}s after SIGUSR1; sending SIGKILL (crash snapshot resumes agents)" >&2
      kill -KILL "$child" 2>/dev/null || true
      return
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

# CACHE-PREFIX WARNING: this watchdog restarts the lane cache proxy that every
# lane request goes through. Check its /health source_sha256 and run the cache
# regression after proxy changes (docs/cache-proxy-safety.md).
watch_cache_proxy() {
  local healthy=0
  local failures=0
  local startup_deadline=$((SECONDS + 20))

  while kill -0 "$child" 2>/dev/null; do
    if curl -fsS --max-time 2 "http://127.0.0.1:${OBS_CACHE_PROXY_PORT}/health" >/dev/null 2>&1; then
      healthy=1
      failures=0
    elif [ "$healthy" -eq 1 ]; then
      failures=$((failures + 1))
      if [ "$failures" -ge 3 ]; then
        echo "Cache proxy on port ${OBS_CACHE_PROXY_PORT} failed three health checks; maintenance-restarting obs-telegram-prod (mid-turn agents resume)" >&2
        request_resuming_restart
        return
      fi
    elif [ "$SECONDS" -ge "$startup_deadline" ]; then
      echo "Cache proxy on port ${OBS_CACHE_PROXY_PORT} did not become healthy within 20 seconds; maintenance-restarting obs-telegram-prod" >&2
      request_resuming_restart
      return
    fi
    sleep 5
  done
}

terminate_rest_of_group() {
  # INVARIANT(process-identity): this wrapper runs only inside a lane PID namespace, never next to production. Breaking it lets the lane OBS's restart and reaper code act on production processes.
  # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
  trap '' TERM INT
  local pgid
  pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
  if [ "$pgid" != "$$" ]; then
    return 0
  fi
  kill -TERM -- "-$pgid" 2>/dev/null || true
  local waited=0
  while [ "$waited" -lt 5 ]; do
    if [ -z "$(pgrep -g "$pgid" 2>/dev/null | grep -vx "$$" || true)" ]; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  local pid
  for pid in $(pgrep -g "$pgid" 2>/dev/null || true); do
    if [ "$pid" != "$$" ]; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  echo "Terminated leftover obs-telegram-prod process-group members after telegram_main exited" >&2
}

watch_cache_proxy &
watchdog=$!
term_handler() {
  trap '' TERM INT
  # A stop request discards the crash snapshot on the next daemon start.
  touch "$KILLSWITCH_SENTINEL" 2>/dev/null || true
  kill -TERM "$watchdog" 2>/dev/null || true
  kill -TERM "$child" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  cleanup_cache_proxy
  exit 143
}
trap term_handler TERM INT
set +e
wait "$child"
status=$?
set -e
kill -TERM "$watchdog" 2>/dev/null || true
wait "$watchdog" 2>/dev/null || true
echo "telegram_main exited with status $status without a stop request; OBS restarts and the crash snapshot resumes mid-turn agents" >&2
terminate_rest_of_group
cleanup_cache_proxy
exit "$status"
