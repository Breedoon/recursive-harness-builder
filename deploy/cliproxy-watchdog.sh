#!/usr/bin/env bash
# cliproxy-watchdog.sh — auto-recovery for CLIProxyAPI (127.0.0.1:8317).
#
# MODEL-AGNOSTIC BY DESIGN (2026-09-23): this script contains no model names.
# Everything it checks is derived at runtime from what the proxy actually
# serves (/v1/models), a persisted snapshot of the last-good catalog, and the
# upstream catalog source. Model renames (e.g. gpt-5.6-* → gpt-6-*) therefore
# need no watchdog edit. The previous version hardcoded gpt-5.6-sol/luna as
# probe/required ids and silently went stale when OBS moved to gpt-6-*.
#
# ---------------------------------------------------------------------------
# FAILURE MODE 0: listener dead / supervisor FATAL / foreign port owner
# ---------------------------------------------------------------------------
# Added 2026-09-23 after the "luna 502" incident. On a 13:06 container restart
# a deprecated Mac reverse SSH tunnel (-R 127.0.0.1:8317) grabbed 8317 before
# cliproxyapi bound it; supervisor exhausted startretries and left cliproxyapi
# FATAL for ~4h. Traffic silently went to the Mac's own proxy and died every
# time the Mac slept (cache proxy 502 "[Errno 111] Connection refused"). The
# old watchdog treated an unreachable proxy as UNKNOWN and never acted.
# (The tunnel jobs are removed and sshd now runs with PermitListen none; this
# path is the belt to those braces.)
#
# Every LIVENESS_INTERVAL_SECS (cheap; no model traffic) the watchdog:
#   * supervisor FATAL/EXITED/BACKOFF-stuck → free the port if held by a
#     foreign process, then `supervisorctl start|restart cliproxyapi`;
#   * listener on 8317 owned by a PID other than supervisor's cliproxyapi PID
#     → kill it if it is ours (agent-owned), otherwise ALERT (cannot kill a
#     root-owned owner such as an sshd forward), then restart cliproxyapi;
#   * supervisor RUNNING but nothing listening for longer than
#     LISTEN_GRACE_SECS (covers the startup egress gate, max 300s) → restart.
# A deliberate `supervisorctl stop` (STOPPED) is respected and never undone.
# Liveness restarts are throttled to one per LIVENESS_MIN_GAP_SECS but are not
# capped: a dead proxy is always worth another attempt.
#
# ---------------------------------------------------------------------------
# FAILURE MODE 1: stale cooldown (count_tokens probe returns 429)
# ---------------------------------------------------------------------------
# Root cause (verified 2026-08-13, CLIProxyAPI v7.2.49; upstream PRs
# #4248/#4399/#4426 still unmerged): after a real 429 "usage_limit_reached" the
# credential is marked unavailable in memory until upstream resets_at, even if
# capacity returns sooner. save-cooldown-status is false, so a restart clears
# it. The probe hits /v1/messages/count_tokens, computed locally (no upstream
# call/cost) but still behind the per-credential availability gate. It is run
# for EVERY served model id (≈1ms each), so any cooling credential is seen.
# Only 429 triggers this path; at most one restart per MIN_RESTART_GAP_SECS.
#
# ---------------------------------------------------------------------------
# FAILURE MODE 2: model catalog registry miss (a served model id vanishes)
# ---------------------------------------------------------------------------
# Added 2026-09-03 (~10h outage: one id silently dropped from /v1/models →
# misleading 503 auth_unavailable) and 2026-09-20/21 (boot served a stale
# embedded catalog → 502 "unknown provider for model X"). A restart refetches
# the catalog. Detection is snapshot-based: ids that were served in the last
# good catalog but are no longer served are a MISS — unless they have also
# disappeared from the upstream catalog source (legitimate deprecation), in
# which case they are quietly forgotten. Optional extra pins via
# CLIPROXY_WATCHDOG_REQUIRED_MODELS (default empty).
# Bounded: MAX_MODEL_RESTARTS attempts per incident with 0/10/30 min backoff,
# then give up permanently for that incident, with a persistent alert file and
# hourly re-alerts on stderr; auto-clears when the ids come back.
# An unfetchable/unparseable catalog is UNKNOWN and never triggers this path
# (liveness handles a dead proxy).

set -u

# --- configuration (env-overridable) ---
BASE_URL="${CLIPROXY_WATCHDOG_BASE_URL:-http://127.0.0.1:8317}"
API_KEY="${CLIPROXY_WATCHDOG_API_KEY:-sk-anything}"
PROGRAM="${CLIPROXY_WATCHDOG_PROGRAM:-cliproxyapi}"
SUPERVISORCTL="${CLIPROXY_WATCHDOG_SUPERVISORCTL:-supervisorctl}"
RESTART_CMD="${CLIPROXY_WATCHDOG_RESTART_CMD:-$SUPERVISORCTL restart $PROGRAM}"
STATE_DIR="${CLIPROXY_WATCHDOG_STATE_DIR:-/workspace/runtime/logs/cliproxy}"
CATALOG_SOURCE_URL="${CLIPROXY_WATCHDOG_CATALOG_SOURCE_URL:-https://raw.githubusercontent.com/router-for-me/models/refs/heads/main/models.json}"

CHECK_INTERVAL_SECS="${CLIPROXY_WATCHDOG_CHECK_INTERVAL_SECS:-300}"       # cooldown + catalog checks
LIVENESS_INTERVAL_SECS="${CLIPROXY_WATCHDOG_LIVENESS_INTERVAL_SECS:-30}"  # liveness tick
LIVENESS_MIN_GAP_SECS="${CLIPROXY_WATCHDOG_LIVENESS_MIN_GAP_SECS:-120}"
LISTEN_GRACE_SECS="${CLIPROXY_WATCHDOG_LISTEN_GRACE_SECS:-360}"           # > egress-gate timeout (300s)
BACKOFF_STUCK_SECS="${CLIPROXY_WATCHDOG_BACKOFF_STUCK_SECS:-120}"
MIN_RESTART_GAP_SECS="${CLIPROXY_WATCHDOG_MIN_RESTART_GAP_SECS:-600}"     # 429 path

# Boot-race coverage (2026-09-21): run the full check immediately and then at
# BOOT_FAST_INTERVAL_SECS for the first BOOT_FAST_WINDOW_SECS.
BOOT_FAST_WINDOW_SECS="${CLIPROXY_WATCHDOG_BOOT_FAST_WINDOW_SECS:-600}"
BOOT_FAST_INTERVAL_SECS="${CLIPROXY_WATCHDOG_BOOT_FAST_INTERVAL_SECS:-30}"

EXTRA_REQUIRED_MODELS="${CLIPROXY_WATCHDOG_REQUIRED_MODELS:-}"
MAX_MODEL_RESTARTS="${CLIPROXY_WATCHDOG_MAX_MODEL_RESTARTS:-3}"
POST_RESTART_WAIT_SECS="${CLIPROXY_WATCHDOG_POST_RESTART_WAIT_SECS:-15}"
REALERT_INTERVAL_SECS="${CLIPROXY_WATCHDOG_REALERT_INTERVAL_SECS:-3600}"
RUN_ONCE="${CLIPROXY_WATCHDOG_ONCE:-0}"

PORT="${BASE_URL##*:}"; PORT="${PORT%%/*}"
PROXY_URL="$BASE_URL/v1/messages/count_tokens?beta=true"
MODELS_URL="$BASE_URL/v1/models"

STATE_FILE="$STATE_DIR/watchdog-last-restart"
LIVENESS_STATE_FILE="$STATE_DIR/watchdog-last-liveness-restart"
MODEL_STATE_FILE="$STATE_DIR/watchdog-model-state"
SNAPSHOT_FILE="$STATE_DIR/watchdog-catalog-snapshot"
ALERT_FILE="$STATE_DIR/watchdog-model-alert.txt"
LOG_FILE="$STATE_DIR/watchdog.log"

mkdir -p "$STATE_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"; }
alert() {
  log "ALERT: $*"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cliproxy-watchdog ALERT: $*" >&2
}

# --- shared-service freeze guard (bead ops-47.6, 2026-10-20) ---
# A freeze declared with scripts/ops/freeze.py (scope cliproxy/supervisor/all-shared) means a
# qualification gate depends on the proxy's exact running identity. The Bash freeze hook cannot see
# restarts this script performs itself, so it asks freeze.py before each restart.
# freeze_blocks "<command>" → 0 (true) if an active freeze covers it; 1 otherwise. Fails open:
# missing tool, timeout or any other exit code counts as "not blocked".
FREEZE_TOOL="${CLIPROXY_WATCHDOG_FREEZE_TOOL:-/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops/freeze.py}"
freeze_blocks() {
  [ -f "$FREEZE_TOOL" ] || return 1
  local cmd="$1" out rc
  [ -n "${OPS_FREEZE_ACK:-}" ] && cmd="OPS_FREEZE_ACK=${OPS_FREEZE_ACK} $cmd"
  out=$(timeout 10 python3 "$FREEZE_TOOL" check --command "$cmd" 2>/dev/null); rc=$?
  if [ "$rc" -eq 3 ]; then
    FREEZE_REASON=$(printf '%s' "$out" | sed -n '2p')
    return 0
  fi
  return 1
}

read_epoch_file() {
  local v=0
  [ -f "$1" ] && v=$(cat "$1" 2>/dev/null || echo 0)
  case "$v" in ''|*[!0-9]*) v=0 ;; esac
  echo "$v"
}

# ============================================================================
# Failure mode 0: liveness
# ============================================================================

# Sets sv_state, sv_pid, sv_uptime_secs from `supervisorctl status`.
supervisor_status() {
  local line
  line=$($SUPERVISORCTL status "$PROGRAM" 2>/dev/null | head -1)
  sv_state=$(printf '%s' "$line" | awk '{print $2}')
  sv_pid=$(printf '%s' "$line" | sed -n 's/.*pid \([0-9]*\),.*/\1/p')
  local up
  up=$(printf '%s' "$line" | sed -n 's/.*uptime \(.*\)$/\1/p')
  sv_uptime_secs=$(printf '%s' "$up" | awk '{
      n=split($NF,t,":"); s=(n==3)?t[1]*3600+t[2]*60+t[3]:0;
      if ($0 ~ /day/) s+=$1*86400; print s+0 }')
  [ -n "$sv_uptime_secs" ] || sv_uptime_secs=0
}

# Echoes "<pid>" of the 8317 listener if visible, "-" if a listener exists but
# its owner is not visible to us (e.g. root-owned sshd forward), "" if none.
listener_owner() {
  local line
  line=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | head -1)
  [ -n "$line" ] || { echo ""; return; }
  local pid
  pid=$(printf '%s' "$line" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
  [ -n "$pid" ] && echo "$pid" || echo "-"
}

liveness_restart() {  # $1 = reason, $2 = supervisor verb (start|restart)
  local now last
  now=$(date +%s); last=$(read_epoch_file "$LIVENESS_STATE_FILE")
  if [ $(( now - last )) -lt "$LIVENESS_MIN_GAP_SECS" ]; then
    log "liveness: $1 — throttled ($(( now - last ))s since last liveness restart, min ${LIVENESS_MIN_GAP_SECS}s)"
    return 0
  fi
  # Liveness restarts revive a DEAD proxy (same binary, same config), so a freeze does not stop them:
  # leaving the fleet without a proxy is worse than the identity risk. They are alerted as such.
  if freeze_blocks "$SUPERVISORCTL $2 $PROGRAM"; then
    alert "liveness: $1 — restarting a DEAD proxy despite an active freeze (${FREEZE_REASON:-see freeze.py status}); tell the freeze owner"
  fi
  alert "liveness: $1 — running '$SUPERVISORCTL $2 $PROGRAM'"
  $SUPERVISORCTL "$2" "$PROGRAM" >> "$LOG_FILE" 2>&1
  echo "$now" > "$LIVENESS_STATE_FILE"
  echo "$now" > "$STATE_FILE"   # shared throttle with the 429 path
}

# Try to free the port from a foreign owner. Returns 0 if the port is free
# afterwards (or was already free), 1 if an unkillable foreign owner remains.
evict_foreign_owner() {  # $1 = owner ("" | "-" | pid)
  local owner="$1"
  [ -z "$owner" ] && return 0
  if [ "$owner" = "-" ]; then
    alert "liveness: port $PORT is held by a process not visible to user $(id -un) (likely root-owned, e.g. an sshd -R forward). Cannot evict — investigate with root: ss -ltnp 'sport = :$PORT'"
    return 1
  fi
  local desc
  desc=$(ps -o user=,args= -p "$owner" 2>/dev/null | cut -c1-200)
  alert "liveness: port $PORT held by foreign pid $owner ($desc) — terminating it"
  kill "$owner" 2>/dev/null
  local i
  for i in 1 2 3 4 5; do
    sleep 1
    [ -z "$(listener_owner)" ] && return 0
  done
  kill -9 "$owner" 2>/dev/null; sleep 1
  [ -z "$(listener_owner)" ]
}

check_liveness() {
  supervisor_status
  local owner
  owner=$(listener_owner)

  case "$sv_state" in
    RUNNING)
      if [ -n "$owner" ] && [ "$owner" != "$sv_pid" ]; then
        # A foreign process holds the port while ours runs (and cannot bind).
        evict_foreign_owner "$owner" && liveness_restart "foreign owner on :$PORT (pid $owner) while $PROGRAM pid $sv_pid could not bind" restart
        return 0
      fi
      if [ -z "$owner" ] && [ "$sv_uptime_secs" -ge "$LISTEN_GRACE_SECS" ]; then
        liveness_restart "$PROGRAM RUNNING for ${sv_uptime_secs}s but nothing listens on :$PORT" restart
      fi
      ;;
    FATAL|EXITED|UNKNOWN)
      evict_foreign_owner "$owner"
      liveness_restart "$PROGRAM is $sv_state (listener owner: ${owner:-none})" start
      ;;
    BACKOFF|STARTING)
      # BACKOFF repeating usually means "address already in use".
      if [ -n "$owner" ]; then
        evict_foreign_owner "$owner"
        liveness_restart "$PROGRAM is $sv_state while :$PORT is held by ${owner}" restart
      fi
      ;;
    STOPPED|STOPPING)
      : ;;  # deliberate operator stop — respect it
    "")
      log "liveness: could not read supervisor status for $PROGRAM"
      ;;
  esac
}

# ============================================================================
# Failure mode 1: stale cooldown
# ============================================================================

# Echoes served model ids, one per line ("" on failure).
served_model_ids() {
  curl -s -m 20 "$MODELS_URL" -H "Authorization: Bearer $API_KEY" 2>/dev/null | python3 -c '
import sys, json
try:
    for m in json.load(sys.stdin).get("data", []):
        if isinstance(m, dict) and m.get("id"):
            print(m["id"])
except Exception:
    pass' 2>/dev/null
}

# Echoes the model ids whose count_tokens probe returned 429.
cooling_models() {
  local m code
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "$PROXY_URL" \
      -X POST -H "x-api-key: $API_KEY" -H "content-type: application/json" \
      -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}")
    [ "$code" = "429" ] && printf '%s ' "$m"
  done
}

handle_cooldown() {
  local ids="$1" cooling now last gap
  [ -n "$ids" ] || return 0
  cooling=$(printf '%s\n' "$ids" | cooling_models)
  [ -n "$cooling" ] || return 0
  log "probe returned 429 for [${cooling% }] (stale-cooldown failure mode) — evaluating restart"
  now=$(date +%s); last=$(read_epoch_file "$STATE_FILE"); gap=$(( now - last ))
  if [ "$gap" -lt "$MIN_RESTART_GAP_SECS" ]; then
    log "skip restart: only ${gap}s since last auto-restart (min ${MIN_RESTART_GAP_SECS}s)"
    return 0
  fi
  if freeze_blocks "$RESTART_CMD"; then
    alert "cooldown 429 restart SKIPPED: active freeze (${FREEZE_REASON:-see freeze.py status}); will re-check next cycle"
    return 0
  fi
  log "restarting $PROGRAM (cooldown 429)"
  $RESTART_CMD >> "$LOG_FILE" 2>&1
  echo "$now" > "$STATE_FILE"
}

# ============================================================================
# Failure mode 2: catalog miss (snapshot-based, model-agnostic)
# ============================================================================

ms_key=""; ms_attempts=0; ms_last_attempt=0; ms_given_up=0; ms_last_alert=0

load_model_state() {
  ms_key=""; ms_attempts=0; ms_last_attempt=0; ms_given_up=0; ms_last_alert=0
  [ -s "$MODEL_STATE_FILE" ] || return 0
  IFS='|' read -r ms_key ms_attempts ms_last_attempt ms_given_up ms_last_alert < "$MODEL_STATE_FILE"
  case "$ms_attempts" in ''|*[!0-9]*) ms_attempts=0 ;; esac
  case "$ms_last_attempt" in ''|*[!0-9]*) ms_last_attempt=0 ;; esac
  case "$ms_given_up" in ''|*[!0-9]*) ms_given_up=0 ;; esac
  case "$ms_last_alert" in ''|*[!0-9]*) ms_last_alert=0 ;; esac
}
save_model_state() {
  printf '%s|%s|%s|%s|%s\n' "$ms_key" "$ms_attempts" "$ms_last_attempt" "$ms_given_up" "$ms_last_alert" > "$MODEL_STATE_FILE"
}
clear_model_state() {
  rm -f "$MODEL_STATE_FILE" "$ALERT_FILE"
  ms_key=""; ms_attempts=0; ms_last_attempt=0; ms_given_up=0; ms_last_alert=0
}

BACKOFF_1_SECS="${CLIPROXY_WATCHDOG_BACKOFF_1_SECS:-0}"
BACKOFF_2_SECS="${CLIPROXY_WATCHDOG_BACKOFF_2_SECS:-600}"
BACKOFF_N_SECS="${CLIPROXY_WATCHDOG_BACKOFF_N_SECS:-1800}"
backoff_for_attempt() {
  case "$1" in 1) echo "$BACKOFF_1_SECS" ;; 2) echo "$BACKOFF_2_SECS" ;; *) echo "$BACKOFF_N_SECS" ;; esac
}

# Given served ids on stdin, echoes the space-separated missing ids, "" if
# none, or UNKNOWN if the served catalog is empty. Maintains the snapshot:
# on a clean check it becomes the current served set; vanished ids that are
# also gone from the upstream catalog source are forgotten (deprecations).
read -r -d '' MISSING_PY <<'PY'
import json, os, sys, urllib.request
snap_path, src_url, extra = sys.argv[1], sys.argv[2], sys.argv[3].split()
served = {l.strip() for l in sys.stdin if l.strip()}
if not served:
    print("UNKNOWN"); sys.exit(0)
prev = set()
if os.path.exists(snap_path):
    prev = {l.strip() for l in open(snap_path) if l.strip()}
tracked = prev | set(extra)
vanished = tracked - served
missing = set(vanished)
if vanished:
    try:
        with urllib.request.urlopen(src_url, timeout=15) as r:
            src = json.load(r)
        upstream = {m.get("id") for sec in src.values() if isinstance(sec, list)
                    for m in sec if isinstance(m, dict)}
        if upstream:
            # Deprecated upstream → not a miss (unless explicitly pinned).
            missing = {m for m in vanished if m in upstream or m in extra}
    except Exception:
        pass  # source unreachable: treat every vanished id as missing
forgotten = vanished - missing
new_snap = served if not missing else (prev - forgotten)
tmp = snap_path + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(sorted(new_snap)) + "\n")
os.replace(tmp, snap_path)
if forgotten:
    sys.stderr.write("forgotten (gone upstream): " + " ".join(sorted(forgotten)) + "\n")
print(" ".join(sorted(missing)))
PY
missing_models_from() {
  python3 -c "$MISSING_PY" "$SNAPSHOT_FILE" "$CATALOG_SOURCE_URL" "$EXTRA_REQUIRED_MODELS" 2>>"$LOG_FILE" || echo "UNKNOWN"
}

handle_model_catalog() {
  local ids="$1" missing now
  missing=$(printf '%s\n' "$ids" | missing_models_from)
  now=$(date +%s)
  [ "$missing" = "UNKNOWN" ] && return 0

  load_model_state
  if [ -z "$missing" ]; then
    if [ -n "$ms_key" ]; then
      log "model catalog RECOVERED: [$ms_key] present again after $ms_attempts restart attempt(s) — clearing alert"
      [ "$ms_given_up" = "1" ] && alert "RESOLVED — previously-abandoned model(s) [$ms_key] are back in the catalog; watchdog re-armed"
      clear_model_state
    fi
    return 0
  fi

  if [ "$missing" != "$ms_key" ]; then
    log "model catalog MISS: [$missing] previously served but absent from $MODELS_URL (still listed upstream)"
    ms_key="$missing"; ms_attempts=0; ms_last_attempt=0; ms_given_up=0; ms_last_alert=0
    save_model_state
  fi

  if [ "$ms_given_up" = "1" ]; then
    if [ $(( now - ms_last_alert )) -ge "$REALERT_INTERVAL_SECS" ]; then
      alert "still unresolved: model(s) [$ms_key] missing from catalog after $ms_attempts failed restart(s); watchdog STOPPED restarting (cap $MAX_MODEL_RESTARTS). See $ALERT_FILE"
      ms_last_alert="$now"; save_model_state
    fi
    return 0
  fi

  if [ "$ms_attempts" -ge "$MAX_MODEL_RESTARTS" ]; then
    ms_given_up=1; ms_last_alert="$now"; save_model_state
    {
      echo "cliproxy-watchdog ALERT — model catalog self-heal FAILED"
      echo "raised_at:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "missing_models:   $ms_key"
      echo "restart_attempts: $ms_attempts (cap $MAX_MODEL_RESTARTS)"
      echo "catalog_url:      $MODELS_URL"
      echo "snapshot:         $SNAPSHOT_FILE"
      echo
      echo "These ids were served before and are still in the upstream catalog"
      echo "($CATALOG_SOURCE_URL), but $PROGRAM no longer serves them after"
      echo "$ms_attempts restart(s). Restarting has STOPPED for this incident."
      echo "Check main.log for 'model refresh' lines, credentials, and container DNS."
      echo "If the removal is intended, delete the ids from the snapshot file."
      echo
      echo "This file is removed automatically if the model(s) reappear."
    } > "$ALERT_FILE"
    alert "GIVING UP on model(s) [$ms_key] after $ms_attempts failed restart(s); details in $ALERT_FILE"
    return 0
  fi

  local attempt gap_needed elapsed recheck
  attempt=$(( ms_attempts + 1 ))
  gap_needed=$(backoff_for_attempt "$attempt")
  elapsed=$(( now - ms_last_attempt ))
  if [ "$ms_last_attempt" -ne 0 ] && [ "$elapsed" -lt "$gap_needed" ]; then
    log "model catalog MISS [$ms_key]: waiting backoff before attempt $attempt/$MAX_MODEL_RESTARTS (${elapsed}s of ${gap_needed}s)"
    return 0
  fi

  if freeze_blocks "$RESTART_CMD"; then
    alert "model catalog MISS [$ms_key]: restart SKIPPED, active freeze (${FREEZE_REASON:-see freeze.py status}); attempt not counted, re-checks next cycle"
    return 0
  fi
  log "model catalog MISS [$ms_key]: restarting $PROGRAM (attempt $attempt/$MAX_MODEL_RESTARTS)"
  $RESTART_CMD >> "$LOG_FILE" 2>&1
  ms_attempts="$attempt"; ms_last_attempt="$now"; save_model_state
  echo "$now" > "$STATE_FILE"

  sleep "$POST_RESTART_WAIT_SECS"
  recheck=$(served_model_ids | missing_models_from)
  if [ "$recheck" = "UNKNOWN" ]; then
    log "model catalog post-restart recheck inconclusive (proxy not answering yet) — next cycle"
  elif [ -z "$recheck" ]; then
    log "model catalog RECOVERED: [$ms_key] present again after restart attempt $attempt — clearing alert"
    clear_model_state
  else
    log "model catalog still missing [$recheck] after restart attempt $attempt/$MAX_MODEL_RESTARTS"
  fi
}

full_check() {
  local ids
  ids=$(served_model_ids)
  if [ -z "$ids" ]; then
    log "full check: $MODELS_URL not answering — liveness path owns this"
    return 0
  fi
  handle_cooldown "$ids"
  handle_model_catalog "$ids"
}

log "watchdog started (model-agnostic; liveness=${LIVENESS_INTERVAL_SECS}s, full=${CHECK_INTERVAL_SECS}s, boot_fast=${BOOT_FAST_INTERVAL_SECS}s/${BOOT_FAST_WINDOW_SECS}s, extra_required='${EXTRA_REQUIRED_MODELS}', max_model_restarts=${MAX_MODEL_RESTARTS})"

WATCHDOG_START=$(date +%s)
last_full=0
while true; do
  check_liveness
  now=$(date +%s)
  interval="$CHECK_INTERVAL_SECS"
  [ $(( now - WATCHDOG_START )) -lt "$BOOT_FAST_WINDOW_SECS" ] && interval="$BOOT_FAST_INTERVAL_SECS"
  if [ "$last_full" -eq 0 ] || [ $(( now - last_full )) -ge "$interval" ]; then
    full_check
    last_full=$(date +%s)
  fi
  [ "$RUN_ONCE" = "1" ] && break
  sleep "$LIVENESS_INTERVAL_SECS"
done
