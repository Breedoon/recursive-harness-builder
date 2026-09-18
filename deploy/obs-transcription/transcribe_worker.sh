#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  printf 'usage: %s AUDIO_FILE TITLE DEST_DIR\n' "$0" >&2
  exit 64
fi

BASE="${OBS_TRANSCRIPTION_BASE:-/workspace/runtime/transcription}"
PYTHON_BIN="${OBS_TRANSCRIPTION_PYTHON:-$BASE/venv/bin/python}"
HANDOFF="${OBS_TRANSCRIPTION_HANDOFF:-$BASE/transcribe_handoff.py}"

if [[ ! -x "$PYTHON_BIN" || ! -f "$HANDOFF" ]]; then
  printf 'transcription handoff runtime is incomplete\n' >&2
  exit 78
fi

exec "$PYTHON_BIN" "$HANDOFF" "$1" "$2" "$3"
