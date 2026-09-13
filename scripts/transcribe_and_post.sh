#!/usr/bin/env bash
# scripts/transcribe_and_post.sh
#
# Transcribes a WAV file using the Sarvam STT API (via transcribe_indic.py)
# and POSTs the transcript to the FastAPI server.
#
set -euo pipefail

SERVER_URL="${SERVER_URL:-http://localhost:3000}"
TRANSCRIBE_PY="/scripts/transcribe_indic.py"
WAV_FILE="${1:?Usage: transcribe_and_post.sh <path_to_wav>}"

reset_session() {
  echo "[transcribe] Resetting pending server session..." >&2
  curl -s -X DELETE "$SERVER_URL/api/transcript/pending" >/dev/null || true
}

if [ ! -f "$WAV_FILE" ]; then
  echo "[transcribe] Error: WAV file not found at $WAV_FILE" >&2
  reset_session
  exit 1
fi

if [ ! -f "$TRANSCRIBE_PY" ]; then
  # Fallback to local script directory if not running in Docker
  TRANSCRIBE_PY="$(dirname "$0")/transcribe_indic.py"
fi

if [ ! -f "$TRANSCRIBE_PY" ]; then
  echo "[transcribe] Error: transcribe_indic.py not found" >&2
  reset_session
  exit 1
fi

echo "[transcribe] Measuring audio duration..."
# Get duration of wav file using ffprobe
DURATION=$(ffprobe -i "$WAV_FILE" -show_entries format=duration -v quiet -of csv="p=0")
if [ -z "$DURATION" ]; then
  DURATION=0
fi

echo "[transcribe] Transcribing $WAV_FILE with Sarvam batch STT (duration: ${DURATION}s)..."
if ! /opt/venv/bin/python3 "$TRANSCRIBE_PY" "$WAV_FILE"; then
  echo "[transcribe] Error: Sarvam STT failed." >&2
  reset_session
  exit 1
fi

TXT_FILE="${WAV_FILE}.txt"
if [ ! -f "$TXT_FILE" ]; then
  echo "[transcribe] Error: Transcription output file not created." >&2
  reset_session
  exit 1
fi

TRANSCRIPT=$(cat "$TXT_FILE")

# Check if transcript is empty
if [ -z "$(echo "$TRANSCRIPT" | tr -d '[:space:]')" ]; then
  echo "[transcribe] Warning: Transcript is empty. Skipping post."
  rm -f "$TXT_FILE"
  reset_session
  exit 0
fi

echo "[transcribe] Sending transcript to server..."
PAYLOAD=$(jq -n \
  --arg t "$TRANSCRIPT" \
  --arg s "$(basename "$WAV_FILE")" \
  --arg d "$DURATION" \
  '{transcript: $t, source_file: $s, duration_seconds: ($d | tonumber | round)}')

HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$SERVER_URL/api/transcript" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

if [ "$HTTP" = "202" ]; then
  echo "[transcribe] ✓ Accepted. Review at http://localhost:3000"
else
  echo "[transcribe] ✗ Server returned HTTP $HTTP"
  reset_session
fi

# Clean up transcription text file
rm -f "$TXT_FILE"
