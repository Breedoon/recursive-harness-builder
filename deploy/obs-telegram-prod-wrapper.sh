#!/bin/bash
# supervisord wrapper for [program:obs-telegram-prod].
# Installed copy: /workspace/runtime/bin/obs-telegram-prod-wrapper.sh
# Install by atomic rename (cp to a temp file, then mv): bash reads a running
# script incrementally, so editing the live file in place can break the
# running wrapper. A new copy takes effect at the next OBS start.
#
# Restart semantics (docs/maintenance-restart.md):
# - TERM to this wrapper (supervisorctl stop/restart, container stop, and the
#   group SIGTERM of a maintenance or plain restart) touches the kill-switch
#   sentinel, so the next daemon discards the crash snapshot.
# - A dead cache proxy triggers a maintenance restart (SIGUSR1), falling back
#   to SIGKILL, so mid-turn agents resume instead of being lost (vault-u3b.73).
# - When telegram_main dies without this wrapper being told to stop (crash,
#   OOM kill), the rest of the process group is terminated before exiting, so
#   no Claude CLI is left orphaned (vault-u3b.66).
set -euo pipefail
cd /workspace/obs
ulimit -n 65536 || true
if [ ! -x /workspace/obs/.venv/bin/python ]; then
  echo "OBS venv missing at /workspace/obs/.venv/bin/python" >&2
  exit 2
fi
if [ ! -f /workspace/obs/.env ]; then
  echo "OBS env file missing at /workspace/obs/.env" >&2
  exit 2
fi
cleanup_cache_proxy() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:28925 -sTCP:LISTEN 2>/dev/null | xargs -r kill -TERM 2>/dev/null || true
  fi
}
cleanup_cache_proxy
LOCAL_LLM_AUTH_FILE="/run/secrets/obs-local-llm-auth"
if [[ ! -r "$LOCAL_LLM_AUTH_FILE" ]]; then
  printf 'required local LLM auth mount is unreadable: %s\n' "$LOCAL_LLM_AUTH_FILE" >&2
  exit 78
fi
IFS= read -r OBS_LOCAL_LLM_AUTH_TOKEN < "$LOCAL_LLM_AUTH_FILE"
if [[ -z "$OBS_LOCAL_LLM_AUTH_TOKEN" ]]; then
  printf 'required local LLM auth mount is empty\n' >&2
  exit 78
fi
export OBS_LOCAL_LLM_BASE_URL="http://host.docker.internal:8080"
export OBS_LOCAL_LLM_AUTH_TOKEN
unset OBS_LOCAL_LLM_API_KEY
KILLSWITCH_SENTINEL="${OBS_KILLSWITCH_SENTINEL:-/workspace/runtime/state/obs-telegram-prod.killswitch}"
export OBS_KILLSWITCH_SENTINEL="$KILLSWITCH_SENTINEL"
MAINTENANCE_EXIT_WAIT_SECONDS="${OBS_WRAPPER_MAINTENANCE_EXIT_WAIT_SECONDS:-30}"
/workspace/obs/.venv/bin/python -m obs_agent.telegram_main --prod &
child=$!
watchdog=

request_resuming_restart() {
  # Maintenance restart: telegram_main snapshots mid-turn agents and TERMs its
  # process group (including this wrapper). If it does not exit in time, SIGKILL
  # it; its periodic crash snapshot then resumes the agents.
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

watch_cache_proxy() {
  local healthy=0
  local failures=0
  local startup_deadline=$((SECONDS + 20))

  while kill -0 "$child" 2>/dev/null; do
    if curl -fsS --max-time 2 http://127.0.0.1:28925/health >/dev/null 2>&1; then
      healthy=1
      failures=0
    elif [ "$healthy" -eq 1 ]; then
      failures=$((failures + 1))
      if [ "$failures" -ge 3 ]; then
        echo "Cache proxy on port 28925 failed three health checks; maintenance-restarting obs-telegram-prod (mid-turn agents resume)" >&2
        request_resuming_restart
        return
      fi
    elif [ "$SECONDS" -ge "$startup_deadline" ]; then
      echo "Cache proxy on port 28925 did not become healthy within 20 seconds; maintenance-restarting obs-telegram-prod" >&2
      request_resuming_restart
      return
    fi
    sleep 5
  done
}

terminate_rest_of_group() {
  # telegram_main died without a stop request: take down what is left of this
  # process group (Claude CLIs, tool subprocesses) so nothing is orphaned.
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
  # Kill switch (or the group SIGTERM of a maintenance/plain restart): the next
  # daemon must not resume the crash snapshot.
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
