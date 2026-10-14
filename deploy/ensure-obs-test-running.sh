#!/usr/bin/env bash
# Host systemd recovery helper. Install atomically at:
# /home/breedoon/infrastructure/obs-test/ensure-obs-test-running.sh
set -euo pipefail

container=${OBS_TEST_CONTAINER:-obs-test}
minimum_age=${OBS_TEST_CREATED_MIN_AGE_SECONDS:-30}
disable_file=/run/obs-test-container-recovery.disabled
poller_disable_file=${OBS_TELEGRAM_PROD_RECOVERY_DISABLE_FILE:-/home/breedoon/infrastructure/obs-test/obs-telegram-prod-recovery.disabled}
# This is the host side of the container's /workspace/runtime bind mount.
shutdown_file=${OBS_TELEGRAM_PROD_SHUTDOWN_FILE:-/data/scratch/obs-test/runtime/state/obs-telegram-prod.shutdown}

[ -e "$disable_file" ] && exit 0

details=$(docker inspect --format '{{.State.Status}}|{{.Created}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$container" 2>/dev/null) || exit 0
IFS='|' read -r state created_at compose_project compose_service <<< "$details"

if [ "$compose_project" != obs-test ] || [ "$compose_service" != app ]; then
  echo "Refusing to start unexpected container: name=$container project=$compose_project service=$compose_service" >&2
  exit 1
fi

if [ "$state" = running ]; then
  [ -e "$poller_disable_file" ] && exit 0

  supervisor_output=$(docker exec "$container" supervisorctl status obs-telegram-prod 2>/dev/null || true)
  supervisor_state=$(awk 'NR == 1 { print $2 }' <<< "$supervisor_output")
  if [ "$supervisor_state" = STOPPED ]; then
    # /shutdown[_autoresume] writes this before asking Supervisor to stop.
    # Recheck after observing STOPPED so a concurrent preparation cannot be
    # undone by a recovery tick that began while OBS was still running.
    [ -e "$shutdown_file" ] && exit 0
    echo "Starting obs-telegram-prod after detecting an explicit Supervisor stop"
    docker exec "$container" supervisorctl start obs-telegram-prod
  fi
  exit 0
fi

[ "$state" = created ] || exit 0

created_epoch=$(date -d "$created_at" +%s)
age=$(( $(date +%s) - created_epoch ))
[ "$age" -ge "$minimum_age" ] || exit 0

echo "Starting $container after it remained in Created state for ${age}s"
docker start "$container" >/dev/null

for _ in $(seq 1 20); do
  [ "$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)" = running ] && exit 0
  sleep 1
done

echo "$container did not reach Running state" >&2
exit 1
