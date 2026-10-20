"""
arm/stt.py — fully local speech-to-text for arm commands (Vosk).

Decision: Vosk, not Whisper, on the Pi
  Commands are 1–3 s. Vosk's small models (~50–80 MB) decode that in a few
  hundred ms on a Pi CPU and stream partial words; Whisper-small takes
  seconds per utterance there. Nothing leaves the device.

Decision: two recognisers, one per language, every utterance
  The Indian-English model hears "wave hello" well and Hindi badly; the Hindi
  model is the reverse (it writes Devanagari, which Laya reads natively).
  Both run on each utterance and arm.brain lets Laya pick.

Decision: our own VAD endpointing (webrtcvad)
  We cut utterances once, then decode the same audio with both models, so
  both see identical boundaries.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
END_SILENCE_MS = int(os.environ.get("ARM_END_SILENCE_MS", "600"))
MAX_UTTERANCE_S = 8

_MODELS: dict[str, object] = {}


def models_dir() -> Path:
    return Path(os.environ.get("VOSK_MODELS_DIR", "/models/vosk"))


def load_models() -> dict:
    """Load every Vosk model under VOSK_MODELS_DIR once (en-in, hi …)."""
    if _MODELS:
        return _MODELS
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    for d in sorted(models_dir().glob("vosk-model-*")):
        lang = "hi" if "-hi-" in d.name else "en" if "-en" in d.name else d.name
        _MODELS[lang] = Model(str(d))
        print(f"[arm] Vosk model loaded: {d.name} ({lang})")
    if not _MODELS:
        raise RuntimeError(f"No Vosk models in {models_dir()} — run scripts/fetch_models.sh")
    return _MODELS


class UtteranceStream:
    """
    Feed 16 kHz mono int16 PCM; get back events:
      ("speech_start", None) · ("partial", text) · ("utterance", {"texts": {...}, "stt_ms": n, "audio_s": x})
    """

    def __init__(self):
        import webrtcvad
        from vosk import KaldiRecognizer

        self._models = load_models()
        self._vad = webrtcvad.Vad(int(os.environ.get("ARM_VAD_LEVEL", "2")))
        self._partial_rec = KaldiRecognizer(self._models.get("en") or next(iter(self._models.values())), SAMPLE_RATE)
        self._KaldiRecognizer = KaldiRecognizer
        self._buf = bytearray()
        self._utt = bytearray()
        self._in_speech = False
        self._silence_ms = 0
        self._speech_ms = 0
        self._last_partial = ""

    def feed(self, pcm: bytes) -> list[tuple[str, object]]:
        events = []
        self._buf.extend(pcm)
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            speech = self._vad.is_speech(frame, SAMPLE_RATE)
            if not self._in_speech:
                if speech:
                    self._speech_ms += FRAME_MS
                    self._utt.extend(frame)
                    if self._speech_ms >= 90:            # 3 voiced frames = real speech, not a click
                        self._in_speech, self._silence_ms = True, 0
                        events.append(("speech_start", None))
                        self._partial_rec.AcceptWaveform(bytes(self._utt))
                else:
                    self._speech_ms = 0
                    self._utt = self._utt[-FRAME_BYTES * 10:]   # keep 300 ms of lead-in
                continue
            self._utt.extend(frame)
            self._partial_rec.AcceptWaveform(frame)
            p = json.loads(self._partial_rec.PartialResult()).get("partial", "")
            if p and p != self._last_partial:
                self._last_partial = p
                events.append(("partial", p))
            self._silence_ms = 0 if speech else self._silence_ms + FRAME_MS
            if self._silence_ms >= END_SILENCE_MS or len(self._utt) >= MAX_UTTERANCE_S * SAMPLE_RATE * 2:
                events.append(("utterance", self._decode(bytes(self._utt))))
                self._reset()
        return events

    def _decode(self, pcm: bytes) -> dict:
        t = time.perf_counter()
        texts = {}
        for lang, model in self._models.items():
            rec = self._KaldiRecognizer(model, SAMPLE_RATE)
            rec.AcceptWaveform(pcm)
            texts[lang] = json.loads(rec.FinalResult()).get("text", "").strip()
        return {"texts": texts, "stt_ms": round((time.perf_counter() - t) * 1000), "audio_s": round(len(pcm) / (SAMPLE_RATE * 2), 2)}

    def _reset(self) -> None:
        self._utt = bytearray()
        self._in_speech = False
        self._silence_ms = self._speech_ms = 0
        self._last_partial = ""
        self._partial_rec = self._KaldiRecognizer(self._models.get("en") or next(iter(self._models.values())), SAMPLE_RATE)
