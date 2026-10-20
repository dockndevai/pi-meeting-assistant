#!/usr/bin/env python3
"""
scripts/demo/record_demo.py — record the Laya Arm demo video (macOS helper).

Drives /arm in headless Chromium, types commands in English, Hinglish and
Hindi, streams spoken commands (macOS `say` voices) into the arm's voice
input, records the screen, then muxes the spoken audio into the video.

    pip install playwright && python -m playwright install chromium
    python scripts/demo/record_demo.py --server http://localhost:3000 --out docs/demo

Outputs demo.mp4 (with audio), demo.gif (silent, for the README) and
timeline.json (what was sent when, and what Laya decided).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_stream import AudioStreamer  # noqa: E402

# (kind, text, caption, voice)
SCRIPT = [
    ("type", "wave hello to everyone", "Typed · English", None),
    ("type", "haath hilao", "Typed · Hinglish", None),
    ("type", "धीरे से झुको", "Typed · Hindi — “bow slowly”", None),
    ("type", "pick up the pen quickly", "Typed · English, with speed", None),
    ("type", "the meeting is at three o'clock", "Not a command — the arm stays still", None),
    ("say", "nach ke dikhao", "Spoken · Hinglish — on-device speech-to-text", "Rishi"),
    ("say", "please say no", "Spoken · English", "Rishi"),
    ("type", "ruko", "Typed · Hinglish — “stop”", None),
]

CAPTION_CSS = """
#demoCaption { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); z-index: 99;
  background: rgba(10,10,10,0.88); color: #fff; font: 600 17px Inter, system-ui, sans-serif;
  padding: 10px 18px; border-radius: 999px; letter-spacing: -0.01em; transition: opacity .25s; }
"""


def tts(text: str, voice: str, out: Path) -> Path:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1",
                    "-sample_fmt", "s16", str(out)], check=True)
    return out


def stream_wav(server: str, path: Path) -> None:
    pcm = wave.open(str(path)).readframes(10**9)
    s = AudioStreamer(server, source="demo", path="/ws/arm/audio")
    step = 3200
    t0 = time.time()
    for i, pos in enumerate(range(0, len(pcm), step)):
        s.write(pcm[pos:pos + step])
        time.sleep(max(0, t0 + (i + 1) * 0.1 - time.time()))
    for _ in range(12):                      # trailing silence closes the utterance
        s.write(b"\x00" * step)
        time.sleep(0.1)
    time.sleep(2.5)
    s.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:3000")
    ap.add_argument("--out", default="docs/demo")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=760)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="arm-demo-"))

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": args.width, "height": args.height},
                                        record_video_dir=str(work), record_video_size={"width": args.width, "height": args.height},
                                        color_scheme="light")
        page = await ctx.new_page()
        video_t0 = time.time()
        await page.goto(f"{args.server}/arm")
        await page.add_style_tag(content=CAPTION_CSS)
        await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=demoCaption>Laya Arm · voice → Laya decision → motion</div>')")
        await page.wait_for_function("document.querySelector('#chipBrain').classList.contains('ok')", timeout=120_000)
        await page.wait_for_timeout(1800)

        timeline, audio_marks = [], []
        for kind, text, caption, voice in SCRIPT:
            await page.evaluate("t => document.getElementById('demoCaption').textContent = t", caption)
            n_before = await page.evaluate("document.querySelectorAll('#history li').length")
            if kind == "type":
                await page.click("#cmdInput")
                await page.type("#cmdInput", text, delay=55)
                await page.wait_for_timeout(250)
                await page.press("#cmdInput", "Enter")
            else:
                wav = tts(text, voice, work / f"say{len(audio_marks)}.wav")
                audio_marks.append((time.time() - video_t0, wav))
                await asyncio.to_thread(stream_wav, args.server, wav)
            await page.wait_for_function(f"document.querySelectorAll('#history li').length > {n_before}", timeout=30_000)
            decision = await page.evaluate("document.querySelector('#history li').innerText")
            timeline.append({"t": round(time.time() - video_t0, 2), "kind": kind, "text": text, "laya": decision})
            print(f"  {kind:4} {text!r:36} → {decision!r}")
            # let the gesture finish (or a beat for "no action")
            await page.wait_for_timeout(600)
            await page.wait_for_function("document.getElementById('nowPlaying').textContent === 'Rest'", timeout=15_000)
            await page.wait_for_timeout(700)

        await page.evaluate("document.getElementById('demoCaption').textContent = 'Runs on a Raspberry Pi · no cloud · github.com/dockndevai/pi-meeting-assistant'")
        await page.wait_for_timeout(2500)
        video_path = await page.video.path()
        await ctx.close()
        await browser.close()

    raw = out / "demo_raw.webm"
    shutil.move(video_path, raw)
    (out / "timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False))

    # MP4 with the spoken commands laid onto the video timeline.
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw)]
    for _, wav in audio_marks:
        cmd += ["-i", str(wav)]
    if audio_marks:
        delays = "".join(f"[{i + 1}:a]adelay={int(t * 1000)}|{int(t * 1000)}[a{i}];" for i, (t, _) in enumerate(audio_marks))
        mix = "".join(f"[a{i}]" for i in range(len(audio_marks)))
        cmd += ["-filter_complex", f"{delays}{mix}amix=inputs={len(audio_marks)}:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "96k"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow", "-movflags", "+faststart",
            str(out / "demo.mp4")]
    subprocess.run(cmd, check=True)
    # GIF for the README: smaller, 12 fps, shared palette.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-vf",
                    "fps=12,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                    str(out / "demo.gif")], check=True)
    raw.unlink()
    for f in ("demo.mp4", "demo.gif"):
        print(f"wrote {out / f} ({(out / f).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
