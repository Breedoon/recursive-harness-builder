#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${OBS_EVAL_SOURCE_VAULT:-$ROOT/examples/recursive-workflow}"
TARGET="${OBS_EVAL_FIXTURE_VAULT:-$ROOT/fixture_vault}"

if [[ ! -d "$SOURCE" ]]; then
  echo "Source fixture vault not found: $SOURCE" >&2
  exit 1
fi

rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
cp -a "$SOURCE" "$TARGET"
