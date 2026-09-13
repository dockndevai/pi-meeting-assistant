"""
services/stt_stream.py

Live speech-to-text for a running meeting.

Two backends behind one interface (send_audio / close):
  • SarvamRealtime — WebSocket to saaras:v3-realtime. Partial transcripts while
    people speak, one final per utterance (Sarvam's own VAD picks the boundary).
  • SarvamChunked  — fallback. Buffers PCM, cuts at a quiet point every
    ~12-25s, and sends each chunk to the synchronous REST API (<30s limit).

Decision: the server owns the Sarvam connection, not the recording worker
  The worker only pushes raw 16 kHz PCM over a local WebSocket. One ingestion
  point means a replayed WAV (testing on a laptop) and the Pi mic take the
  exact same path, and the API key and reconnect logic live in one place.

Decision: connect lazily, on first audio
  Sarvam closes idle sockets (code 1008). While the meeting is paused no audio
  is sent, so the stream waits for audio instead of reconnect-looping.

Decision: realtime falls back to chunked for the rest of the meeting
  Realtime is beta. After 3 consecutive failed connects we stop retrying and
  switch to chunked REST, so the meeting keeps producing a transcript.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import io
import json
import os
import time
import wave
from typing import Awaitable, Callable, Optional

import httpx
import websockets

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono int16

# on_event(kind, payload): kind is "partial" | "final" | "state"
EventCallback = Callable[[str, dict], Awaitable[None]]


def _api_key() -> str:
    return os.environ.get("SARVAM_API_KEY", "").strip()


def _base_url() -> str:
    return os.environ.get("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")


class SarvamRealtime:
    MAX_CONNECT_FAILURES = 3
    MAX_QUEUED_CHUNKS = 600  # ~60s of 100ms chunks buffered across a reconnect

    def __init__(self, on_event: EventCallback):
        self.on_event = on_event
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._done = asyncio.Event()
        self._carry: Optional[bytes] = None
        self.failed = False  # True once we gave up and the caller should fall back

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def send_audio(self, pcm: bytes) -> None:
        if self._queue.qsize() >= self.MAX_QUEUED_CHUNKS:
            try:
                self._queue.get_nowait()  # drop oldest rather than grow unbounded
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(pcm)

    async def close(self, timeout: float = 8.0) -> None:
        """Ask Sarvam to finish, wait for the last finals, then stop."""
        self._closing = True
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except asyncio.TimeoutError:
            if self._task:
                self._task.cancel()

    def _ws_url(self) -> str:
        base = _base_url().replace("https://", "wss://").replace("http://", "ws://")
        params = {
            "model": os.environ.get("SARVAM_REALTIME_MODEL", "saaras:v3-realtime"),
            "language_code": os.environ.get("SARVAM_REALTIME_LANGUAGE", "hi-IN"),
            "mode": os.environ.get("SARVAM_STT_MODE", "codemix"),
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "stream_type": os.environ.get("SARVAM_REALTIME_STREAM_TYPE", "balanced"),
            "endpointing": "vad",
            # Meetings have natural mid-sentence pauses; 500ms default splits too eagerly.
            "silence_duration_ms": os.environ.get("SARVAM_REALTIME_SILENCE_MS", "800"),
        }
        return f"{base}/speech-to-text-realtime/ws?{httpx.QueryParams(params)}"

    async def _run(self) -> None:
        failures = 0
        backoff = 1.0
        try:
            while True:
                # Lazy connect: wait for audio (or close) before opening a socket.
                # A chunk held over from a failed connect goes first to keep order.
                if self._carry is not None:
                    first, self._carry = self._carry, None
                else:
                    first = await self._queue.get()
                if first is None:
                    break
                await self.on_event("state", {"stt": "connecting", "backend": "realtime"})
                try:
                    async with websockets.connect(
                        self._ws_url(),
                        extra_headers={"api-subscription-key": _api_key()},
                        open_timeout=10,
                        ping_interval=20,
                        max_size=2**22,
                    ) as ws:
                        failures = 0
                        backoff = 1.0
                        ended_by_client = await self._session(ws, first)
                        if ended_by_client:
                            break
                        await self.on_event("state", {"stt": "reconnecting", "backend": "realtime"})
                except Exception as e:
                    failures += 1
                    print(f"[stt] Realtime connect/session error ({failures}): {e}")
                    # Hold the chunk we already took so it isn't lost or reordered.
                    self._carry = first
                    if failures >= self.MAX_CONNECT_FAILURES:
                        self.failed = True
                        await self.on_event("state", {"stt": "fallback", "backend": "realtime", "detail": str(e)})
                        return
                    await self.on_event("state", {"stt": "reconnecting", "backend": "realtime", "detail": str(e)})
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
        finally:
            self._done.set()

    async def _session(self, ws, first_chunk: bytes) -> bool:
        """Run one socket session. Returns True if it ended because we closed it."""
        got_begin = asyncio.Event()

        async def sender():
            chunk: Optional[bytes] = first_chunk
            while True:
                if chunk is None:
                    await ws.send(json.dumps({"event": "end"}))
                    return
                await ws.send(json.dumps({
                    "event": "audio_input",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }))
                chunk = await self._queue.get()

        async def receiver():
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event = msg.get("event")
                if event == "session.begin":
                    got_begin.set()
                    await self.on_event("state", {"stt": "live", "backend": "realtime"})
                elif event == "transcript.partial":
                    await self.on_event("partial", {"text": msg.get("text", "")})
                elif event == "transcript.final":
                    text = (msg.get("text") or "").strip()
                    if text:
                        await self.on_event("final", {"text": text, "language": msg.get("language")})
                    else:
                        await self.on_event("partial", {"text": ""})
                elif event == "error":
                    print(f"[stt] Realtime error: {msg}")
                    if msg.get("is_fatal"):
                        raise RuntimeError(f"{msg.get('code')}: {msg.get('message')}")
                elif event == "session.end":
                    return

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)

        if send_task in done and send_task.exception() is None:
            # We sent "end" — give Sarvam time to flush the last finals.
            try:
                await asyncio.wait_for(recv_task, timeout=6)
            except (asyncio.TimeoutError, Exception):
                pass
            return True

        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                if not got_begin.is_set():
                    raise exc  # counts as a connect failure
                print(f"[stt] Realtime session dropped: {exc}")
        return False

    def drain_pending_audio(self) -> list[bytes]:
        chunks = [self._carry] if self._carry is not None else []
        self._carry = None
        while not self._queue.empty():
            c = self._queue.get_nowait()
            if c is not None:
                chunks.append(c)
        return chunks


class SarvamChunked:
    """Fallback: cut PCM at quiet points and transcribe each chunk via REST."""

    MIN_CHUNK_S = 12
    MAX_CHUNK_S = 25          # REST API limit is 30s
    QUIET_RMS = 300           # int16 RMS below this counts as a pause

    def __init__(self, on_event: EventCallback):
        self.on_event = on_event
        self._buf = bytearray()
        self._pending: set[asyncio.Task] = set()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10))

    def start(self) -> None:
        pass

    async def send_audio(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        if len(self._buf) >= self.MIN_CHUNK_S * BYTES_PER_SECOND:
            cut = self._find_cut()
            if cut:
                chunk = bytes(self._buf[:cut])
                del self._buf[:cut]
                self._submit(chunk)

    def _find_cut(self) -> Optional[int]:
        """First quiet 100ms window after MIN_CHUNK_S, or a hard cut at MAX_CHUNK_S."""
        window = BYTES_PER_SECOND // 10
        start = self.MIN_CHUNK_S * BYTES_PER_SECOND
        limit = min(len(self._buf), self.MAX_CHUNK_S * BYTES_PER_SECOND)
        for pos in range(start, limit - window + 1, window):
            if audioop.rms(bytes(self._buf[pos:pos + window]), 2) < self.QUIET_RMS:
                return pos + window
        if len(self._buf) >= self.MAX_CHUNK_S * BYTES_PER_SECOND:
            return self.MAX_CHUNK_S * BYTES_PER_SECOND
        return None

    def _submit(self, pcm: bytes) -> None:
        # Chunks are transcribed concurrently but must be emitted in order.
        task =asyncio.create_task(self._transcribe_in_order(pcm, list(self._pending)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _transcribe_in_order(self, pcm: bytes, earlier: list[asyncio.Task]) -> None:
        text_task = asyncio.create_task(self._transcribe(pcm))
        if earlier:
            await asyncio.gather(*earlier, return_exceptions=True)
        try:
            result = await text_task
        except Exception as e:
            print(f"[stt] Chunk transcription failed: {e}")
            await self.on_event("state", {"stt": "error", "backend": "chunked", "detail": str(e)})
            return
        if result.get("text"):
            await self.on_event("final", result)

    async def _transcribe(self, pcm: bytes) -> dict:
        wav = io.BytesIO()
        with wave.open(wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        res = await self._client.post(
            f"{_base_url()}/speech-to-text",
            headers={"api-subscription-key": _api_key()},
            files={"file": ("chunk.wav", wav.getvalue(), "audio/wav")},
            data={
                "model": os.environ.get("SARVAM_STT_MODEL", "saaras:v3"),
                "mode": os.environ.get("SARVAM_STT_MODE", "codemix"),
                "language_code": os.environ.get("SARVAM_LANGUAGE_CODE", "unknown"),
            },
        )
        if not res.is_success:
            raise RuntimeError(f"Sarvam REST {res.status_code}: {res.text[:300]}")
        body = res.json()
        return {"text": (body.get("transcript") or "").strip(), "language": body.get("language_code")}

    async def close(self, timeout: float = 60.0) -> None:
        if len(self._buf) >= BYTES_PER_SECOND:  # ignore sub-second tails
            self._submit(bytes(self._buf))
        self._buf.clear()
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout)
        await self._client.aclose()


class LiveTranscriber:
    """
    Picks realtime by default (STT_BACKEND=realtime|chunked) and swaps to
    chunked transparently if realtime gives up.
    """

    def __init__(self, on_event: EventCallback):
        self.on_event = on_event
        preferred = os.environ.get("STT_BACKEND", "realtime").strip().lower()
        self.backend_name = "chunked" if preferred == "chunked" else "realtime"
        self._impl = SarvamChunked(on_event) if self.backend_name == "chunked" else SarvamRealtime(on_event)
        self._impl.start()

    async def send_audio(self, pcm: bytes) -> None:
        if isinstance(self._impl, SarvamRealtime) and self._impl.failed:
            await self._switch_to_chunked()
        await self._impl.send_audio(pcm)

    async def _switch_to_chunked(self) -> None:
        held = self._impl.drain_pending_audio()
        print(f"[stt] Switching to chunked REST fallback ({len(held)} buffered chunks carried over)")
        self._impl = SarvamChunked(self.on_event)
        self.backend_name = "chunked"
        await self.on_event("state", {"stt": "live", "backend": "chunked"})
        for c in held:
            await self._impl.send_audio(c)

    async def close(self) -> None:
        if isinstance(self._impl, SarvamRealtime) and self._impl.failed:
            await self._switch_to_chunked()
        await self._impl.close()
        if isinstance(self._impl, SarvamRealtime) and self._impl.failed:
            await self._switch_to_chunked()
            await self._impl.close()
