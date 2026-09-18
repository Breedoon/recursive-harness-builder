#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ $# -ne 3 ]]; then
  printf '%s\n' 'voice transcription request is invalid; retry later' >&2
  exit 75
fi

readonly AUDIO_FILE="$1"
readonly TITLE="$2"
readonly DEST_DIR="$3"
readonly API_URL="${OBS_TRANSCRIPTION_API_URL:-http://obs-transcription:8765/transcribe}"
readonly HEALTH_URL="${OBS_TRANSCRIPTION_HEALTH_URL:-http://obs-transcription:8765/ready}"
readonly REQUEST_TIMEOUT_SECONDS=5670
readonly CURL_TIMEOUT_SECONDS=5685
readonly DISABLED_SENTINEL="/workspace/runtime/transcription/run/gpu-only-disabled.json"
readonly MODEL_SNAPSHOT="/workspace/runtime/transcription/models/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
readonly SAFE_TITLE_RE='^[A-Za-z0-9._ -]+$'
readonly SAFE_CORRELATION_RE='^[a-f0-9]{32}$'

fail_retryably() {
  printf '%s\n' "$1" >&2
  exit 75
}

if [[ -e "$DISABLED_SENTINEL" || -L "$DISABLED_SENTINEL" ]]; then
  fail_retryably 'GPU voice transcription is temporarily disabled after failed lifecycle qualification; retry later'
fi

CORRELATION_ID="${OBS_TRANSCRIPTION_CORRELATION_ID:-}"
if [[ -z "$CORRELATION_ID" ]]; then
  CORRELATION_ID="$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(16))')" \
    || fail_retryably 'voice transcription correlation could not be created; retry later'
fi
readonly CORRELATION_ID
if [[ ! "$CORRELATION_ID" =~ $SAFE_CORRELATION_RE ]]; then
  fail_retryably 'voice transcription correlation is invalid; retry later'
fi

if [[ ! -f "$AUDIO_FILE" || -L "$AUDIO_FILE" ]]; then
  fail_retryably 'voice audio is invalid or unsupported; retry with a new voice note'
fi
if [[ ! -d "$DEST_DIR" || -L "$DEST_DIR" ]]; then
  fail_retryably 'voice transcript destination is invalid; retry later'
fi
if [[ -z "$TITLE" || ! "$TITLE" =~ $SAFE_TITLE_RE ]]; then
  fail_retryably 'voice transcript title is invalid; retry later'
fi
if [[ "${TITLE:0:1}" == '.' || "${TITLE:0:1}" == ' ' || "${TITLE: -1}" == '.' || "${TITLE: -1}" == ' ' ]]; then
  fail_retryably 'voice transcript title is invalid; retry later'
fi

readonly OUTPUT_PATH="$DEST_DIR/$TITLE.md"
if [[ -e "$OUTPUT_PATH" || -L "$OUTPUT_PATH" ]]; then
  fail_retryably 'voice transcript destination already exists; retry later'
fi

TMP_DIR="$(/usr/bin/mktemp -d --tmpdir="$DEST_DIR" '.obs-gpu-request.XXXXXXXX')" \
  || fail_retryably 'voice transcription workspace could not be created; retry later'
readonly TMP_DIR
readonly REQUEST_PATH="$TMP_DIR/request.json"
readonly RESPONSE_PATH="$TMP_DIR/response.json"
readonly HEALTH_PATH="$TMP_DIR/health.json"
readonly HEALTH_HTTP_CODE_PATH="$TMP_DIR/health-http-code.txt"
readonly HTTP_CODE_PATH="$TMP_DIR/http-code.txt"
PUBLISHED=0
OUTPUT_OWNED=0
OUTPUT_IDENTITY=''
CURL_PID=''

cleanup() {
  local rc=$?
  local current_identity=''
  trap - EXIT TERM INT HUP
  if (( PUBLISHED == 0 && OUTPUT_OWNED == 1 )) && [[ -e "$OUTPUT_PATH" && ! -L "$OUTPUT_PATH" ]]; then
    current_identity="$(/usr/bin/stat --format='%d:%i' -- "$OUTPUT_PATH" 2>/dev/null || true)"
    if [[ -n "$current_identity" && "$current_identity" == "$OUTPUT_IDENTITY" ]]; then
      /usr/bin/rm -f -- "$OUTPUT_PATH" >/dev/null 2>&1 || true
    fi
  fi
  /usr/bin/rm -rf -- "$TMP_DIR" >/dev/null 2>&1 || true
  exit "$rc"
}

terminate_request() {
  if [[ -n "$CURL_PID" ]] && /usr/bin/kill -0 "$CURL_PID" 2>/dev/null; then
    /usr/bin/kill -TERM "$CURL_PID" 2>/dev/null || true
    wait "$CURL_PID" 2>/dev/null || true
  fi
  exit 143
}

trap cleanup EXIT
trap terminate_request TERM INT HUP

health_curl_rc=0
/usr/bin/curl --noproxy '*' --silent --max-time 5 \
  --output "$HEALTH_PATH" --write-out '%{http_code}' "$HEALTH_URL" > "$HEALTH_HTTP_CODE_PATH" \
  || health_curl_rc=$?
if (( health_curl_rc != 0 )); then
  fail_retryably 'GPU transcription service is unavailable; retry later'
fi
health_http_code="$(<"$HEALTH_HTTP_CODE_PATH")"
health_detail=''
health_parse_rc=0
health_detail="$(/usr/bin/python3 - "$HEALTH_PATH" "$health_http_code" <<'PY'
import json
import sys
from pathlib import Path

path, http_code = sys.argv[1:]
try:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("readiness response is invalid")
    raise SystemExit(1)
contract = value.get("runtime_contract") or {}
gate = value.get("gate") or {}
if (
    http_code != "200"
    or value.get("ok") is not True
    or value.get("queue_supported") is not True
    or value.get("liveness_ok") is not True
    or gate.get("ok") is not True
):
    reason = value.get("readiness_reason")
    failure_kind = gate.get("failure_kind")
    safe_gate_errors = {
        "transport": "gate unavailable",
        "timeout": "gate control timed out",
        "rejected": "gate rejected status request",
        "invalid": "gate status invalid",
    }
    if reason == "gate_unavailable":
        detail = safe_gate_errors.get(failure_kind, "gate status unavailable")
    elif reason == "transcription_resources_unavailable":
        detail = "transcription resources unavailable"
    elif http_code != "200":
        detail = "readiness endpoint unavailable"
    else:
        detail = "readiness contract is invalid"
    print(detail)
    raise SystemExit(1)
if contract.get("device") != "cuda" or contract.get("compute_type") != "float16":
    raise SystemExit(1)
if contract.get("cpu_fallback_enabled") is not False:
    raise SystemExit(1)
if contract.get("correlation_id_required") is not True:
    raise SystemExit(1)
PY
)" || health_parse_rc=$?
if (( health_parse_rc != 0 )); then
  if [[ -n "$health_detail" ]]; then
    fail_retryably "GPU transcription unavailable: $health_detail; retry later"
  fi
  fail_retryably 'GPU transcription service contract is invalid; retry later'
fi

if ! /usr/bin/python3 - "$AUDIO_FILE" "$TITLE" "$DEST_DIR" "$CORRELATION_ID" "$REQUEST_TIMEOUT_SECONDS" "$REQUEST_PATH" <<'PY'
import json
import sys
from pathlib import Path

audio, title, dest, correlation_id, timeout, output = sys.argv[1:]
Path(output).write_text(
    json.dumps(
        {
            "audio_file": audio,
            "title": title,
            "dest_dir": dest,
            "correlation_id": correlation_id,
            "timeout_seconds": int(timeout),
        },
        separators=(",", ":"),
    )
    + "\n",
    encoding="utf-8",
)
PY
then
  fail_retryably 'voice transcription request could not be prepared; retry later'
fi

curl_rc=0
/usr/bin/curl --noproxy '*' --silent \
  --max-time "$CURL_TIMEOUT_SECONDS" \
  --output "$RESPONSE_PATH" \
  --write-out '%{http_code}' \
  --header 'content-type: application/json' \
  --data-binary "@$REQUEST_PATH" \
  "$API_URL" > "$HTTP_CODE_PATH" &
CURL_PID=$!
if wait "$CURL_PID"; then
  curl_rc=0
else
  curl_rc=$?
fi
CURL_PID=''
http_code="$(<"$HTTP_CODE_PATH")"

if (( curl_rc != 0 )) || [[ "$http_code" != '200' ]]; then
  api_detail=''
  api_detail="$(/usr/bin/python3 - "$RESPONSE_PATH" <<'PY'
import json
import sys
from pathlib import Path

ERRORS = {
    "gate_unavailable": "GPU transcription gate unavailable; retry later",
    "admission_unavailable": "GPU transcription admission unavailable; retry later",
    "runtime_unavailable": "GPU transcription runtime unavailable; retry later",
    "worker_timeout": "GPU transcription exceeded its finite worker bound; retry later",
    "completion_invalid": "GPU transcription returned no complete transcript; retry later",
    "worker_failure": "GPU transcription worker failed; retry later",
    "invalid_request": "GPU transcription request was invalid; retry later",
}
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {}
error_class = value.get("error_class")
print(ERRORS.get(error_class, "GPU transcription did not complete; retry later"))
PY
)" || true
  fail_retryably "${api_detail:-GPU transcription did not complete; retry later}"
fi

if ! OUTPUT_IDENTITY="$(/usr/bin/python3 - "$RESPONSE_PATH" "$OUTPUT_PATH" "$CORRELATION_ID" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

response_path, expected_output, correlation_id = sys.argv[1:]
try:
    value = json.loads(Path(response_path).read_text(encoding="utf-8"))
    output_stat = os.lstat(expected_output)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if value.get("ok") is not True or value.get("returncode") != 0:
    raise SystemExit(1)
if value.get("correlation_id") != correlation_id:
    raise SystemExit(1)
if value.get("output_path") != expected_output or value.get("output_exists") is not True:
    raise SystemExit(1)
if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size <= 0:
    raise SystemExit(1)
if value.get("output_dev") != output_stat.st_dev or value.get("output_ino") != output_stat.st_ino:
    raise SystemExit(1)
print(f"{output_stat.st_dev}:{output_stat.st_ino}")
PY
)"; then
  fail_retryably 'GPU transcription returned an invalid completion receipt; retry later'
fi
OUTPUT_OWNED=1

if [[ ! -f "$OUTPUT_PATH" || -L "$OUTPUT_PATH" || ! -s "$OUTPUT_PATH" ]]; then
  fail_retryably 'GPU transcription returned no complete transcript; retry later'
fi

status_ok=0
correlation_ok=0
model_ok=0
device_ok=0
compute_ok=0
heading_ok=0
while IFS= read -r line; do
  case "$line" in
    'transcription_status: "ok"') status_ok=1 ;;
    "correlation_id: \"$CORRELATION_ID\"") correlation_ok=1 ;;
    "model: \"$MODEL_SNAPSHOT\"") model_ok=1 ;;
    'device: "cuda"') device_ok=1 ;;
    'compute_type: "float16"') compute_ok=1 ;;
    '# Transcript') heading_ok=1 ;;
  esac
done < "$OUTPUT_PATH"
if (( ! status_ok || ! correlation_ok || ! model_ok || ! device_ok || ! compute_ok || ! heading_ok )); then
  fail_retryably 'GPU transcription returned an incomplete transcript; retry later'
fi

chmod 0600 "$OUTPUT_PATH"
PUBLISHED=1
exit 0
