#!/bin/bash
set -euo pipefail

usage() {
  printf 'Usage: install-host.sh HOST_BUILD_CONTEXT IMAGE_TAG\n'
  printf 'Install the aggregate obs-e2e.slice limits and build obs-e2e:IMAGE_TAG.\n'
  printf 'Run on the Ubuntu Docker host as root; --help changes nothing.\n'
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac
if [ "$#" -ne 2 ]; then
  usage >&2
  exit 2
fi

context=$1
tag=$2
unit=$(mktemp)
trap 'rm -f -- "$unit"' EXIT
cat > "$unit" <<'UNIT'
[Unit]
Description=Aggregate resource limits for isolated OBS E2E harness containers

[Slice]
MemoryMax=14G
MemorySwapMax=0
CPUQuota=800%
TasksMax=8192
UNIT

# INVARIANT: the slice caps all lanes together, not just each container.
# Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Isolation
install -d /etc/systemd/system
if ! cmp -s "$unit" /etc/systemd/system/obs-e2e.slice; then
  install -m 0644 "$unit" /etc/systemd/system/obs-e2e.slice
fi
systemctl daemon-reload
systemctl start obs-e2e.slice
systemctl show obs-e2e.slice -p MemoryMax -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksMax
DOCKER_BUILDKIT=1 docker build -t "obs-e2e:$tag" "$context"
