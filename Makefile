.PHONY: up down build logs shell inject inject-file replay live-say deploy-pi export-pi status pulse-mac

pulse-mac:
	@echo "Stopping existing pulseaudio services..."
	@brew services stop pulseaudio 2>/dev/null || true
	@pulseaudio -k 2>/dev/null || true
	@echo "Starting pulseaudio with TCP protocol enabled..."
	pulseaudio --load="module-native-protocol-tcp auth-anonymous=1" --exit-idle-time=-1 --daemon
	@echo "PulseAudio is running on host."

up:
	docker compose up --build -d
	@echo ""
	@echo "  Pi OS image running (arm64 Debian bookworm via QEMU)"
	@echo "  Dashboard  ->  http://localhost:3000"
	@echo "  Logs       ->  make logs"
	@echo "  Test       ->  make inject TEXT='Asha to fix login bug by Friday'"

down:
	docker compose down

build:
	docker compose build

logs:
	docker exec pi-meeting tail -f \
	  /var/log/supervisor/server.log \
	  /var/log/supervisor/worker.log

shell:
	docker exec -it pi-meeting bash

inject:
	@[ -n "$(TEXT)" ] || (echo "Usage: make inject TEXT='...'"; exit 1)
	docker exec pi-meeting bash /scripts/inject.sh "$(TEXT)"

inject-file:
	@[ -n "$(FILE)" ] || (echo "Usage: make inject-file FILE=./path.txt"; exit 1)
	cp "$(FILE)" ./test-transcripts/

# Stream a WAV through the live pipeline as if it were the mic (needs SARVAM_API_KEY)
replay:
	@[ -n "$(FILE)" ] || (echo "Usage: make replay FILE=speech_upload.wav [SPEED=2]  (file in ./test-transcripts)"; exit 1)
	docker exec pi-meeting python3 /scripts/replay_audio.py /test-transcripts/$(FILE) --speed $(or $(SPEED),1)

# Add one line to the live transcript without audio (MOCK=true or LIVE_DEV=true)
live-say:
	@[ -n "$(TEXT)" ] || (echo "Usage: make live-say TEXT='...'"; exit 1)
	curl -s -X POST http://localhost:3000/api/live/inject -H 'Content-Type: application/json' \
	  -d "$$(jq -n --arg t "$(TEXT)" '{text: $$t}')"; echo

# Build, ship and start on a Pi over SSH (key auth), e.g. make deploy-pi PI=pi@192.168.1.50
deploy-pi:
	@[ -n "$(PI)" ] || (echo "Usage: make deploy-pi PI=user@pi-host"; exit 1)
	bash scripts/deploy-pi.sh $(PI)

export-pi:
	@echo "Saving image..."
	docker save pi-meeting:latest | gzip > pi-meeting.tar.gz
	@echo "Done -> pi-meeting.tar.gz ($(du -sh pi-meeting.tar.gz | cut -f1))"
	@echo ""
	@echo "On the Pi:"
	@echo "  docker load < pi-meeting.tar.gz"
	@echo "  docker run -d --name pi-meeting --restart unless-stopped --env-file .env \\"
	@echo "    -p 3000:3000 -e MOCK=false -e RECORD_DEVICE_INDEX=0 \\"
	@echo "    --device /dev/snd --group-add audio -v /dev/snd:/dev/snd \\"
	@echo "    -v pi-recordings:/recordings -v pi-data:/data -v \$$PWD/asoundrc:/root/.asoundrc:ro \\"
	@echo "    -v \$$PWD/mcp.json:/app/mcp.json:ro \\"
	@echo "    -v \$$HOME/.msgraph-mcp-auth.json:/root/.msgraph-mcp-auth.json \\"
	@echo "    -v \$$HOME/.teams-mcp-token-cache.json:/root/.teams-mcp-token-cache.json \\"
	@echo "    pi-meeting:latest"

status:
	docker compose ps
	@echo ""
	@docker exec pi-meeting supervisorctl status 2>/dev/null || true
