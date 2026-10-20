#!/usr/bin/env bash
# scripts/fetch_models.sh — download the small Vosk speech models (~130 MB).
#
#   scripts/fetch_models.sh [dest]      default dest: /models/vosk
#
# Indian English + Hindi. Both run on every utterance; Laya picks the
# transcript it is more confident about (arm/brain.py).
set -euo pipefail
DEST="${1:-/models/vosk}"
mkdir -p "$DEST"
cd "$DEST"
for m in vosk-model-small-en-in-0.4 vosk-model-small-hi-0.22; do
  if [ -d "$m" ]; then echo "have $m"; continue; fi
  echo "fetching $m"
  curl -fsSL -o "$m.zip" "https://alphacephei.com/vosk/models/$m.zip"
  unzip -q "$m.zip" && rm "$m.zip"
done
du -sh "$DEST"/*
