#!/bin/sh
set -eu

umask 007

: "${TOOLBOX_ACTIVE_VAULT:?TOOLBOX_ACTIVE_VAULT is required}"
[ "$TOOLBOX_ACTIVE_VAULT" = "/workspace/runtime/git/obs-vault-active" ] || {
  printf '%s\n' "unexpected active vault path" >&2
  exit 64
}
[ -d "$TOOLBOX_ACTIVE_VAULT" ] || {
  printf '%s\n' "active vault mount is unavailable" >&2
  exit 64
}

mkdir -p \
  "$HOME" \
  "$XDG_CONFIG_HOME" \
  "$XDG_CACHE_HOME" \
  "$XDG_DATA_HOME" \
  "$TOOLBOX_WORKSPACE"

case "${1:-idle}" in
  idle)
    exec sleep infinity
    ;;
  self-test)
    shift
    exec /opt/toolbox/bin/self-test "$@"
    ;;
  boundary-probe)
    shift
    exec /opt/toolbox/bin/boundary-probe "$@"
    ;;
  desktop-commander-version)
    exec node -p "require('/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/package.json').version"
    ;;
  remote)
    shift
    remote_dir="$HOME/.desktop-commander-device"
    remote_config="$remote_dir/device.json"
    runtime_dir="$TOOLBOX_SESSION/runtime"
    pid_file="$runtime_dir/remote-adapter.pid"
    child_pid=""

    cleanup_remote() {
      rm -f "$remote_config" "$pid_file"
      rmdir "$remote_dir" 2>/dev/null || true
    }
    terminate_remote() {
      if [ -n "$child_pid" ]; then
        kill -TERM "$child_pid" 2>/dev/null || true
      fi
    }

    mkdir -p "$runtime_dir"
    cleanup_remote
    printf '%s\n' "$$" > "$pid_file"
    trap terminate_remote INT TERM HUP
    trap cleanup_remote EXIT

    node /opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/dist/index.js \
      remote --no-persist-session "$@" &
    child_pid="$!"
    set +e
    wait "$child_pid"
    status="$?"
    set -e
    exit "$status"
    ;;
  remote-purge-config)
    rm -f "$HOME/.desktop-commander-device/device.json"
    rmdir "$HOME/.desktop-commander-device" 2>/dev/null || true
    ;;
  remote-clean)
    runtime_dir="$TOOLBOX_SESSION/runtime"
    pid_file="$runtime_dir/remote-adapter.pid"
    if [ -f "$pid_file" ]; then
      adapter_pid="$(tr -cd '0-9' < "$pid_file")"
      if [ -n "$adapter_pid" ]; then
        kill -TERM "$adapter_pid" 2>/dev/null || true
      fi
    fi
    rm -f "$HOME/.desktop-commander-device/device.json" "$pid_file"
    rmdir "$HOME/.desktop-commander-device" 2>/dev/null || true
    ;;
  exec)
    shift
    if [ "$#" -eq 0 ]; then
      printf '%s\n' 'exec requires a command' >&2
      exit 64
    fi
    exec "$@"
    ;;
  *)
    printf 'unsupported toolbox mode: %s\n' "$1" >&2
    exit 64
    ;;
esac
