#!/usr/bin/env python3
"""
record_worker.py

Listens continuously for either the "hey jarvis" wake word or a "start"
command from the dashboard (written to /tmp/record_cmd), records audio
until silence or a dashboard "stop" command.

While recording, audio is streamed live to the server (/ws/audio) for the
real-time transcript, and also saved to a WAV. If the live transcript
captured the meeting, the server summarises it directly; otherwise the WAV
goes through transcribe_and_post.sh (batch STT) as before.

Requirements:
    pip install pyaudio webrtcvad openwakeword onnxruntime
"""

import audioop
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

# PortAudio enumerates raw hw: devices by default, which refuse 16 kHz on the
# USB mic. plughw lets ALSA resample so openWakeWord gets clean 16 kHz audio.
os.environ.setdefault("PA_ALSA_PLUGHW", "1")

import numpy as np
import pyaudio
import webrtcvad
from dotenv import load_dotenv
from openwakeword.model import Model as OWWModel

from audio_stream import AudioStreamer

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────
# RECORD_DEVICE_INDEX env: unset/empty = auto-pick first input (or system default)
# Explicit int forces that PortAudio device index.
SILENCE_DURATION_S = 60.0         # stop recording after N seconds of silence
MAX_RECORD_S       = 3600         # hard cap: 1 hour
RECORDINGS_DIR     = Path("/recordings")
SCRIPT_DIR         = Path(__file__).parent
CMD_FILE           = Path("/tmp/record_cmd")
PAUSE_FLAG         = Path("/tmp/record_paused")   # set by the server while a meeting is paused
WAKEWORD_MODEL     = os.environ.get("WAKEWORD_MODEL", "hey_jarvis")
WAKEWORD_THRESHOLD = float(os.environ.get("WAKEWORD_THRESHOLD", "0.5"))
SERVER_URL         = os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/")
# ───────────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000               # target rate for VAD / wake word / WAV output
CHANNELS = 1
FRAME_MS = 30                     # webrtcvad supports 10, 20, 30 ms
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
OWW_CHUNK_MS = 80
OWW_CHUNK_SAMPLES = 1280          # openWakeWord expects 80ms chunks @ 16kHz


def list_devices(pa=None):
    """Helper: print all audio input devices so you can find your mic index."""
    owns = pa is None
    if owns:
        pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(f"  [{i}] {info['name']}")
    finally:
        if owns:
            pa.terminate()


def resolve_input_device(pa):
    """Pick mic: RECORD_DEVICE_INDEX env, else default input, else first capture device."""
    raw = os.environ.get("RECORD_DEVICE_INDEX", "").strip()
    if raw != "":
        idx = int(raw)
        info = pa.get_device_info_by_index(idx)
        print(f"[worker] Using RECORD_DEVICE_INDEX={idx} ({info['name']})")
        return idx

    try:
        info = pa.get_default_input_device_info()
        idx = int(info["index"])
        print(f"[worker] Using default input device {idx} ({info['name']})")
        return idx
    except Exception as e:
        print(f"[worker] No default input device ({e}); scanning...")

    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"[worker] Using first input device {i} ({info['name']})")
            return i

    print("[worker] Available devices:")
    list_devices(pa)
    raise RuntimeError("No audio input device found. Check USB mic and /dev/snd.")


def open_input_stream(pa, device_index, chunk_ms):
    """
    Open a mono int16 input stream.
    Prefer ALSA default at 16 kHz (uses /root/.asoundrc plug → hw:2,0 resampled
    cleanly). Forced RECORD_DEVICE_INDEX often maps to raw hw @ 44.1 kHz, which
    breaks openWakeWord after audioop.ratecv.
    Returns (stream, capture_rate, frames_per_buffer).
    """
    attempts = []
    # 1) System default / asoundrc plug at native 16 kHz (best for wake word)
    attempts.append((None, SAMPLE_RATE))
    # 2) Explicit index at 16 kHz (works if device is already a plug)
    if device_index is not None:
        attempts.append((device_index, SAMPLE_RATE))
    # 3) Fallbacks at hardware rates + software resample
    for rate in (44100, 48000, 32000, 22050):
        attempts.append((device_index, rate))
        if device_index is not None:
            attempts.append((None, rate))

    last_err = None
    seen = set()
    for idx, rate in attempts:
        key = (idx, rate)
        if key in seen:
            continue
        seen.add(key)
        frames_per_buffer = max(1, int(rate * chunk_ms / 1000))
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=rate,
                input=True,
                input_device_index=idx,
                frames_per_buffer=frames_per_buffer,
            )
            label = "default/asoundrc" if idx is None else f"index {idx}"
            if rate != SAMPLE_RATE:
                print(f"[worker] Mic opened via {label} at {rate} Hz (will resample to {SAMPLE_RATE} Hz)")
            else:
                print(f"[worker] Mic opened via {label} at {rate} Hz")
            return stream, rate, frames_per_buffer
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Could not open input device {device_index}: {last_err}")


def resample_to_16k(pcm: bytes, from_rate: int, state=None):
    """Resample mono int16 PCM to 16 kHz. Returns (pcm16k, ratecv_state)."""
    if from_rate == SAMPLE_RATE:
        return pcm, state
    converted, state = audioop.ratecv(pcm, 2, CHANNELS, from_rate, SAMPLE_RATE, state)
    return converted, state


def notify_dashboard_recording():
    """Tell FastAPI so the UI shows Stop after a wake-word start."""
    url = f"{SERVER_URL}/api/transcript/record/active"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=3) as res:
            print(f"[worker] Dashboard notified: recording active (HTTP {res.status})")
    except Exception as e:
        print(f"[worker] Could not notify dashboard of recording start: {e}")


def notify_dashboard_processing() -> bool:
    """
    Tell FastAPI recording ended. Returns True if the server already has a
    usable live transcript (so batch STT can be skipped).
    The server waits for Sarvam's last finals before answering, hence the
    longer timeout.
    """
    url = f"{SERVER_URL}/api/transcript/record/processing"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=30) as res:
            body = json.loads(res.read() or b"{}")
            print(f"[worker] Dashboard notified: processing (HTTP {res.status}, "
                  f"live_transcript={body.get('live_transcript')})")
            return bool(body.get("live_transcript"))
    except Exception as e:
        print(f"[worker] Could not notify dashboard of processing: {e}")
        return False


def wait_for_trigger(pa, oww, device_index):
    """
    Idles until either the wake word ("hey jarvis") is detected or a "start"
    command arrives from the dashboard. Returns once triggered.
    """
    stream, capture_rate, chunk = open_input_stream(pa, device_index, OWW_CHUNK_MS)
    ratecv_state = None
    leftover = b""
    last_score_log = 0.0
    try:
        while True:
            # Check commands before blocking on mic I/O — USB mics can stall reads.
            if CMD_FILE.exists():
                try:
                    cmd = CMD_FILE.read_text().strip()
                    if cmd == "start":
                        CMD_FILE.unlink(missing_ok=True)
                        print("\n[worker] Start command received from dashboard!")
                        return
                    if cmd == "stop":
                        # Stop while idle: clear and keep waiting.
                        CMD_FILE.unlink(missing_ok=True)
                except Exception as e:
                    print(f"Error checking start command: {e}")

            # IMPORTANT: do not skip reads when get_read_available() reports 0 —
            # on ALSA that often stays 0 and starves the wake-word loop.
            try:
                available = stream.get_read_available()
            except Exception:
                available = chunk
            if 0 < available < chunk:
                time.sleep(0.01)
                continue

            raw = stream.read(chunk, exception_on_overflow=False)
            pcm16k, ratecv_state = resample_to_16k(raw, capture_rate, ratecv_state)
            leftover += pcm16k
            while len(leftover) >= OWW_CHUNK_SAMPLES * 2:
                block = leftover[: OWW_CHUNK_SAMPLES * 2]
                leftover = leftover[OWW_CHUNK_SAMPLES * 2 :]
                audio = np.frombuffer(block, dtype=np.int16)
                scores = oww.predict(audio)
                score = float(scores.get(WAKEWORD_MODEL, 0.0))
                now = time.time()
                if score >= 0.2 and now - last_score_log > 0.5:
                    print(f"[worker] wake score={score:.2f} (need > {WAKEWORD_THRESHOLD})")
                    last_score_log = now
                if score > WAKEWORD_THRESHOLD:
                    print(f"\n[worker] Wake word '{WAKEWORD_MODEL}' detected! (score={score:.2f})")
                    return
    finally:
        stream.stop_stream()
        stream.close()


def record_until_silence(pa, vad, device_index):
    """
    Record PCM frames from the mic (stored as 16 kHz for STT).
    Stops on: dashboard Stop, silence timeout, or max duration.
    """
    stream, capture_rate, chunk = open_input_stream(pa, device_index, FRAME_MS)
    ratecv_state = None
    leftover = b""
    streamer = AudioStreamer(SERVER_URL, source="mic")
    was_paused = False

    print("[record] Recording... (use dashboard Stop to end, or wait for silence)")
    frames = []
    silent_frames = 0
    max_frames = int(MAX_RECORD_S * 1000 / FRAME_MS)
    silence_limit = int(SILENCE_DURATION_S * 1000 / FRAME_MS)

    try:
        while len(frames) < max_frames:
            if CMD_FILE.exists():
                try:
                    cmd = CMD_FILE.read_text().strip()
                    if cmd == "stop":
                        CMD_FILE.unlink(missing_ok=True)
                        print("[record] Stop command received from dashboard!")
                        break
                except Exception as e:
                    print(f"Error checking stop command: {e}")

            try:
                available = stream.get_read_available()
            except Exception:
                available = chunk
            if 0 < available < chunk:
                time.sleep(0.01)
                continue

            raw = stream.read(chunk, exception_on_overflow=False)

            # Paused: keep draining the mic so the buffer doesn't overflow,
            # but record nothing, stream nothing, and don't count silence.
            paused = PAUSE_FLAG.exists()
            if paused != was_paused:
                print(f"[record] {'Paused — not capturing' if paused else 'Resumed capturing'}")
                was_paused = paused
                silent_frames = 0
                leftover = b""
            if paused:
                continue

            pcm16k, ratecv_state = resample_to_16k(raw, capture_rate, ratecv_state)
            leftover += pcm16k

            while len(leftover) >= FRAME_SAMPLES * 2 and len(frames) < max_frames:
                block = leftover[: FRAME_SAMPLES * 2]
                leftover = leftover[FRAME_SAMPLES * 2 :]
                frames.append(block)
                streamer.write(block)
                is_speech = vad.is_speech(block, SAMPLE_RATE)
                silent_frames = 0 if is_speech else silent_frames + 1

                if len(frames) % 33 == 0:
                    duration = len(frames) * FRAME_MS / 1000
                    silence_sec = silent_frames * FRAME_MS / 1000
                    if silent_frames > 0:
                        print(
                            f"  [record] {duration:.1f}s recorded... "
                            f"(VAD: silence detected for {silence_sec:.1f}s / {SILENCE_DURATION_S}s)"
                        )
                    else:
                        print(f"  [record] {duration:.1f}s recorded... (VAD: capturing active speech)")

                if silent_frames >= silence_limit and len(frames) > silence_limit * 2:
                    print(f"  [record] Silence limit of {SILENCE_DURATION_S}s reached.")
                    return frames
    finally:
        stream.stop_stream()
        stream.close()
        streamer.close()

    print(f"[record] Stopped — {len(frames) * FRAME_MS / 1000:.1f}s recorded")
    return frames


def save_wav(frames, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return path


def main():
    vad = webrtcvad.Vad(2)   # aggressiveness 0-3; 2 works well in a room
    pa = pyaudio.PyAudio()
    oww = OWWModel(wakeword_models=[WAKEWORD_MODEL], inference_framework="onnx")
    device_index = resolve_input_device(pa)

    print(
        f"[worker] Waiting for wake word ('{WAKEWORD_MODEL.replace('_', ' ')}') "
        f"or start command from dashboard... "
        f"(while recording, use dashboard Stop)"
    )

    try:
        while True:
            wait_for_trigger(pa, oww, device_index)
            # Wake-word starts bypass UI /record/start — sync session so Stop appears.
            notify_dashboard_recording()

            frames = record_until_silence(pa, vad, device_index)
            oww.reset()  # clear detector state so the recording's own audio isn't replayed as a trigger

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            wav_path = save_wav(frames, RECORDINGS_DIR / f"meeting_{ts}.wav")
            print(f"[record] Saved to {wav_path}")

            # Ends the live meeting on the server. If its live transcript is
            # usable the server summarises it; otherwise fall back to batch STT.
            if notify_dashboard_processing():
                print("[record] Live transcript captured — skipping batch STT")
            else:
                print("[record] No live transcript — running batch STT on the WAV")
                script = SCRIPT_DIR / "transcribe_and_post.sh"
                subprocess.Popen(["bash", str(script), str(wav_path)])

            print(
                f"\n[worker] Waiting for wake word ('{WAKEWORD_MODEL.replace('_', ' ')}') "
                f"or start command from dashboard... "
                f"(while recording, use dashboard Stop)"
            )
    except KeyboardInterrupt:
        print("\n[worker] Stopped.")
    finally:
        pa.terminate()


if __name__ == "__main__":
    main()
