#!/usr/bin/env bash
# refresh_fixture_vault.sh
# Refresh fixture_vault/ template from a real/source vault snapshot.
#
# Usage:
#   scripts/refresh_fixture_vault.sh
#   scripts/refresh_fixture_vault.sh /path/to/source/vault
#
# Env overrides:
#   OBS_REAL_VAULT_PATH        source vault path
#   OBS_FIXTURE_TEMPLATE_PATH  destination template path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DEFAULT_SOURCE="${HOME}/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
SOURCE_VAULT="${1:-${OBS_REAL_VAULT_PATH:-$DEFAULT_SOURCE}}"
TEMPLATE_VAULT="${OBS_FIXTURE_TEMPLATE_PATH:-${PROJECT_ROOT}/fixture_vault}"
META_FILE="${PROJECT_ROOT}/fixture_vault.refresh.meta"

if [ ! -d "$SOURCE_VAULT" ]; then
  echo "ERROR: Source vault not found: $SOURCE_VAULT" >&2
  exit 1
fi

TMP_DIR="${TEMPLATE_VAULT}.tmp.$$"
rm -rf "$TMP_DIR"

echo "Refreshing fixture template..." >&2
echo "  source: $SOURCE_VAULT" >&2
echo "  target: $TEMPLATE_VAULT" >&2

cp -a "$SOURCE_VAULT" "$TMP_DIR"
rm -rf "$TEMPLATE_VAULT"
mv "$TMP_DIR" "$TEMPLATE_VAULT"

SOURCE_GIT_COMMIT=""
if git -C "$SOURCE_VAULT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SOURCE_GIT_COMMIT="$(git -C "$SOURCE_VAULT" rev-parse --short HEAD 2>/dev/null || true)"
fi

STAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
{
  echo "refreshed_at_utc=$STAMP"
  echo "source_vault=$SOURCE_VAULT"
  echo "template_vault=$TEMPLATE_VAULT"
  if [ -n "$SOURCE_GIT_COMMIT" ]; then
    echo "source_git_commit=$SOURCE_GIT_COMMIT"
  fi
} > "$META_FILE"

echo "Refresh complete." >&2
echo "Metadata: $META_FILE" >&2
echo "$TEMPLATE_VAULT"
