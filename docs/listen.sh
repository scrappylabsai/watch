#!/usr/bin/env bash
# listen — universal ASR wrapper. Local-first, fleet-fallback.
#
# Usage:
#   listen path/to/audio.wav                     # transcribe a file → stdout
#   listen --model scrappy-asr-1 audio.mp3
#   listen --json audio.wav                      # full JSON, not just text
#   listen --segments audio.mp3                  # request verbose_json (segments+timestamps); implies --json
#   cat audio.wav | listen                       # stdin → stdout
#
# Path resolution (in order):
#   1. http://127.0.0.1:9877/v1/audio/transcriptions   (Sparks have local Qwen3-ASR)
#   2. https://api.scrappylabs.ai/v1/audio/transcriptions
#
# Loads SL_ADMIN_API_KEY from ~/fleet-config/secrets.env.
set -euo pipefail

MODEL="scrappy-asr-1"
JSON_OUT=0
SEGMENTS_OUT=0
FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)    MODEL="$2"; shift 2 ;;
    --json)     JSON_OUT=1; shift ;;
    --segments) SEGMENTS_OUT=1; JSON_OUT=1; shift ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) FILE="$1"; shift ;;
  esac
done

# stdin fallback
TMP=""
if [[ -z "$FILE" ]] && ! [[ -t 0 ]]; then
  TMP="$(mktemp -t listen-XXXXXX.audio)"
  cat > "$TMP"
  FILE="$TMP"
fi

if [[ -z "$FILE" || ! -f "$FILE" ]]; then
  echo "listen: no audio file provided. Use 'listen --help'." >&2
  [[ -n "$TMP" ]] && rm -f "$TMP"
  exit 2
fi

LOCAL_URL="http://127.0.0.1:9877"
API_URL="https://api.scrappylabs.ai"

# Load API key now (may need for fallback)
if [[ -z "${SL_ADMIN_API_KEY:-}" ]]; then
  # shellcheck disable=SC1091
  [[ -f "$HOME/fleet-config/secrets.env" ]] && source "$HOME/fleet-config/secrets.env"
fi

try_endpoint() {
  local endpoint="$1"; shift
  local extra_form=()
  if [[ "$SEGMENTS_OUT" == "1" ]]; then
    extra_form=(-F "response_format=verbose_json")
  fi
  curl -sS -X POST "$endpoint" \
    "$@" \
    -F "file=@${FILE}" \
    -F "model=${MODEL}" \
    "${extra_form[@]}" \
    -w "\n__HTTP__:%{http_code}"
}

ROUTE=""
RESPONSE=""
HTTP=""

# Try local first if its /health is up
if curl -sS -o /dev/null -m 1 "${LOCAL_URL}/health" 2>/dev/null; then
  ROUTE="local"
  RESPONSE="$(try_endpoint "${LOCAL_URL}/v1/audio/transcriptions")" || RESPONSE=""
  HTTP="${RESPONSE##*__HTTP__:}"
  if [[ "$HTTP" != "200" ]]; then
    # Local exists but rejected (likely model-name mismatch). Fall through to API.
    ROUTE="api (local-${HTTP}-fallback)"
    RESPONSE=""
  fi
fi

# Fall back to API
if [[ -z "$RESPONSE" || "$HTTP" != "200" ]]; then
  [[ -z "$ROUTE" ]] && ROUTE="api"
  RESPONSE="$(try_endpoint "${API_URL}/v1/audio/transcriptions" -H "Authorization: Bearer ${SL_ADMIN_API_KEY:-}")" || {
    echo "listen: curl failed (route=$ROUTE)" >&2
    [[ -n "$TMP" ]] && rm -f "$TMP"
    exit 1
  }
  HTTP="${RESPONSE##*__HTTP__:}"
fi

BODY="${RESPONSE%$'\n'__HTTP__:*}"

if [[ "$HTTP" != "200" ]]; then
  echo "listen: returned HTTP $HTTP (route=$ROUTE)" >&2
  echo "$BODY" >&2
  [[ -n "$TMP" ]] && rm -f "$TMP"
  exit 1
fi

if [[ "$JSON_OUT" == "1" ]]; then
  echo "$BODY"
else
  echo "$BODY" | jq -r '.text // .results[0].alternatives[0].transcript // .' 2>/dev/null || echo "$BODY"
fi

[[ -n "$TMP" ]] && rm -f "$TMP"
exit 0
