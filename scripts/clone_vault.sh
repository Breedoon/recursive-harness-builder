#!/usr/bin/env bash
# clone_vault.sh
# Creates/bootstraps the fixture_vault/ template in the project root.
# Skips if fixture_vault/ already exists.
# Eval runs should use an ephemeral copy of this template (see tests/conftest.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REAL_VAULT="${HOME}/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
FIXTURE_DIR="${PROJECT_ROOT}/fixture_vault"

if [ -d "$FIXTURE_DIR" ]; then
    echo "fixture_vault/ already exists, skipping clone." >&2
    echo "$FIXTURE_DIR"
    exit 0
fi

if [ ! -d "$REAL_VAULT" ]; then
    echo "ERROR: Real vault not found at: $REAL_VAULT" >&2
    exit 1
fi

echo "Cloning vault to ${FIXTURE_DIR}..." >&2
cp -a "$REAL_VAULT" "$FIXTURE_DIR"

echo "Done. fixture_vault/ ready." >&2
echo "$FIXTURE_DIR"
