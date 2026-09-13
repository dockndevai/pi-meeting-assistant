"""
services/live.py

LiveHub — the single owner of "the meeting happening right now".

Holds the active meeting, feeds its audio to the live transcriber, persists
final utterances, and broadcasts every change to all connected dashboards.

Decision: WebSocket broadcast to every screen
  The dashboard is a shared room screen, possibly also open on laptops.
  Every viewer must see the same transcript at the same moment, and a screen
  that joins late gets a full snapshot on connect.

Decision: pause is enforced in two places
  The worker stops capturing (nothing hits the WAV) and the hub drops any
  audio that still arrives. A paused meeting sends nothing to Sarvam.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

from services.meeting_store import MeetingStore, format_transcript
from services.stt_stream import LiveTranscriber

PAUSE_FLAG = Path("/tmp/record_paused")
# A meeting left "live" by a server restart, with activity in the last
# 10 minutes, is resumed instead of starting a fresh one.
RESUME_WINDOW_S = 10 * 60


class LiveHub:
    def __init__(self) -> None:
        self.store = MeetingStore()
        self.store.mark_stale_live_meetings(RESUME_WINDOW_S)
        self._lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()
        self.meeting: Optional[dict] = None
        self.paused = False
        self.partial = ""
        self.stt_state = {"stt": "idle", "backend": None}
        self._transcriber: Optional[LiveTranscriber] = None
        PAUSE_FLAG.unlink(missing_ok=True)

    # ── Dashboard clients ──────────────────────────────────────────────────
    async def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        await ws.send_text(json.dumps(self.snapshot()))

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "server_time": time.time(),
            "meeting": self.meeting,
            "paused": self.paused,
            "partial": self.partial,
            "stt": self.stt_state,
            "segments": self.store.segments(self.meeting["id"]) if self.meeting else [],
        }

    # ── Meeting lifecycle ──────────────────────────────────────────────────
    async def start_meeting(self, source: str = "mic") -> dict:
        async with self._lock:
            if self.meeting:
                return self.meeting
            resumed = self.store.latest_live_meeting(RESUME_WINDOW_S)
            self.meeting = resumed or self.store.create_meeting(source)
            self.paused = False
            self.partial = ""
            PAUSE_FLAG.unlink(missing_ok=True)
            self._transcriber = LiveTranscriber(self._on_stt_event)
            self.store.set_stt_backend(self.meeting["id"], self._transcriber.backend_name)
            print(f"[live] Meeting #{self.meeting['id']} {'resumed' if resumed else 'started'} "
                  f"(stt={self._transcriber.backend_name})")
            meeting = self.meeting
        await self.broadcast({"type": "meeting.started", "meeting": meeting, "server_time": time.time()})
        return meeting

    async def end_meeting(self) -> Optional[dict]:
        """
        Stop live STT (waiting for the last finals), close the meeting and
        return {"meeting", "transcript", "duration_seconds"} — or None if no
        meeting was running.
        """
        async with self._lock:
            meeting, transcriber = self.meeting, self._transcriber
            self._transcriber = None
        if not meeting:
            return None

        if transcriber:
            try:
                await transcriber.close()
            except Exception as e:
                print(f"[live] Error closing transcriber: {e}")

        async with self._lock:
            ended = self.store.end_meeting(meeting["id"]) or meeting
            segments = self.store.segments(meeting["id"])
            self.meeting = None
            self.paused = False
            self.partial = ""
            self.stt_state = {"stt": "idle", "backend": None}
            PAUSE_FLAG.unlink(missing_ok=True)

        duration = int((ended.get("ended_at") or time.time()) - ended["started_at"])
        print(f"[live] Meeting #{meeting['id']} ended — {len(segments)} utterances, {duration}s")
        await self.broadcast({"type": "meeting.ended", "meeting": ended})
        return {"meeting": ended, "transcript": format_transcript(segments), "duration_seconds": duration}

    async def set_paused(self, paused: bool) -> bool:
        if not self.meeting:
            return False
        self.paused = paused
        if paused:
            PAUSE_FLAG.write_text("1")
            self.partial = ""
        else:
            PAUSE_FLAG.unlink(missing_ok=True)
        print(f"[live] Meeting #{self.meeting['id']} {'paused' if paused else 'resumed'}")
        await self.broadcast({"type": "paused", "paused": paused})
        return True

    # ── Audio + transcript ─────────────────────────────────────────────────
    async def push_audio(self, pcm: bytes) -> None:
        if self.paused or not self._transcriber:
            return
        await self._transcriber.send_audio(pcm)

    async def add_segment(self, text: str, language: Optional[str] = None) -> Optional[dict]:
        meeting = self.meeting
        if not meeting:
            return None
        offset = max(0.0, time.time() - meeting["started_at"])
        segment = self.store.add_segment(meeting["id"], text, language, offset)
        self.partial = ""
        await self.broadcast({"type": "segment", "segment": segment})
        return segment

    async def _on_stt_event(self, kind: str, payload: dict) -> None:
        if kind == "final":
            await self.add_segment(payload["text"], payload.get("language"))
        elif kind == "partial":
            if self.paused:
                return
            self.partial = payload.get("text", "")
            await self.broadcast({"type": "partial", "text": self.partial})
        elif kind == "state":
            self.stt_state = payload
            if self.meeting and payload.get("backend"):
                self.store.set_stt_backend(self.meeting["id"], payload["backend"])
            await self.broadcast({"type": "stt", **payload})


hub = LiveHub()
