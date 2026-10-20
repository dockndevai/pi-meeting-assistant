#!/usr/bin/env python3
"""
scripts/replay_audio.py

Replays a WAV file into the live pipeline as if it were the Pi mic —
test the real-time transcript on a laptop without audio hardware.

    python3 scripts/replay_audio.py test-transcripts/speech_upload.wav
    python3 scripts/replay_audio.py meeting.wav --speed 2 --no-end
    python3 scripts/replay_audio.py commands.wav --arm   # into the robot arm instead

Streams at real-time pace (--speed to go faster), then ends the meeting via
/record/processing so the summary/review flow runs, unless --no-end.
Any WAV works: it is converted to 16 kHz mono int16 first.
"""

import argparse
import audioop
import json
import os
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audio_stream import AudioStreamer  # noqa: E402

TARGET_RATE = 16000
FRAME_MS = 100


def load_pcm16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif channels != 1:
        raise SystemExit(f"Unsupported channel count: {channels}")
    if rate != TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)
    return pcm


def post(server: str, path: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(f"{server}{path}", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read() or b"{}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--server", default=os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/"))
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
    parser.add_argument("--no-end", action="store_true", help="leave the meeting running afterwards")
    parser.add_argument("--arm", action="store_true", help="send to the robot arm's voice input (/ws/arm/audio)")
    args = parser.parse_args()

    pcm = load_pcm16k(args.wav)
    seconds = len(pcm) / (TARGET_RATE * 2)
    print(f"[replay] {args.wav.name}: {seconds:.1f}s of audio at {args.speed}x")

    if args.arm:
        streamer = AudioStreamer(args.server, source="replay", path="/ws/arm/audio")
    else:
        print(f"[replay] Starting meeting: {post(args.server, '/api/transcript/record/active')}")
        streamer = AudioStreamer(args.server, source="replay")

    step = int(TARGET_RATE * 2 * FRAME_MS / 1000)
    start = time.time()
    for i, pos in enumerate(range(0, len(pcm), step)):
        streamer.write(pcm[pos:pos + step])
        target = start + (i + 1) * FRAME_MS / 1000 / args.speed
        delay = target - time.time()
        if delay > 0:
            time.sleep(delay)
    # A little trailing silence lets Sarvam's VAD close the last utterance.
    for _ in range(int(1500 / FRAME_MS)):
        streamer.write(b"\x00" * step)
        time.sleep(FRAME_MS / 1000 / args.speed)
    streamer.close()

    if args.arm:
        time.sleep(2)   # let the last utterance finish decoding before the socket closes
        print("[replay] Done")
        return
    if args.no_end:
        print("[replay] Done — meeting left running (--no-end)")
        return
    print(f"[replay] Ending meeting: {post(args.server, '/api/transcript/record/processing')}")


if __name__ == "__main__":
    main()
