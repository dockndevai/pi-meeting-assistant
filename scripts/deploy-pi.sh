#!/usr/bin/env bash
# scripts/deploy-pi.sh — set up and deploy to a Raspberry Pi over SSH.
#
#   scripts/deploy-pi.sh user@pi-host            # build, ship, run, verify
#   SKIP_BUILD=1 scripts/deploy-pi.sh user@host  # reuse the local image
#
# Requirements:
#   • SSH key access to the Pi (run `ssh-copy-id user@pi-host` once first)
#   • Docker on the Pi, USB mic plugged in
#   • ~/pi-meeting/.env on the Pi (created from .env.example on first run —
#     fill in the keys there; this script never copies secrets)
#   • For the robot arm: a trained model in arm/model (python -m arm.train);
#     it is copied to ~/pi-meeting/models/laya-arm on the Pi
#
# Decision: stream the image over SSH instead of scp'ing a tarball
#   `docker save | gzip | ssh docker load` needs no free space for a ~600 MB
#   file on either side and is one step instead of three.
#
# Decision: tag the running image before replacing it
#   A bad deploy is one `docker tag` away from rolled back.
set -euo pipefail

PI="${1:?Usage: scripts/deploy-pi.sh user@pi-host}"
IMAGE="pi-meeting:latest"
NAME="pi-meeting"
REMOTE_DIR="pi-meeting"   # relative to the login user's home
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI")

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Checking SSH access to $PI"
"${SSH[@]}" 'echo "connected to $(hostname) ($(uname -m)), user $(whoami)"' || {
  echo "Cannot SSH with a key. Run: ssh-copy-id $PI" >&2; exit 1; }

step "Checking Docker and the microphone on the Pi"
"${SSH[@]}" 'docker --version && docker info >/dev/null' || {
  echo "Docker missing or the user can't reach the daemon (add the user to the docker group)." >&2; exit 1; }
MIC_CARD="$("${SSH[@]}" "arecord -l 2>/dev/null | sed -n 's/^card \([0-9]*\):.*/\1/p' | head -1")"
if [ -z "$MIC_CARD" ]; then
  echo "No capture device found (arecord -l is empty). Plug in the USB mic and retry." >&2; exit 1
fi
echo "Microphone on ALSA card $MIC_CARD"

step "Preparing ~/$REMOTE_DIR on the Pi"
"${SSH[@]}" "mkdir -p ~/$REMOTE_DIR"
# asoundrc with the detected card number, so capture works without editing.
sed "s/hw:[0-9]*,0/hw:${MIC_CARD},0/; s/card [0-9]*/card ${MIC_CARD}/" "$ROOT/asoundrc" \
  | "${SSH[@]}" "cat > ~/$REMOTE_DIR/asoundrc"
scp -q "$ROOT/mcp.json" "$PI:$REMOTE_DIR/mcp.json.new"
# Don't overwrite an mcp.json already customised on the Pi.
"${SSH[@]}" "cd ~/$REMOTE_DIR && { [ -f mcp.json ] && rm mcp.json.new || mv mcp.json.new mcp.json; }"

if ! "${SSH[@]}" "test -f ~/$REMOTE_DIR/.env"; then
  scp -q "$ROOT/.env.example" "$PI:$REMOTE_DIR/.env"
  echo
  echo "Created ~/$REMOTE_DIR/.env on the Pi from .env.example."
  echo "Fill in the keys there (at least SARVAM_API_KEY and LLM_API_KEY), then re-run:"
  echo "  ssh $PI nano ~/$REMOTE_DIR/.env"
  echo "  SKIP_BUILD=1 scripts/deploy-pi.sh $PI"
  exit 2
fi
# Robot mode is fully local (Vosk + Laya), so it needs no cloud keys.
if "${SSH[@]}" "grep -qE '^ROBOT_MODE=true' ~/$REMOTE_DIR/.env"; then
  echo "ROBOT_MODE=true — no cloud keys needed"
elif "${SSH[@]}" "grep -qE '^(SARVAM_API_KEY|LLM_API_KEY)=\s*$' ~/$REMOTE_DIR/.env"; then
  echo "SARVAM_API_KEY or LLM_API_KEY is empty in ~/$REMOTE_DIR/.env on the Pi — fill them in first," >&2
  echo "or set ROBOT_MODE=true for the fully local robot arm." >&2
  exit 2
fi

if [ -f "$ROOT/arm/model/rl_agent_config.json" ]; then
  step "Copying the fine-tuned Laya model to the Pi"
  "${SSH[@]}" "mkdir -p ~/$REMOTE_DIR/models/laya-arm"
  rsync -a --delete --info=stats1 -e "ssh -o BatchMode=yes" "$ROOT/arm/model/" "$PI:$REMOTE_DIR/models/laya-arm/"
else
  echo "No arm/model here — the arm will fall back to Laya's stock model (lower accuracy)."
  echo "Train one with: python -m arm.dataset && python -m arm.train"
fi

if [ -z "${SKIP_BUILD:-}" ]; then
  step "Building arm64 image locally"
  docker build --platform linux/arm64 -t "$IMAGE" "$ROOT"
fi

step "Shipping image to the Pi (streamed, compressed)"
"${SSH[@]}" "docker image inspect $IMAGE >/dev/null 2>&1 && docker tag $IMAGE pi-meeting:rollback-\$(date +%Y%m%d-%H%M%S) || true"
docker save "$IMAGE" | gzip -1 | "${SSH[@]}" 'gunzip | docker load'

step "Starting the container"
"${SSH[@]}" bash -s <<EOF
set -euo pipefail
cd ~/$REMOTE_DIR
docker rm -f $NAME >/dev/null 2>&1 || true
# Teams MCP token files are optional — only mount them if they exist.
extra=()
for f in .msgraph-mcp-auth.json .teams-mcp-token-cache.json; do
  [ -f "\$HOME/\$f" ] && extra+=(-v "\$HOME/\$f:/root/\$f")
done
docker run -d --name $NAME --restart unless-stopped --env-file .env \\
  -p 3000:3000 -e MOCK=false \\
  --device /dev/snd --group-add audio \\
  -v pi-recordings:/recordings -v pi-data:/data -v pi-hf:/models/hf \\
  -v "\$PWD/models/laya-arm:/models/laya-arm:ro" \\
  -v "\$PWD/asoundrc:/root/.asoundrc:ro" \\
  -v "\$PWD/mcp.json:/app/mcp.json:ro" \\
  "\${extra[@]}" \\
  $IMAGE >/dev/null
EOF

step "Verifying"
# Laya takes a few seconds to load on the Pi; wait for it before checking.
for _ in $(seq 1 30); do
  "${SSH[@]}" "curl -s -m 3 http://localhost:3000/api/arm/state 2>/dev/null | grep -qE '\"brain\":\"(ready|error)\"'" && break
  sleep 3
done
"${SSH[@]}" "docker exec $NAME supervisorctl status || true"
"${SSH[@]}" "docker exec $NAME tail -5 /var/log/supervisor/worker.log || true"
STATUS="$("${SSH[@]}" 'curl -s -m 5 http://localhost:3000/api/transcript/pending' || true)"
echo "Meeting API: ${STATUS:-no response}"
ARM="$("${SSH[@]}" 'curl -s -m 5 http://localhost:3000/api/arm/state | head -c 200' || true)"
echo "Arm API:     ${ARM:-no response}"
PI_IP="$("${SSH[@]}" "hostname -I | awk '{print \$1}'")"
echo
echo "Dashboard: http://$PI_IP:3000"
echo "Robot arm: http://$PI_IP:3000/arm"
echo "Logs:      ssh $PI docker exec $NAME tail -f /var/log/supervisor/server.log /var/log/supervisor/worker.log"
echo "Rollback:  ssh $PI docker images 'pi-meeting:rollback-*'"
