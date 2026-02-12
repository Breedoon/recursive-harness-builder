#!/usr/bin/env bash
# setup_fixture_vault.sh
# Creates a fixture vault for testing by copying relevant files from the real vault.
# Outputs the path to the fixture vault on stdout.

set -euo pipefail

REAL_VAULT="${HOME}/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
FIXTURE_DIR=$(mktemp -d -t obs-fixture-vault)

if [ ! -d "$REAL_VAULT" ]; then
    echo "ERROR: Real vault not found at: $REAL_VAULT" >&2
    exit 1
fi

echo "Creating fixture vault at: $FIXTURE_DIR" >&2

# Copy Agent directory (context, skills, system)
mkdir -p "$FIXTURE_DIR/Agent"
cp -R "$REAL_VAULT/Agent/context.md" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/skills.md" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/system.md" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/skills" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/system" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/memory.md" "$FIXTURE_DIR/Agent/" 2>/dev/null || true
cp -R "$REAL_VAULT/Agent/memory" "$FIXTURE_DIR/Agent/" 2>/dev/null || true

# Copy a few Misc/Meeting Notes files (immutable test data)
if [ -d "$REAL_VAULT/Misc/Meeting Notes" ]; then
    mkdir -p "$FIXTURE_DIR/Misc/Meeting Notes"
    # Copy up to 3 meeting note files
    find "$REAL_VAULT/Misc/Meeting Notes" -name "*.md" -maxdepth 1 | head -3 | while read f; do
        cp "$f" "$FIXTURE_DIR/Misc/Meeting Notes/"
    done
fi

# Copy a few journal entries if they exist
if [ -d "$REAL_VAULT/Journal" ]; then
    mkdir -p "$FIXTURE_DIR/Journal"
    find "$REAL_VAULT/Journal" -name "*.md" -maxdepth 1 | head -3 | while read f; do
        cp "$f" "$FIXTURE_DIR/Journal/"
    done
fi

# Initialize fresh git repo
cd "$FIXTURE_DIR"
git init -q
git add -A
git commit -q -m "Initial fixture vault"

# Output the path (stdout only)
echo "$FIXTURE_DIR"
