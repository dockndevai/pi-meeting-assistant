#!/usr/bin/env bash
set -euo pipefail

SERVER_URL="${SERVER_URL:-http://localhost:3000}"
SEEN_DIR="/tmp/seen"
mkdir -p "$SEEN_DIR"

while true; do
  # Process text files
  for txt_file in /test-transcripts/*.txt; do
    [ -f "$txt_file" ] || continue
    BASENAME=$(basename "$txt_file")
    [ -f "$SEEN_DIR/$BASENAME.posted" ] && continue

    echo "[mock] Detected text file: $BASENAME"
    PAYLOAD=$(jq -n \
      --arg t "$(cat "$txt_file")" \
      --arg s "$txt_file" \
      '{transcript: $t, source_file: $s, duration_seconds: 0}')

    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "$SERVER_URL/api/transcript" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD")

    if [ "$HTTP" = "202" ]; then
      echo "[mock] ✓ Accepted — open http://localhost:3000"
      touch "$SEEN_DIR/$BASENAME.posted"
    else
      echo "[mock] ✗ Server returned HTTP $HTTP"
    fi
  done

  # Process WAV audio files (triggers real Sarvam API transcription!)
  for wav_file in /test-transcripts/*.wav; do
    [ -f "$wav_file" ] || continue
    BASENAME=$(basename "$wav_file")
    [ -f "$SEEN_DIR/$BASENAME.posted" ] && continue

    echo "[mock] Detected WAV audio: $BASENAME. Running real Sarvam transcription..."
    if bash /scripts/transcribe_and_post.sh "$wav_file"; then
      touch "$SEEN_DIR/$BASENAME.posted"
    else
      echo "[mock] ✗ Transcription failed for $BASENAME"
    fi
  done

  sleep 3
done
