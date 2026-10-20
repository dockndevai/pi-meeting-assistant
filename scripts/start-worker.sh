#!/usr/bin/env bash
set -euo pipefail

MOCK="${MOCK:-true}"
SERVER_URL="${SERVER_URL:-http://localhost:3000}"

echo "[worker] Waiting for FastAPI server..."
until curl -sf "$SERVER_URL/api/transcript/pending" >/dev/null 2>&1; do sleep 2; done
echo "[worker] Server is up."

if [ "${ROBOT_MODE:-false}" = "true" ]; then
  echo "[worker] ROBOT_MODE — streaming the mic to the robot arm (Vosk + Laya, all local)"
  exec /opt/venv/bin/python3 /scripts/arm_voice.py
elif [ "$MOCK" = "true" ]; then
  echo "[worker] MOCK mode — watching /test-transcripts for .txt files"
  echo "[worker] Or: docker exec pi-meeting bash /scripts/inject.sh 'your text'"
  exec bash /scripts/mock-watcher.sh
else
  echo "[worker] REAL mode — waiting for dashboard start/stop + Sarvam STT handoff"
  exec /opt/venv/bin/python3 /scripts/record_worker.py
fi
