# syntax=docker/dockerfile:1
#
# Base: debian:bookworm-slim arm64
# ─────────────────────────────────────────────────────────────────────────────
# Decision: debian:bookworm-slim over raspios/official Pi image
#   Raspberry Pi OS 64-bit IS Debian bookworm arm64 with Pi firmware on top.
#   The firmware layer is irrelevant inside Docker — no GPIO, no VideoCore.
#   bookworm-slim gives us the identical userspace (same apt repos, same
#   package versions) at a much smaller image size (~30 MB vs ~400 MB).
#   Smaller image = faster export to Pi, faster docker load on Pi.
#
# Decision: Python 3.11 (Debian bookworm default, no PPA needed)
#   Python 3.12 is faster (~10%) but requires deadsnakes PPA or compiling
#   from source — both add build time. 3.11 ships with bookworm, zero extra
#   steps. The performance delta doesn't matter here; network latency to
#   the Anthropic API (1–5s) dwarfs any Python interpreter difference.
#
# Decision: ONE venv for everything (server + recording scripts)
#   Previous Node design needed npm for the server AND pip for Python scripts.
#   Single venv means one install step, one PATH, no context switching.
#   /opt/venv is activated globally in the image via ENV PATH.

FROM --platform=linux/arm64 debian:bookworm-slim

# ── System packages ───────────────────────────────────────────────────────────
# Decision: install build-essential + cmake even in the base image
#   Some pip packages (pyaudio, webrtcvad) compile C extensions at install time.
#   Having the compiler in the image avoids a separate build stage, keeping
#   the Dockerfile simple. Image size cost is ~50 MB — acceptable for a Pi
#   that will have a 32 GB SD card.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # runtime
    curl ca-certificates jq ffmpeg \
    # python
    python3 python3-dev python3-pip python3-venv \
    # audio (pyaudio needs portaudio headers at pip install time)
    portaudio19-dev libportaudio2 \
    # audio routing (for PulseAudio network bridge on macOS)
    pulseaudio pulseaudio-utils alsa-utils libasound2-plugins \
    # process manager — manages uvicorn + worker as separate processes in one container
    supervisor \
    # pyaudio/webrtcvad compile C extensions at pip install time
    build-essential \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# ── Single Python venv ────────────────────────────────────────────────────────
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Python dependencies ───────────────────────────────────────────────────────
# Decision: copy requirements.txt first, then install
#   Docker layer caching: if requirements.txt hasn't changed, this layer is
#   cached. Rebuilds only re-run pip install when deps actually change.
#   On Pi via QEMU this install takes ~3 min — caching saves it on every
#   code change rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Decision: bake wake-word models into the image at build time
#   openWakeWord's pretrained ONNX models aren't bundled in the pip package.
#   Downloading them at build time (not container startup) keeps boot fast
#   and avoids a network dependency every time the container starts.
RUN python3 -c "import openwakeword.utils; openwakeword.utils.download_models()"

# Decision: bake the Vosk speech models into the image, mount the Laya one
#   Vosk models are small (~130 MB) and fixed, so the image carries them and
#   voice works offline from first boot. The fine-tuned Laya checkpoint is
#   ~0.65 GB and changes whenever it's retrained, so it's mounted from the
#   host at /models/laya-arm instead (scripts/deploy-pi.sh copies it over).
COPY scripts/fetch_models.sh /tmp/fetch_models.sh
RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/* \
  && bash /tmp/fetch_models.sh /models/vosk
ENV VOSK_MODELS_DIR=/models/vosk ARM_MODEL_DIR=/models/laya-arm \
    HF_HOME=/models/hf TOKENIZERS_PARALLELISM=false

# ── Application code ──────────────────────────────────────────────────────────
WORKDIR /app
COPY main.py .
COPY mcp.json .
COPY routes/   routes/
COPY services/ services/
COPY config/   config/
COPY public/   public/
COPY arm/      arm/

# ── Scripts ───────────────────────────────────────────────────────────────────
COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh

RUN mkdir -p /recordings /test-transcripts /data

# ── Supervisor ────────────────────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/meeting.conf

EXPOSE 3000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
