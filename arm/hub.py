"""
arm/hub.py — glue: commands in (text or voice), Laya decision, motion out,
everything broadcast to every open screen.

Timing for each command is measured and sent with the decision, so the demo
screen shows where the time went: speech-to-text, Laya, first motion.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import WebSocket

from arm.brain import ArmBrain
from arm.drivers import make_driver
from arm.motion import GESTURE_NAMES, HOME, JOINTS, LIMITS, MotionEngine


class ArmHub:
    def __init__(self):
        self.brain = ArmBrain()
        self.driver = make_driver()
        self.engine = MotionEngine(self.driver.write, on_pose=self._on_pose)
        self._clients: set[WebSocket] = set()
        self.history: list[dict] = []
        self.listening = 0          # open voice streams
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self.engine.start()
        # Weights take seconds to load; don't hold up the web server for it.
        asyncio.get_running_loop().run_in_executor(None, self._load_brain)

    def _load_brain(self) -> None:
        self.brain.load()
        asyncio.run_coroutine_threadsafe(self.broadcast({"type": "status", **self.status()}), self._loop)

    def status(self) -> dict:
        return {"brain": self.brain.status, "brain_kind": self.brain.kind, "brain_error": self.brain.error,
                "driver": self.driver.name, "listening": self.listening}

    # ── screens ────────────────────────────────────────────────────────────
    async def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        await ws.send_text(json.dumps({
            "type": "snapshot", **self.status(), "gestures": GESTURE_NAMES, "joints": JOINTS,
            "limits": LIMITS, "home": HOME, "pose": self.engine.pose, "playing": self.engine.playing,
            "history": self.history[-20:],
        }))

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                self._clients.discard(ws)

    async def _on_pose(self, pose: dict, playing: Optional[str]) -> None:
        if self._clients:
            await self.broadcast({"type": "pose", "pose": {k: round(v, 1) for k, v in pose.items()}, "playing": playing})

    # ── commands ───────────────────────────────────────────────────────────
    async def command(self, texts: list[str], source: str, stt: Optional[dict] = None) -> dict:
        """Decide with Laya and act. `texts` are candidate transcripts of one utterance."""
        t0 = time.perf_counter()
        try:
            decision = await asyncio.get_running_loop().run_in_executor(None, self.brain.decide, texts)
        except RuntimeError as e:
            evt = {"type": "decision", "error": str(e), "source": source, "texts": texts}
            await self.broadcast(evt)
            return evt

        action = None
        if decision["gesture"] != "none" and not decision["gated"]:
            played, seconds = self.engine.play(decision["gesture"], decision["speed_score"])
            action = {"gesture": played, "seconds": round(seconds, 2)}
        evt = {
            "type": "decision", "source": source, "at": time.time(),
            **{k: decision[k] for k in ("text", "gesture", "confidence", "top", "speed_score", "candidates", "gated")},
            "action": action,
            "timing_ms": {"stt": (stt or {}).get("stt_ms"), "laya": decision["laya_ms"],
                          "total": round((time.perf_counter() - t0) * 1000) + ((stt or {}).get("stt_ms") or 0)},
            "cached": decision.get("cached", False),
        }
        self.history.append(evt)
        self.history = self.history[-100:]
        print(f"[arm] {source}: {decision['text']!r} → {decision['gesture']} "
              f"({decision['confidence']:.2f}){' GATED' if decision['gated'] else ''} "
              f"laya={decision['laya_ms']}ms stt={(stt or {}).get('stt_ms')}ms")
        await self.broadcast(evt)
        return evt

    async def play(self, gesture: str, speed_score: Optional[float] = None) -> dict:
        played, seconds = self.engine.play(gesture, speed_score)
        evt = {"type": "manual", "gesture": played, "seconds": round(seconds, 2)}
        await self.broadcast(evt)
        return evt


hub = ArmHub()
