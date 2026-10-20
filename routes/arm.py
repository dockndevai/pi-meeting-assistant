"""
routes/arm.py — the Laya robot arm.

  GET  /arm                 simulator + control screen
  POST /api/arm/command     {"text": "..."} → Laya decides, arm moves
  POST /api/arm/gesture     {"gesture": "wave", "speed": 1} → play directly (no Laya)
  GET  /api/arm/state       status, pose, recent decisions
  WS   /ws/arm              live pose (~25 Hz), decisions, transcripts → screens
  WS   /ws/arm/audio        16 kHz mono int16 PCM in (Pi mic worker or browser mic)
"""

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from arm.hub import hub
from arm.motion import GESTURE_NAMES

router = APIRouter()
PAGE = Path(__file__).parent.parent / "public" / "arm.html"


@router.get("/arm", include_in_schema=False)
async def arm_page():
    return FileResponse(PAGE)


class CommandRequest(BaseModel):
    text: str


class GestureRequest(BaseModel):
    gesture: str
    speed: Optional[float] = None


@router.post("/api/arm/command")
async def command(req: CommandRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty command")
    return await hub.command([req.text], source="text")


@router.post("/api/arm/gesture")
async def gesture(req: GestureRequest):
    if req.gesture not in GESTURE_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown gesture; one of {GESTURE_NAMES}")
    return await hub.play(req.gesture, req.speed)


@router.get("/api/arm/state")
async def state():
    return {**hub.status(), "pose": hub.engine.pose, "playing": hub.engine.playing, "history": hub.history[-20:]}


@router.websocket("/ws/arm")
async def arm_feed(ws: WebSocket):
    await ws.accept()
    await hub.add_client(ws)
    try:
        while True:
            await ws.receive_text()      # keepalive pings only
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove_client(ws)


@router.websocket("/ws/arm/audio")
async def arm_audio(ws: WebSocket):
    await ws.accept()
    source = ws.query_params.get("source", "mic")
    loop = asyncio.get_running_loop()
    try:
        from arm.stt import UtteranceStream
        stream = await loop.run_in_executor(None, UtteranceStream)
    except Exception as e:
        await hub.broadcast({"type": "voice", "state": "error", "detail": str(e)})
        await ws.close(code=1011)
        return
    hub.listening += 1
    await hub.broadcast({"type": "voice", "state": "listening", "source": source, "listening": hub.listening})
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            pcm = msg.get("bytes")
            if not pcm:
                continue
            for kind, payload in await loop.run_in_executor(None, stream.feed, pcm):
                if kind == "speech_start":
                    await hub.broadcast({"type": "voice", "state": "hearing", "source": source})
                elif kind == "partial":
                    await hub.broadcast({"type": "partial", "text": payload, "source": source})
                elif kind == "utterance":
                    await hub.broadcast({"type": "heard", "source": source, **payload})
                    texts = [t for t in payload["texts"].values() if t]
                    if texts:
                        asyncio.create_task(hub.command(texts, source=f"voice:{source}", stt=payload))
    except WebSocketDisconnect:
        pass
    finally:
        hub.listening -= 1
        await hub.broadcast({"type": "voice", "state": "idle" if not hub.listening else "listening",
                             "source": source, "listening": hub.listening})
