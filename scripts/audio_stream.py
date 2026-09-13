"""
scripts/audio_stream.py

AudioStreamer — pushes 16 kHz PCM to the server's /ws/audio in the background.

Decision: separate sender thread with a bounded queue
  The mic read loop must never block on the network. If the socket is down,
  audio queues (up to ~60s) while the thread reconnects; beyond that the
  oldest audio is dropped. The WAV on disk is still complete either way, so
  batch STT remains a fallback.
"""

import queue
import threading
import time

from websockets.sync.client import connect

SEND_BYTES = 3200            # batch 30ms frames into ~100ms messages
MAX_QUEUED = 600             # ~60s of 100ms messages


class AudioStreamer:
    def __init__(self, server_url: str, source: str = "mic"):
        ws_base = server_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        self.url = f"{ws_base}/ws/audio?source={source}"
        self._q: "queue.Queue[bytes | None]" = queue.Queue()
        self._buf = bytearray()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stopped = False
        self._thread.start()

    def write(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        if len(self._buf) >= SEND_BYTES:
            self._enqueue(bytes(self._buf))
            self._buf.clear()

    def _enqueue(self, data: bytes) -> None:
        if self._q.qsize() >= MAX_QUEUED:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
        self._q.put(data)

    def close(self, timeout: float = 5.0) -> None:
        """Flush what's buffered, then stop the sender thread."""
        if self._buf:
            self._enqueue(bytes(self._buf))
            self._buf.clear()
        self._q.put(None)
        self._thread.join(timeout)
        self._stopped = True

    def _run(self) -> None:
        pending = None
        backoff = 1.0
        while True:
            try:
                with connect(self.url, open_timeout=5, max_size=None) as ws:
                    print(f"[stream] Connected to {self.url}")
                    backoff = 1.0
                    while True:
                        data = pending if pending is not None else self._q.get()
                        pending = None
                        if data is None:
                            return
                        try:
                            ws.send(data)
                        except Exception:
                            pending = data  # resend after reconnect
                            raise
            except Exception as e:
                print(f"[stream] Live audio stream unavailable ({e}); retrying in {backoff:.0f}s")
                # Stop promptly if close() was requested while disconnected.
                deadline = time.time() + backoff
                while time.time() < deadline:
                    if pending is None:
                        try:
                            head = self._q.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        if head is None:
                            return
                        pending = head
                    else:
                        time.sleep(0.2)
                backoff = min(backoff * 2, 10.0)
