"""
routes/live.py

Real-time endpoints:
  WS   /ws/audio        worker → server: raw 16 kHz mono int16 PCM frames
  WS   /ws/live         server → dashboards: snapshot, segments, partials, state
  GET  /api/live/state  same snapshot over plain HTTP (debugging, curl)
  POST /api/live/pause  / resume
  POST /api/live/inject add a fake final utterance (MOCK / LIVE_DEV only)
"""

import os

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from services.live import hub

router = APIRouter()


@router.websocket("/ws/audio")
async def audio_ingest(ws: WebSocket):
    await ws.accept()
    source = ws.query_params.get("source", "mic")
    await hub.start_meeting(source=source)
    print(f"[live] Audio stream connected (source={source})")
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                await hub.push_audio(data)
    except WebSocketDisconnect:
        pass
    finally:
        # Ending the meeting is the worker's call (/record/processing), not a
        # dropped socket — the worker may just be reconnecting.
        print("[live] Audio stream disconnected")


@router.websocket("/ws/live")
async def live_feed(ws: WebSocket):
    await ws.accept()
    await hub.add_client(ws)
    try:
        while True:
            # Clients only send keepalive pings; controls go through REST.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove_client(ws)


@router.get("/api/live/state")
async def live_state():
    return hub.snapshot()


@router.post("/api/live/pause")
async def pause():
    if not await hub.set_paused(True):
        raise HTTPException(status_code=400, detail="No meeting is running")
    return {"paused": True}


@router.post("/api/live/resume")
async def resume():
    if not await hub.set_paused(False):
        raise HTTPException(status_code=400, detail="No meeting is running")
    return {"paused": False}


class InjectRequest(BaseModel):
    text: str


@router.post("/api/live/inject")
async def inject(req: InjectRequest):
    """Dev helper: test the live UI without a mic or a Sarvam key."""
    dev = os.environ.get("MOCK", "true").lower() == "true" or os.environ.get("LIVE_DEV", "").lower() == "true"
    if not dev:
        raise HTTPException(status_code=403, detail="Only available with MOCK=true or LIVE_DEV=true")
    if not hub.meeting:
        await hub.start_meeting(source="inject")
    segment = await hub.add_segment(req.text.strip())
    return {"segment": segment}
