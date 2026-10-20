#!/usr/bin/env python3
"""
scripts/arm_voice.py — the Pi's microphone, streamed to the robot arm.

Runs instead of the meeting recorder when ROBOT_MODE=true (see
start-worker.sh). It only captures and streams 16 kHz PCM to /ws/arm/audio;
speech recognition (Vosk) and the Laya decision happen in the server, so the
same path also serves a browser mic or a replayed WAV.
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PA_ALSA_PLUGHW", "1")
sys.path.insert(0, str(Path(__file__).parent))

import pyaudio  # noqa: E402

from audio_stream import AudioStreamer  # noqa: E402
from record_worker import open_input_stream, resample_to_16k, resolve_input_device  # noqa: E402

SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/")
CHUNK_MS = 30


def main():
    pa = pyaudio.PyAudio()
    device = resolve_input_device(pa)
    while True:
        try:
            stream, rate, chunk = open_input_stream(pa, device, CHUNK_MS)
            streamer = AudioStreamer(SERVER_URL, source="pi-mic", path="/ws/arm/audio")
            print("[arm-voice] Listening — speak a command to the arm")
            state = None
            while True:
                raw = stream.read(chunk, exception_on_overflow=False)
                pcm, state = resample_to_16k(raw, rate, state)
                streamer.write(pcm)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[arm-voice] Mic error ({e}); reopening in 2s")
            time.sleep(2)
    pa.terminate()


if __name__ == "__main__":
    main()
